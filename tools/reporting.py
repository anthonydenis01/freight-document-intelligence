"""Audit report assembly + terminal rendering."""
from __future__ import annotations

from typing import Any

from tools.compliance import ComplianceItem
from tools.reconcile import Finding

SEV_ICON = {"CRITICAL": "✖", "HIGH": "⚠", "MEDIUM": "⚠", "LOW": "·", "INFO": "✓"}


def build_report(shipment_id: str, findings: list[Finding], compliance: list[ComplianceItem],
                 status: str, run_id: str) -> dict[str, Any]:
    fails = [c for c in compliance if c.status == "FAIL"]
    total_impact = round(sum(f.dollar_impact for f in findings) +
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
        "estimated_exposure_usd": total_impact,
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
        print(f"  {SEV_ICON[f['severity']]} [{f['severity']}] {f['detail']}")
        print(f"      values: " + " | ".join(f"{k}: {v}" for k, v in f["values"].items()))
        print(f"      est. impact: ${f['dollar_impact']:,.2f} ({f['impact_basis']})")
        print(f"      sources: {cites}")
    print(f"Compliance: {comp['failed']} failed item(s)")
    for c in comp["items"]:
        if c["status"] != "PASS":
            print(f"  {SEV_ICON.get(c['severity'], '·')} [{c['status']}] {c['detail']}")
    print(f"Estimated exposure: ${report['estimated_exposure_usd']:,.2f}")
    print(f"Recommendation: {report['filing_recommendation']}")
    print(f"Governance status: {report['status']}  (run {report['run_id']})")
