"""Test suite for the deterministic core of the Freight Document Intelligence agent.

Runs entirely offline against the committed synthetic document packs
(data/shipments/) and their ground truth. No API key required.

    python -m pytest tests/ -v
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import indexer, rag
from tools.compliance import run_compliance
from tools.governance import ReviewGate, TraceLog
from tools.pdf_extract import extract_document, extract_pack
from tools.reconcile import reconcile_pack

DATA = Path(__file__).parent.parent / "data"
GT = json.loads((DATA / "ground_truth.json").read_text())
SHIPMENTS = {s["shipment_id"]: s for s in GT["shipments"]}


@pytest.fixture(scope="module")
def packs():
    return {sid: extract_pack(DATA / "shipments" / sid) for sid in SHIPMENTS}


@pytest.fixture(scope="module")
def col(packs):
    docs = [d for pack in packs.values() for d in pack.values()]
    return indexer.build_index(docs, persist_dir=None)


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------

def test_pack_has_eight_documents(packs):
    for sid, pack in packs.items():
        assert len(pack) == 8, f"{sid} extracted {len(pack)} docs"


def test_doc_type_detection():
    doc = extract_document(DATA / "shipments" / "MSKU-2847" / "01_bill_of_lading.pdf")
    assert doc.doc_type == "bill_of_lading"
    assert doc.shipment_id == "MSKU-2847"


def test_money_and_weight_parsing(packs):
    entry = packs["MSKU-2847"]["customs_entry"]
    assert entry.value("declared_value") == pytest.approx(46800.00)
    bl = packs["MSKU-2847"]["bill_of_lading"]
    assert isinstance(bl.value("gross_weight_kg"), float)


def test_combined_package_field_split(packs):
    bl = packs["MSKU-2847"]["bill_of_lading"]
    assert isinstance(bl.value("package_count"), int)
    assert bl.value("package_type") == "CARTONS"


def test_invoice_line_items_parsed(packs):
    inv = packs["MSKU-2847"]["commercial_invoice"]
    assert len(inv.line_items) == 2
    line_sum = round(sum(li["amount"] for li in inv.line_items), 2)
    assert line_sum == pytest.approx(inv.value("invoice_total"))


def test_blank_isf_manufacturer_extracted_as_empty(packs):
    isf = packs["MSKU-2847"]["isf_filing"]
    assert isf.value("manufacturer_name") == ""


def test_every_field_has_page_and_confidence(packs):
    for pack in packs.values():
        for doc in pack.values():
            for f in doc.fields.values():
                assert f.page >= 1
                assert 0.0 < f.confidence <= 1.0


def test_extraction_accuracy_against_ground_truth(packs):
    total = correct = 0
    for sid, ship in SHIPMENTS.items():
        for doc_type, doc_gt in ship["documents"].items():
            ex = packs[sid][doc_type]
            for fname, gt_val in doc_gt["fields"].items():
                total += 1
                ex_val = ex.value(fname)
                if isinstance(gt_val, (int, float)) and isinstance(ex_val, (int, float)):
                    ok = float(ex_val) == pytest.approx(float(gt_val), abs=0.01)
                else:
                    ok = str(gt_val).strip().lower() == str(ex_val).strip().lower()
                correct += bool(ok)
    assert correct / total >= 0.95, f"extraction accuracy {100 * correct / total:.1f}% < 95%"


# ---------------------------------------------------------------------------
# reconciliation
# ---------------------------------------------------------------------------

def test_all_injected_discrepancies_caught(packs):
    for sid, ship in SHIPMENTS.items():
        found = {f.check for f in reconcile_pack(packs[sid], sid)}
        found |= {c.check for c in run_compliance(packs[sid], sid) if c.status == "FAIL"}
        for inj in ship["injected_discrepancies"]:
            assert inj["check"] in found, f"{sid}: {inj['check']} not caught"


def test_clean_shipments_have_no_reconciliation_findings(packs):
    for sid, ship in SHIPMENTS.items():
        if ship["clean"]:
            assert reconcile_pack(packs[sid], sid) == [], f"{sid} should be clean"


def test_declared_value_finding_detail(packs):
    findings = reconcile_pack(packs["MSKU-2847"], "MSKU-2847")
    dv = [f for f in findings if f.check == "declared_value_vs_invoice_total"]
    assert len(dv) == 1
    assert dv[0].severity == "CRITICAL"
    assert dv[0].dollar_impact == pytest.approx(18068.40, abs=1.0)
    assert len(dv[0].citations) == 2  # invoice + entry, each with doc + page


def test_findings_sorted_by_severity(packs):
    findings = reconcile_pack(packs["TCLU-5015"], "TCLU-5015")
    sevs = [f.severity for f in findings]
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    assert sevs == sorted(sevs, key=order.__getitem__)


def test_every_finding_carries_citations(packs):
    for sid in SHIPMENTS:
        for f in reconcile_pack(packs[sid], sid):
            assert f.citations, f"{sid}/{f.check} has no citations"
            for c in f.citations:
                assert c["doc_type"] and c["page"] >= 1


def test_weight_tolerance_does_not_flag_rounding():
    # identical weights across docs must not be flagged (2% tolerance)
    pack = extract_pack(DATA / "shipments" / "TRLU-2770")
    findings = reconcile_pack(pack, "TRLU-2770")
    assert not [f for f in findings if f.check == "gross_weight_consistency"]


# ---------------------------------------------------------------------------
# compliance
# ---------------------------------------------------------------------------

def test_isf_missing_manufacturer_fails(packs):
    items = run_compliance(packs["MSKU-2847"], "MSKU-2847")
    isf = [c for c in items if c.check == "isf_completeness"][0]
    assert isf.status == "FAIL"
    assert "Manufacturer" in isf.detail


def test_isf_complete_passes_on_clean_pack(packs):
    items = run_compliance(packs["TRLU-2770"], "TRLU-2770")
    isf = [c for c in items if c.check == "isf_completeness"][0]
    assert isf.status == "PASS"


def test_hts_mismatch_priced_as_duty_delta(packs):
    findings = reconcile_pack(packs["TCLU-5015"], "TCLU-5015")
    hts = [f for f in findings if f.check == "hts_consistency"][0]
    assert hts.severity == "CRITICAL"
    assert hts.dollar_impact > 0, "duty-rate delta must produce a non-zero impact"


def test_incoherent_hts_chapter_fails_coherence(packs):
    # cross-chapter incoherence: put an apparel HTS on the furniture invoice
    import copy
    pack = copy.deepcopy(packs["MSCU-5011"])
    pack["customs_entry"].fields["hts_code"].value = "6109.10.0012"
    items = run_compliance(pack, "MSCU-5011")
    coher = [c for c in items if c.check == "hts_description_coherence" and c.status == "FAIL"]
    assert coher, "apparel HTS on a furniture entry should fail coherence"


def test_coo_mismatch_flagged(packs):
    items = run_compliance(packs["CMAU-7913"], "CMAU-7913")
    coo = [c for c in items if c.check == "country_of_origin_consistency"][0]
    assert coo.status == "FAIL"


# ---------------------------------------------------------------------------
# RAG + citations
# ---------------------------------------------------------------------------

def test_structured_answer_cites_and_flags_mismatch(packs, col):
    ans = rag.answer_offline(
        "What's the declared value vs invoice total for shipment MSKU-2847?", col, packs)
    assert "MISMATCH" in ans.text
    assert len(ans.citations) == 2
    assert ans.confidence > 0.9


def test_retrieval_answer_carries_citations(packs, col):
    ans = rag.answer_offline("Which documents mention demurrage charges?", col, packs)
    assert ans.citations


def test_unanswerable_question_is_refused(packs, col):
    ans = rag.answer_offline("Qzxv blorp?", col, packs)
    assert ans.citations or "Not found" in ans.text


def test_index_filters_by_shipment(col):
    hits = indexer.search(col, "container number", top_k=3, shipment_id="MSKU-2847")
    assert hits and all(h["meta"]["shipment_id"] == "MSKU-2847" for h in hits)


# ---------------------------------------------------------------------------
# governance
# ---------------------------------------------------------------------------

def test_trace_log_appends(tmp_path):
    trace = TraceLog(tmp_path / "trace.jsonl")
    trace.log("test.action", foo="bar")
    trace.log("test.action2")
    lines = (tmp_path / "trace.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["action"] == "test.action"


def test_review_gate_defaults_to_pending(tmp_path):
    gate = ReviewGate(TraceLog(tmp_path / "t.jsonl"))
    assert gate.status == "PENDING_HUMAN_REVIEW"


def test_review_gate_requires_named_reviewer(tmp_path):
    gate = ReviewGate(TraceLog(tmp_path / "t.jsonl"))
    with pytest.raises(ValueError):
        gate.approve("")
    assert gate.approve("A. Denis") == "APPROVED_BY_HUMAN"
