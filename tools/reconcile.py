"""Cross-document reconciliation engine.

Compares the same real-world fact as it appears on different documents of a
shipment pack (container number on the B/L vs the arrival notice, invoice
total vs entered value on the customs entry, ...) and flags every mismatch
with a severity level, a dollar-impact estimate, and full citations
(document + page for each side of the comparison).

Deterministic by design: rules, tolerances and impact estimates live here,
not in a model. The Claude layer reasons *about* these findings; it never
replaces them.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

from tools.pdf_extract import ExtractedDoc

SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}

# duty-rate hints for the HTS codes used in the synthetic set, so an HTS
# mismatch can be priced as a duty-rate delta on the entered value
HTS_DUTY_HINTS = {
    "8517.62.0090": 0.000, "9401.61.4011": 0.000, "6109.10.0012": 0.165,
    "8708.30.5090": 0.025, "0901.11.0025": 0.000, "9503.00.0073": 0.000,
    "8413.70.2004": 0.000, "4015.12.1000": 0.030, "7323.93.0045": 0.020,
    "8541.43.0010": 0.000, "6104.62.2011": 0.149,
}

# Impact kinds — only arithmetic/valuation deltas carry a firm dollar figure;
# everything else is a potential-exposure estimate requiring broker review.
EXACT_DELTA = "exact_delta"            # recomputable from the documents themselves
DUTY_DELTA = "duty_delta"              # rate difference x entered value (rate basis shown)
POTENTIAL = "potential_exposure"       # rough estimate — broker review required

# flat potential-exposure estimates for non-valuation mismatches (README table)
FLAT_IMPACT = {
    "container_number_consistency": 1250.0,   # misrelease / detention risk
    "seal_number_consistency": 350.0,          # seal verification at terminal
    "vessel_consistency": 150.0,               # doc correction fee
    "consignee_consistency": 1800.0,           # entry amendment + release delay
    "gross_weight_consistency": 850.0,         # reweigh + chassis/rail refees
    "net_vs_gross_weight": 850.0,
    "package_count_consistency": 950.0,        # tally / possible shortage claim
    "isf_completeness": 5000.0,                # CBP ISF liquidated damages
    "coo_flat": 4500.0,                        # exam + origin verification
}


@dataclass
class Finding:
    check: str
    shipment_id: str
    severity: str
    field: str
    detail: str
    values: dict[str, Any]        # {"doc_type (p.N)": value, ...}
    citations: list[dict]          # [{doc_type, file, page}, ...]
    dollar_impact: float
    impact_basis: str
    impact_kind: str = POTENTIAL   # exact_delta | duty_delta | potential_exposure

    def to_dict(self) -> dict:
        return asdict(self)


def _cite(doc: ExtractedDoc, field: str) -> dict:
    f = doc.fields.get(field)
    return {"doc_type": doc.doc_type, "file": doc.file, "page": f.page if f else 1}


def _label(doc: ExtractedDoc, field: str) -> str:
    f = doc.fields.get(field)
    return f"{doc.doc_type} (p.{f.page if f else 1})"


class Reconciler:
    def __init__(self, pack: dict[str, ExtractedDoc], shipment_id: str, trace=None):
        self.pack = pack
        self.sid = shipment_id
        self.trace = trace
        self.findings: list[Finding] = []

    # -- helpers ------------------------------------------------------------

    def _docs_with(self, field: str, doc_types: list[str]) -> list[ExtractedDoc]:
        return [self.pack[d] for d in doc_types if d in self.pack and field in self.pack[d].fields]

    def _flag(self, check, severity, field, detail, docs_fields, impact, basis,
              kind: str = POTENTIAL):
        finding = Finding(
            check=check, shipment_id=self.sid, severity=severity, field=field, detail=detail,
            values={_label(d, fl): d.value(fl) for d, fl in docs_fields},
            citations=[_cite(d, fl) for d, fl in docs_fields],
            dollar_impact=round(impact, 2), impact_basis=basis, impact_kind=kind,
        )
        self.findings.append(finding)
        if self.trace:
            self.trace.log("reconcile.flag", shipment=self.sid, check=check,
                           severity=severity, detail=detail,
                           dollar_impact=finding.dollar_impact, impact_kind=kind)

    def _consistency(self, check, field, doc_types, severity, impact_key,
                     normalize=lambda v: v, tolerance: float | None = None):
        docs = self._docs_with(field, doc_types)
        if len(docs) < 2:
            if self.trace:
                self.trace.log("reconcile.pass", shipment=self.sid, check=check,
                               note="fewer than 2 documents carry this field")
            return
        ref = docs[0]
        ref_val = normalize(ref.value(field))
        mismatched = []
        for d in docs[1:]:
            v = normalize(d.value(field))
            if tolerance is not None:
                base = max(abs(float(ref_val)), 1e-9)
                if abs(float(v) - float(ref_val)) / base > tolerance:
                    mismatched.append(d)
            elif v != ref_val:
                mismatched.append(d)
        if mismatched:
            pairs = [(ref, field)] + [(d, field) for d in mismatched]
            self._flag(check, severity, field,
                       f"{field.replace('_', ' ')} differs across documents",
                       pairs, FLAT_IMPACT.get(impact_key, 500.0),
                       "potential exposure — rough estimate, broker review required (README impact table)")
        elif self.trace:
            self.trace.log("reconcile.pass", shipment=self.sid, check=check,
                           docs=[d.doc_type for d in docs])

    # -- checks -------------------------------------------------------------

    def check_container_number(self):
        self._consistency("container_number_consistency", "container_number",
                          ["bill_of_lading", "packing_list", "arrival_notice", "delivery_order"],
                          "HIGH", "container_number_consistency")

    def check_seal_number(self):
        self._consistency("seal_number_consistency", "seal_number",
                          ["bill_of_lading", "arrival_notice"], "LOW", "seal_number_consistency")

    def check_vessel(self):
        self._consistency("vessel_consistency", "vessel",
                          ["bill_of_lading", "customs_entry", "arrival_notice"],
                          "LOW", "vessel_consistency", normalize=lambda v: str(v).upper().strip())

    def check_bl_number(self):
        self._consistency("bl_number_consistency", "bl_number",
                          ["bill_of_lading", "isf_filing", "customs_entry",
                           "arrival_notice", "delivery_order"],
                          "HIGH", "container_number_consistency")

    def check_consignee(self):
        bl = self.pack.get("bill_of_lading")
        entry = self.pack.get("customs_entry")
        isf = self.pack.get("isf_filing")
        if not bl:
            return
        base = str(bl.value("consignee_name", "")).upper()
        for other, fld in ((entry, "importer_of_record"), (isf, "importer_of_record")):
            if other and fld in other.fields:
                if str(other.value(fld)).upper() != base:
                    self._flag("consignee_consistency", "HIGH", fld,
                               "importer of record does not match B/L consignee",
                               [(bl, "consignee_name"), (other, fld)],
                               FLAT_IMPACT["consignee_consistency"],
                               "entry amendment + release delay estimate")
                elif self.trace:
                    self.trace.log("reconcile.pass", shipment=self.sid,
                                   check="consignee_consistency", docs=[other.doc_type])

    def check_declared_value(self):
        inv = self.pack.get("commercial_invoice")
        entry = self.pack.get("customs_entry")
        if not (inv and entry and "invoice_total" in inv.fields and "declared_value" in entry.fields):
            return
        total = float(inv.value("invoice_total"))
        declared = float(entry.value("declared_value"))
        delta = round(total - declared, 2)
        if abs(delta) > 1.00:
            duty_rate = float(entry.value("duty_rate", 0.0) or 0.0)
            duty_note = (f"; duty at entry rate {duty_rate:.1%}: ${abs(delta) * duty_rate:,.2f}"
                         if duty_rate else "")
            self._flag("declared_value_vs_invoice_total", "CRITICAL", "declared_value",
                       f"entered value differs from commercial invoice total by ${delta:,.2f}",
                       [(inv, "invoice_total"), (entry, "declared_value")],
                       abs(delta), f"exact delta between entered value and invoice total{duty_note}",
                       kind=EXACT_DELTA)
        elif self.trace:
            self.trace.log("reconcile.pass", shipment=self.sid, check="declared_value_vs_invoice_total")

    def check_invoice_math(self):
        inv = self.pack.get("commercial_invoice")
        if not (inv and inv.line_items and "invoice_total" in inv.fields):
            return
        line_sum = round(sum(li["amount"] for li in inv.line_items), 2)
        stated = float(inv.value("invoice_total"))
        delta = round(stated - line_sum, 2)
        if abs(delta) > 0.01:
            self._flag("invoice_math", "MEDIUM", "invoice_total",
                       f"stated invoice total differs from sum of line items by ${delta:,.2f}",
                       [(inv, "invoice_total")], abs(delta),
                       "exact delta: stated total vs recomputed line-item sum", kind=EXACT_DELTA)
        elif self.trace:
            self.trace.log("reconcile.pass", shipment=self.sid, check="invoice_math")

    def check_hts(self):
        docs = self._docs_with("hts_code", ["commercial_invoice", "isf_filing", "customs_entry"])
        if len(docs) < 2:
            return
        codes = {d.value("hts_code") for d in docs}
        if len(codes) > 1:
            entry = self.pack.get("customs_entry")
            declared = float(entry.value("declared_value", 0.0)) if entry else 0.0
            rates = [HTS_DUTY_HINTS.get(c) for c in codes if HTS_DUTY_HINTS.get(c) is not None]
            if len(rates) >= 2 and declared:
                impact = abs(max(rates) - min(rates)) * declared
                basis = (f"duty-rate delta ({min(rates):.1%} vs {max(rates):.1%}, demo rate table) "
                         f"x entered value ${declared:,.2f} — verify against current HTSUS")
                kind = DUTY_DELTA
            else:
                impact, basis, kind = 2500.0, \
                    "reclassification + potential penalty exposure (rough estimate)", POTENTIAL
            self._flag("hts_consistency", "CRITICAL", "hts_code",
                       "HTS classification differs across documents",
                       [(d, "hts_code") for d in docs], impact, basis, kind=kind)
        elif self.trace:
            self.trace.log("reconcile.pass", shipment=self.sid, check="hts_consistency")

    def check_country_of_origin(self):
        docs = self._docs_with("country_of_origin",
                               ["commercial_invoice", "certificate_of_origin", "isf_filing", "customs_entry"])
        if len(docs) < 2:
            return
        vals = {str(d.value("country_of_origin")).strip().title() for d in docs}
        if len(vals) > 1:
            self._flag("country_of_origin_consistency", "CRITICAL", "country_of_origin",
                       "country of origin differs across documents",
                       [(d, "country_of_origin") for d in docs],
                       FLAT_IMPACT["coo_flat"], "exam + origin verification exposure (flat)")
        elif self.trace:
            self.trace.log("reconcile.pass", shipment=self.sid, check="country_of_origin_consistency")

    def check_gross_weight(self):
        self._consistency("gross_weight_consistency", "gross_weight_kg",
                          ["bill_of_lading", "packing_list", "arrival_notice"],
                          "MEDIUM", "gross_weight_consistency",
                          normalize=float, tolerance=0.02)

    def check_net_vs_gross(self):
        pl = self.pack.get("packing_list")
        if not (pl and "net_weight_kg" in pl.fields and "gross_weight_kg" in pl.fields):
            return
        net, gross = float(pl.value("net_weight_kg")), float(pl.value("gross_weight_kg"))
        if net > gross:
            self._flag("net_vs_gross_weight", "MEDIUM", "net_weight_kg",
                       f"net weight ({net:,.1f} kg) exceeds gross weight ({gross:,.1f} kg) on the same document",
                       [(pl, "net_weight_kg"), (pl, "gross_weight_kg")],
                       FLAT_IMPACT["net_vs_gross_weight"], "reweigh / document correction estimate")
        elif self.trace:
            self.trace.log("reconcile.pass", shipment=self.sid, check="net_vs_gross_weight")

    def check_package_count(self):
        self._consistency("package_count_consistency", "package_count",
                          ["bill_of_lading", "packing_list", "certificate_of_origin",
                           "arrival_notice", "delivery_order"],
                          "MEDIUM", "package_count_consistency", normalize=int)

    # -- run ----------------------------------------------------------------

    def run(self) -> list[Finding]:
        for check in (self.check_container_number, self.check_seal_number, self.check_vessel,
                      self.check_bl_number, self.check_consignee, self.check_declared_value,
                      self.check_invoice_math, self.check_hts, self.check_country_of_origin,
                      self.check_gross_weight, self.check_net_vs_gross, self.check_package_count):
            check()
        self.findings.sort(key=lambda f: SEVERITY_ORDER[f.severity])
        return self.findings


def reconcile_pack(pack: dict[str, ExtractedDoc], shipment_id: str, trace=None) -> list[Finding]:
    return Reconciler(pack, shipment_id, trace).run()
