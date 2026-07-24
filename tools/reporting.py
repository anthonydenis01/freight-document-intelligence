"""Audit report assembly + terminal rendering.

Dollar figures are reported in two separate buckets, on purpose:
- quantified deltas (exact arithmetic / declared-value / duty-rate deltas,
  recomputable from the documents themselves)
- potential exposure (rough estimates that require broker review)
They are never summed into one misleading number.
"""
from __future__ import annotations

from typing import Any

from tools.compliance import ComplianceItem
from tools.reconcile import Finding, POTENTIAL

SEV_ICON = {"CRITICAL": "✖", "HIGH": "⚠", "MEDIUM": "⚠", "LOW": "·", "INFO": "✓"}
KIND_LABEL = {"exact_delta": "exact delta", "duty_delta": "duty-rate delta",
              "potential_exposure": "potential exposure"}


def build_report(shipment_id: str, findings: list[Finding], compliance: list[ComplianceItem],
                 status: str, run_id: str, actions: list | None = None) -> dict[str, Any]:
    fails = [c for c in compliance if c.status == "FAIL"]
    quantified = round(sum(f.dollar_impact for f in findings
                           if f.impact_kind != POTENTIAL), 2)
    potential = round(sum(f.dollar_impact for f in findings
                          if f.impact_kind == POTENTIAL) +
                      sum(c.dollar_impact for c in fails), 2)
    return {
        "shipment_id": shipment_id,
        "run_id": run_id,
        "status": status,
        "reconciliation": {
            "checks_run": 12,
            "mismatches": len(findings),
            "findings": [f.to_dict() for f in findings],
        },
        "compliance": {
            "items": [c.to_dict() for c in compliance],
            "failed": len(fails),
        },
        "follow_up_actions": [a.to_dict() for a in (actions or [])],
        "quantified_delta_usd": quantified,
        "potential_exposure_usd": potential,
        "filing_recommendation": ("HOLD — resolve findings before filing"
                                  if findings or fails else "CLEAR TO PROPOSE FOR FILING"),
    }


def print_report(report: dict[str, Any]) -> None:
    sid = report["shipment_id"]
    rec, comp = report["reconciliation"], report["compliance"]
    print(f"\n===== SHIPMENT {sid} — DOCUMENT AUDIT =====")
    print(f"Reconciliation: {rec['checks_run']} checks · {rec['mismatches']} mismatch(es)")
    for f in rec["findings"]:
        cites = ", ".join(f"{c['doc_type']} p.{c['page']}" for c in f["citations"])
        kind = KIND_LABEL.get(f.get("impact_kind", "potential_exposure"), "potential exposure")
        print(f"  {SEV_ICON[f['severity']]} [{f['severity']}] {f['detail']}")
        print(f"      values: " + " | ".join(f"{k}: {v}" for k, v in f["values"].items()))
        print(f"      {kind}: ${f['dollar_impact']:,.2f} ({f['impact_basis']})")
        print(f"      sources: {cites}")
    print(f"Compliance: {comp['failed']} failed item(s)")
    for c in comp["items"]:
        if c["status"] != "PASS":
            print(f"  {SEV_ICON.get(c['severity'], '·')} [{c['status']}] {c['detail']}")
    acts = report.get("follow_up_actions", [])
    if acts:
        print(f"Follow-up actions ({len(acts)}) — approve with: "
              f"freight_doc_agent.py actions approve --id <ID> --reviewer \"Name\"")
        for a in acts:
            print(f"  → [{a['status']}] {a['summary']}")
            print(f"      to: {a['recipient_name']} <{a['recipient_email']}>   id: {a['id']}")
    print(f"Quantified deltas: ${report['quantified_delta_usd']:,.2f}   "
          f"Potential exposure (est.): ${report['potential_exposure_usd']:,.2f}")
    print(f"Recommendation: {report['filing_recommendation']}")
    print(f"Governance status: {report['status']}  (run {report['run_id']})")
