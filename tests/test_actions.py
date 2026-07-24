"""Tests for the follow-up action engine (fully offline)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import actions as act
from tools.actions import APPROVED, PROPOSED, SENT, approve_action, build_actions, send_action
from tools.compliance import run_compliance
from tools.pdf_extract import extract_pack
from tools.reconcile import reconcile_pack

DATA = Path(__file__).parent.parent / "data"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(act, "STATE_FILE", tmp_path / "actions_state.json")


def _actions_for(sid: str):
    pack = extract_pack(DATA / "shipments" / sid)
    findings = reconcile_pack(pack, sid)
    compliance = run_compliance(pack, sid)
    return build_actions(pack, sid, findings, compliance)


def test_msku_pack_proposes_value_and_isf_actions():
    acts = _actions_for("MSKU-2847")
    checks = {a.check for a in acts}
    assert checks == {"declared_value_vs_invoice_total", "isf_completeness"}
    assert all(a.status == PROPOSED for a in acts)
    # CRITICAL first
    assert acts[0].check == "declared_value_vs_invoice_total"


def test_clean_shipment_has_no_actions():
    assert _actions_for("TRLU-2770") == []


def test_draft_email_quotes_values_and_cites_sources():
    acts = _actions_for("MSKU-2847")
    value_action = next(a for a in acts if a.check == "declared_value_vs_invoice_total")
    d = value_action.draft_email
    assert "MSKU-2847" in d
    assert "46,800" in d.replace("46800.0", "46,800") or "46800" in d
    assert "customs_entry p.1" in d
    assert "HOLD" in d
    assert value_action.recipient_email.endswith(".example")  # synthetic contacts only


def test_recipient_routing_matches_playbook():
    acts = {a.check: a for a in _actions_for("CMAU-7913")}
    # origin conflict goes to the shipper; container typo goes to the carrier desk
    assert "export.docs@" in acts["country_of_origin_consistency"].recipient_email
    assert acts["container_number_consistency"].recipient_name == "Carrier documentation desk"


def test_approve_requires_named_reviewer():
    with pytest.raises(ValueError):
        approve_action("MSKU-2847:isf_completeness", "   ")


def test_send_requires_prior_approval():
    with pytest.raises(PermissionError):
        send_action("MSKU-2847:isf_completeness")
    assert approve_action("MSKU-2847:isf_completeness", "A. Denis") == APPROVED
    assert send_action("MSKU-2847:isf_completeness") == SENT


def test_state_survives_rebuild():
    approve_action("MSKU-2847:declared_value_vs_invoice_total", "A. Denis")
    acts = {a.check: a for a in _actions_for("MSKU-2847")}
    assert acts["declared_value_vs_invoice_total"].status == APPROVED
    assert acts["declared_value_vs_invoice_total"].approved_by == "A. Denis"
    assert acts["isf_completeness"].status == PROPOSED


def test_report_embeds_actions():
    from tools.governance import ReviewGate, TraceLog
    from tools.reporting import build_report
    sid = "MSKU-2847"
    pack = extract_pack(DATA / "shipments" / sid)
    findings = reconcile_pack(pack, sid)
    compliance = run_compliance(pack, sid)
    acts = build_actions(pack, sid, findings, compliance)
    rep = build_report(sid, findings, compliance, "PENDING_HUMAN_REVIEW", "run", actions=acts)
    assert len(rep["follow_up_actions"]) == 2
    json.dumps(rep)  # must be JSON-serializable
