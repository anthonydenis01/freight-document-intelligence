"""Compliance checklist: ISF completeness, HTS coherence, origin consistency.

Produces an audit-ready checklist per shipment. Deterministic rules with
citations; findings feed the same report and trace log as reconciliation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict

from tools.pdf_extract import ExtractedDoc

ISF_10_ELEMENTS = [
    ("importer_of_record", "Importer of record number/name"),
    ("consignee_name", "Consignee"),
    ("seller", "Seller"),
    ("buyer", "Buyer"),
    ("manufacturer_name", "Manufacturer (or supplier)"),
    ("ship_to", "Ship-to party"),
    ("country_of_origin", "Country of origin"),
    ("hts_code", "Commodity HTS number"),
    ("stuffing_location", "Container stuffing location"),
    ("consolidator", "Consolidator (stuffer)"),
]

HTS_FORMAT = re.compile(r"^\d{4}\.\d{2}\.\d{4}$")

# HTS chapter -> keywords we expect to see in the goods description.
# Used as a plausibility screen, not a classification engine.
HTS_CHAPTER_KEYWORDS = {
    "09": ["coffee", "tea", "spice"],
    "40": ["rubber", "glove", "nitrile", "latex"],
    "61": ["knit", "t-shirt", "shirt", "apparel", "cotton"],
    "73": ["steel", "cookware", "iron", "kitchen"],
    "84": ["pump", "machine", "engine", "compressor", "computer"],
    "85": ["router", "electronic", "solar", "panel", "network", "telephone", "semiconductor", "photovoltaic", "module"],
    "87": ["vehicle", "brake", "auto", "car", "chassis"],
    "94": ["furniture", "chair", "seat", "table", "lamp"],
    "95": ["toy", "game", "play"],
}


@dataclass
class ComplianceItem:
    check: str
    shipment_id: str
    status: str            # PASS | FAIL | WARN
    severity: str          # informational when PASS
    detail: str
    citations: list[dict]
    dollar_impact: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _cite(doc: ExtractedDoc, field: str | None = None) -> dict:
    page = doc.fields[field].page if field and field in doc.fields else 1
    return {"doc_type": doc.doc_type, "file": doc.file, "page": page}


def run_compliance(pack: dict[str, ExtractedDoc], shipment_id: str, trace=None) -> list[ComplianceItem]:
    items: list[ComplianceItem] = []

    def add(check, status, severity, detail, citations, impact=0.0):
        items.append(ComplianceItem(check, shipment_id, status, severity, detail, citations, impact))
        if trace:
            trace.log("compliance." + ("fail" if status == "FAIL" else "pass"),
                      shipment=shipment_id, check=check, detail=detail)

    # 1. ISF completeness --------------------------------------------------
    isf = pack.get("isf_filing")
    if isf is None:
        add("isf_completeness", "FAIL", "HIGH", "No ISF filing found in the document pack", [], 5000.0)
    else:
        missing = [label for fld, label in ISF_10_ELEMENTS
                   if not str(isf.value(fld, "") or "").strip()]
        if missing:
            add("isf_completeness", "FAIL", "HIGH",
                f"ISF-10 element(s) missing: {', '.join(missing)}",
                [_cite(isf)], 5000.0)
        else:
            add("isf_completeness", "PASS", "INFO",
                "All 10 ISF importer data elements present", [_cite(isf)])

    # 2. HTS format + description coherence --------------------------------
    for doc_type in ("commercial_invoice", "customs_entry"):
        doc = pack.get(doc_type)
        if not doc or "hts_code" not in doc.fields:
            continue
        code = str(doc.value("hts_code"))
        if not HTS_FORMAT.match(code):
            add("hts_format", "FAIL", "MEDIUM",
                f"HTS number '{code}' on {doc_type} is not a valid 10-digit format",
                [_cite(doc, "hts_code")], 500.0)
            continue
        chapter = code[:2]
        desc = str(doc.value("goods_description", "")).lower()
        keywords = HTS_CHAPTER_KEYWORDS.get(chapter)
        if desc and keywords and not any(k in desc for k in keywords):
            add("hts_description_coherence", "FAIL", "HIGH",
                f"HTS chapter {chapter} is implausible for goods described as "
                f"'{doc.value('goods_description')}' on {doc_type}",
                [_cite(doc, "hts_code")], 2500.0)
        else:
            add("hts_description_coherence", "PASS", "INFO",
                f"HTS {code} coherent with goods description on {doc_type}",
                [_cite(doc, "hts_code")])

    # 3. Country-of-origin consistency -------------------------------------
    coo_docs = [(d, pack[d]) for d in
                ("commercial_invoice", "certificate_of_origin", "isf_filing", "customs_entry")
                if d in pack and "country_of_origin" in pack[d].fields]
    if len(coo_docs) >= 2:
        vals = {str(doc.value("country_of_origin")).strip().title() for _, doc in coo_docs}
        if len(vals) > 1:
            add("country_of_origin_consistency", "FAIL", "CRITICAL",
                f"Country of origin inconsistent across documents: {', '.join(sorted(vals))}",
                [_cite(doc, "country_of_origin") for _, doc in coo_docs], 4500.0)
        else:
            add("country_of_origin_consistency", "PASS", "INFO",
                f"Country of origin consistent ({vals.pop()}) across {len(coo_docs)} documents",
                [_cite(doc, "country_of_origin") for _, doc in coo_docs])

    # 4. Entry present & drafted -------------------------------------------
    entry = pack.get("customs_entry")
    if entry is None:
        add("entry_prepared", "WARN", "MEDIUM", "No draft customs entry found in pack", [])
    else:
        add("entry_prepared", "PASS", "INFO",
            f"Draft entry {entry.value('entry_number')} present (not filed)", [_cite(entry)])

    return items
