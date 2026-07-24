"""Follow-up action engine — every finding proposes its resolution.

For each reconciliation finding / failed compliance item, this module
generates the concrete next step a desk operator would take: who to
contact (shipper, customs broker, carrier, forwarder, terminal), what to
ask for, and a ready-to-send draft email that quotes the exact values and
cites the source documents.

Governance, same rules as everything else in this repo:
- The engine only ever PROPOSES. Approval requires a named human reviewer
  (one click / one command).
- Nothing is actually emailed — "send" is simulated and produces a
  dispatch record + the draft to copy into your own mail client. The
  agent has no access to any mailbox by design.
- Every transition (proposed, approved, simulated send) is appended to
  trace.jsonl.

State machine:  PROPOSED → APPROVED (named reviewer) → SENT_SIMULATED
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from tools.compliance import ComplianceItem
from tools.pdf_extract import ExtractedDoc
from tools.reconcile import Finding

PROPOSED = "PROPOSED"
APPROVED = "APPROVED"
SENT = "SENT_SIMULATED"

STATE_FILE = Path("reports/actions_state.json")

# Synthetic contact book (mirrors the generator's fictitious parties).
BROKER = ("Panamerican Customs Brokers", "entryteam@panamerican-cb.example")
FORWARDER = ("Meridian Global Forwarding", "isf@meridianglobal.example")
CARRIER_OPS = ("Carrier documentation desk", "docdesk@carrier.example")
TERMINAL = ("POMTOC terminal - Miami", "gateops@pomtoc.example")


def _shipper_contact(pack: dict[str, ExtractedDoc]) -> tuple[str, str]:
    name = None
    for dt in ("commercial_invoice", "bill_of_lading"):
        if dt in pack and "shipper_name" in pack[dt].fields:
            name = str(pack[dt].value("shipper_name"))
            break
    slug = (name or "shipper").split()[0].lower()
    return (name or "Shipper", f"export.docs@{slug}.example")


# check -> (recipient resolver, request line, action verb for the summary)
PLAYBOOK = {
    "declared_value_vs_invoice_total": (
        lambda p: BROKER,
        "hold the entry and align the entered value with the commercial invoice total "
        "(or obtain a corrected invoice from the shipper if the invoice is wrong)",
        "Hold entry & reconcile declared value"),
    "invoice_math": (
        _shipper_contact,
        "issue a corrected commercial invoice whose line items sum to the stated total",
        "Request corrected commercial invoice"),
    "hts_consistency": (
        lambda p: BROKER,
        "confirm the correct HTS classification and amend the draft entry before filing",
        "Confirm HTS classification with broker"),
    "country_of_origin_consistency": (
        _shipper_contact,
        "confirm the true country of origin and reissue the certificate of origin "
        "(or corrected invoice) so all documents agree",
        "Resolve country-of-origin conflict"),
    "consignee_consistency": (
        lambda p: BROKER,
        "correct the importer-of-record on the draft entry to match the B/L consignee",
        "Correct importer of record on entry"),
    "container_number_consistency": (
        lambda p: CARRIER_OPS,
        "reissue the arrival notice with the container number matching the B/L",
        "Request corrected arrival notice (container no.)"),
    "bl_number_consistency": (
        lambda p: CARRIER_OPS,
        "confirm the master B/L number and reissue the mismatched document",
        "Confirm master B/L number"),
    "seal_number_consistency": (
        lambda p: TERMINAL,
        "verify the physical seal on arrival and report the seal number found",
        "Verify seal at terminal"),
    "vessel_consistency": (
        lambda p: CARRIER_OPS,
        "confirm the importing vessel name and correct the arrival notice",
        "Confirm vessel name with carrier"),
    "gross_weight_consistency": (
        _shipper_contact,
        "confirm the actual gross weight (reweigh if needed) and reissue the packing list",
        "Reconcile gross weight / request reweigh"),
    "net_vs_gross_weight": (
        _shipper_contact,
        "correct the packing list - net weight currently exceeds gross weight",
        "Request corrected packing list (net > gross)"),
    "package_count_consistency": (
        _shipper_contact,
        "confirm the shipped carton count and reissue the packing list",
        "Reconcile package count"),
    "isf_completeness": (
        lambda p: FORWARDER,
        "complete and re-transmit the ISF with the missing element(s) immediately "
        "to limit liquidated-damages exposure",
        "Complete & re-file ISF"),
    "hts_description_coherence": (
        lambda p: BROKER,
        "review the HTS classification against the goods description before filing",
        "Review HTS vs goods description"),
}


@dataclass
class FollowUpAction:
    id: str
    shipment_id: str
    check: str
    severity: str
    summary: str
    recipient_name: str
    recipient_email: str
    request: str
    draft_email: str
    citations: list[dict] = field(default_factory=list)
    status: str = PROPOSED
    approved_by: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _draft(sid: str, severity: str, detail: str, values: dict, request: str,
           recipient_name: str, citations: list[dict]) -> str:
    cites = ", ".join(f"{c['doc_type']} p.{c['page']}" for c in citations) or "audit report"
    vals = "\n".join(f"  - {k}: {v}" for k, v in values.items())
    return (
        f"To: {recipient_name}\n"
        f"Subject: [{severity}] Shipment {sid} - document discrepancy, action required\n\n"
        f"Hello,\n\n"
        f"Our pre-filing document audit for shipment {sid} flagged the following:\n\n"
        f"  {detail}\n\n"
        f"Values as extracted from the documents:\n{vals if vals else '  (see audit report)'}\n\n"
        f"Requested action: please {request}.\n\n"
        f"Source documents: {cites}.\n"
        f"This shipment is on HOLD on our side until resolved.\n\n"
        f"Thank you,\n"
        f"Import operations desk\n"
        f"-- generated by freight-document-intelligence (synthetic demo); "
        f"reviewed and released by a named human before any send --"
    )


def build_actions(pack: dict[str, ExtractedDoc], shipment_id: str,
                  findings: list[Finding], compliance: list[ComplianceItem],
                  trace=None) -> list[FollowUpAction]:
    actions: list[FollowUpAction] = []
    seen: set[str] = set()

    def add(check, severity, detail, values, citations):
        if check not in PLAYBOOK or check in seen:
            return
        seen.add(check)
        resolver, request, summary = PLAYBOOK[check]
        name, email = resolver(pack)
        action = FollowUpAction(
            id=f"{shipment_id}:{check}",
            shipment_id=shipment_id, check=check, severity=severity, summary=summary,
            recipient_name=name, recipient_email=email, request=request,
            draft_email=_draft(shipment_id, severity, detail, values, request, name, citations),
            citations=citations,
        )
        state = _load_state().get(action.id)
        if state:
            action.status = state["status"]
            action.approved_by = state.get("approved_by")
        actions.append(action)
        if trace:
            trace.log("action.proposed", shipment=shipment_id, check=check,
                      recipient=email, status=action.status)

    for f in findings:
        add(f.check, f.severity, f.detail, f.values, f.citations)
    for c in compliance:
        if c.status == "FAIL":
            add(c.check, c.severity, c.detail, {}, c.citations)
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    actions.sort(key=lambda a: order.get(a.severity, 9))
    return actions


# ---------------------------------------------------------------------------
# state persistence + transitions
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def approve_action(action_id: str, reviewer: str, trace=None) -> str:
    reviewer = (reviewer or "").strip()
    if not reviewer:
        raise ValueError("A named human reviewer is required to approve an action.")
    state = _load_state()
    state[action_id] = {"status": APPROVED, "approved_by": reviewer}
    _save_state(state)
    if trace:
        trace.log("action.approved", action_id=action_id, reviewer=reviewer)
    return APPROVED


def send_action(action_id: str, trace=None) -> str:
    """Simulated dispatch — requires prior human approval, sends nothing."""
    state = _load_state()
    entry = state.get(action_id)
    if not entry or entry["status"] not in (APPROVED, SENT):
        raise PermissionError("Action must be approved by a named reviewer before dispatch.")
    entry["status"] = SENT
    _save_state(state)
    if trace:
        trace.log("action.sent_simulated", action_id=action_id,
                  note="no real email sent - draft available for copy")
    return SENT
