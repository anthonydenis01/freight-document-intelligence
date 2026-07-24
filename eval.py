#!/usr/bin/env python3
"""Evaluation harness — measures the repo's success criteria against ground truth.

Criteria (from the project spec):
  1. 95%+ field-extraction accuracy on the synthetic set
  2. 100% of injected discrepancies caught by the reconciliation + compliance pass
  3. Every RAG answer carries a source citation
  4. End-to-end < 60s for one shipment pack

Usage:  python eval.py
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

from tools import indexer, rag
from tools.compliance import run_compliance
from tools.governance import TraceLog
from tools.pdf_extract import extract_pack
from tools.reconcile import reconcile_pack

DATA = Path("data")

EVAL_QUESTIONS = [
    "What's the declared value vs invoice total for shipment MSKU-2847?",
    "Which container number is on the bill of lading for TCLU-5015?",
    "What is the country of origin for CMAU-7913?",
    "What is the gross weight for shipment HLXU-2347?",
    "Who is the consignee for OOLU-8571?",
    "What vessel is carrying shipment TGHU-7401?",
    "What is the HTS code for MSCU-5011?",
    "What are the terminal handling charges on the arrival notice for APZU-8397?",
    "What is the seal number for FCIU-8271?",
    "What is the estimated duty for TRLU-2770?",
]


def norm(v):
    if isinstance(v, float):
        return round(v, 2)
    if isinstance(v, str):
        return " ".join(v.split()).strip().lower()
    return v


def values_equal(gt, ex):
    if isinstance(gt, (int, float)) and isinstance(ex, (int, float)):
        if isinstance(gt, float) or isinstance(ex, float):
            return math.isclose(float(gt), float(ex), rel_tol=1e-4, abs_tol=0.01)
        return int(gt) == int(ex)
    return norm(str(gt)) == norm(str(ex))


def main() -> None:
    gt = json.loads((DATA / "ground_truth.json").read_text())
    trace = TraceLog("eval_trace.jsonl")

    # --- 1. extraction accuracy -------------------------------------------
    total = correct = 0
    misses = []
    packs = {}
    for ship in gt["shipments"]:
        sid = ship["shipment_id"]
        pack = extract_pack(DATA / "shipments" / sid)
        packs[sid] = pack
        for doc_type, doc_gt in ship["documents"].items():
            ex = pack.get(doc_type)
            for fname, gt_val in doc_gt["fields"].items():
                if fname == "duty_rate":
                    gt_val = round(float(gt_val), 4)
                total += 1
                ex_val = ex.value(fname) if ex else None
                if ex is not None and fname in ex.fields and values_equal(gt_val, ex_val):
                    correct += 1
                else:
                    misses.append((sid, doc_type, fname, gt_val, ex_val))
    accuracy = 100.0 * correct / total

    # --- 2. discrepancy recall --------------------------------------------
    injected = [(s["shipment_id"], d["check"]) for s in gt["shipments"]
                for d in s["injected_discrepancies"]]
    caught = set()
    false_positives = []
    for ship in gt["shipments"]:
        sid = ship["shipment_id"]
        found_checks = {f.check for f in reconcile_pack(packs[sid], sid)}
        found_checks |= {c.check for c in run_compliance(packs[sid], sid) if c.status == "FAIL"}
        expected = {chk for s2, chk in injected if s2 == sid}
        for chk in expected & found_checks:
            caught.add((sid, chk))
        # Legitimate cascades: a wrong HTS is also incoherent with the goods
        # description; a wrong invoice total also mismatches the entered value.
        cascades = {"hts_description_coherence"}
        if "invoice_math" in expected:
            cascades.add("declared_value_vs_invoice_total")
        for chk in found_checks - expected - cascades:
            false_positives.append((sid, chk))
    recall = 100.0 * len(caught) / len(injected)

    # --- 3. citation coverage + 4. latency --------------------------------
    docs = [d for pack in packs.values() for d in pack.values()]
    col = indexer.build_index(docs, persist_dir=None)
    cited = 0
    latencies = []
    for q in EVAL_QUESTIONS:
        t0 = time.time()
        ans = rag.answer_offline(q, col, packs, trace=trace)
        latencies.append(time.time() - t0)
        if ans.citations:
            cited += 1
    avg_latency = sum(latencies) / len(latencies)

    t0 = time.time()
    pack = extract_pack(DATA / "shipments" / "MSKU-2847")
    _ = indexer.build_index(list(pack.values()), persist_dir=None)
    _ = reconcile_pack(pack, "MSKU-2847")
    _ = run_compliance(pack, "MSKU-2847")
    _ = rag.answer_offline(EVAL_QUESTIONS[0], col, packs)
    single_pack = time.time() - t0

    # --- report ------------------------------------------------------------
    print("===== FREIGHT DOCUMENT INTELLIGENCE — EVALUATION =====")
    print(f"1. Field extraction accuracy : {accuracy:.1f}%  ({correct}/{total} fields)  "
          f"[target ≥ 95%] {'PASS' if accuracy >= 95 else 'FAIL'}")
    if misses:
        for m in misses[:10]:
            print(f"     miss: {m}")
    print(f"2. Injected discrepancies    : {len(caught)}/{len(injected)} caught "
          f"({recall:.0f}%)  [target 100%] {'PASS' if recall == 100 else 'FAIL'}")
    if false_positives:
        print(f"     unexpected extra flags: {false_positives}")
    print(f"3. RAG answers with citation : {cited}/{len(EVAL_QUESTIONS)}  "
          f"[target 100%] {'PASS' if cited == len(EVAL_QUESTIONS) else 'FAIL'}")
    print(f"4. One-pack end-to-end       : {single_pack:.2f}s  "
          f"[target < 60s] {'PASS' if single_pack < 60 else 'FAIL'}")
    print(f"   Avg answer latency        : {avg_latency * 1000:.0f} ms (offline mode)")

    Path("eval_results.json").write_text(json.dumps({
        "extraction_accuracy_pct": round(accuracy, 1),
        "fields_total": total, "fields_correct": correct,
        "discrepancies_injected": len(injected), "discrepancies_caught": len(caught),
        "citation_coverage": f"{cited}/{len(EVAL_QUESTIONS)}",
        "single_pack_seconds": round(single_pack, 2),
        "avg_answer_latency_ms": round(avg_latency * 1000),
        "false_positives": false_positives,
    }, indent=2))
    print("Saved: eval_results.json")


if __name__ == "__main__":
    main()
