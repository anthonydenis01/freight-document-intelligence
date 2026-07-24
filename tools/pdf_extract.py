"""PDF text + structured field extraction for freight document packs.

Uses pdfplumber to pull page text, detects the document type from its title
line, then parses labelled fields ("Container No: MSKU2847614") into typed
values via per-document label maps. Every extracted field records the page
it came from and a confidence score, so downstream answers can cite
document + page.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pdfplumber

DOC_TYPE_TITLES = {
    "BILL OF LADING": "bill_of_lading",
    "COMMERCIAL INVOICE": "commercial_invoice",
    "PACKING LIST": "packing_list",
    "CERTIFICATE OF ORIGIN": "certificate_of_origin",
    "IMPORTER SECURITY FILING": "isf_filing",
    "ENTRY SUMMARY": "customs_entry",
    "ARRIVAL NOTICE": "arrival_notice",
    "DELIVERY ORDER": "delivery_order",
}

# label on the PDF  ->  (canonical field, parser)
def _text(v: str) -> str:
    return v.strip()

def _money(v: str) -> float:
    m = re.search(r"([\d,]+\.?\d*)", v)
    return float(m.group(1).replace(",", "")) if m else float("nan")

def _weight(v: str) -> float:
    return _money(v)

def _intval(v: str) -> int:
    m = re.search(r"([\d,]+)", v)
    return int(m.group(1).replace(",", "")) if m else -1

def _pct(v: str) -> float:
    return round(_money(v) / 100.0, 4)

def _dash_blank(v: str) -> str:
    v = v.strip()
    return "" if v == "-" else v


LABEL_MAPS: dict[str, dict[str, tuple[str, Any]]] = {
    "bill_of_lading": {
        "B/L Number": ("bl_number", _text),
        "Shipper": ("shipper_name", _text),
        "Shipper Address": ("shipper_address", _text),
        "Consignee": ("consignee_name", _text),
        "Consignee Address": ("consignee_address", _text),
        "Notify Party": ("notify_party", _text),
        "Ocean Vessel": ("vessel", _text),
        "Voyage No": ("voyage", _text),
        "Port of Loading": ("port_of_loading", _text),
        "Port of Discharge": ("port_of_discharge", _text),
        "Shipped on Board": ("etd", _text),
        "Container No": ("container_number", _text),
        "Seal No": ("seal_number", _text),
        "No of Packages": ("package_count", _intval),
        "Gross Weight": ("gross_weight_kg", _weight),
        "Measurement": ("volume_cbm", _money),
        "Description of Goods": ("goods_description", _text),
    },
    "commercial_invoice": {
        "Invoice Number": ("invoice_number", _text),
        "Seller": ("shipper_name", _text),
        "Seller Address": ("shipper_address", _text),
        "Buyer": ("consignee_name", _text),
        "Buyer Address": ("consignee_address", _text),
        "Incoterm": ("incoterm", _text),
        "Currency": ("currency", _text),
        "Country of Origin": ("country_of_origin", _text),
        "HTS Code": ("hts_code", _text),
        "Invoice Total": ("invoice_total", _money),
    },
    "packing_list": {
        "Invoice Number": ("invoice_number", _text),
        "Container No": ("container_number", _text),
        "Total Packages": ("package_count", _intval),
        "Gross Weight": ("gross_weight_kg", _weight),
        "Net Weight": ("net_weight_kg", _weight),
        "Total Volume": ("volume_cbm", _money),
        "Goods": ("goods_description", _text),
    },
    "certificate_of_origin": {
        "Exporter": ("shipper_name", _text),
        "Exporter Address": ("shipper_address", _text),
        "Consignee": ("consignee_name", _text),
        "Consignee Address": ("consignee_address", _text),
        "Invoice Reference": ("invoice_number", _text),
        "Packages": ("package_count", _intval),
        "Goods": ("goods_description", _text),
        "Country of Origin": ("country_of_origin", _text),
    },
    "isf_filing": {
        "ISF Transaction No": ("isf_transaction", _text),
        "Master B/L": ("bl_number", _text),
        "Bond Type": ("bond_type", _text),
        "Importer of Record": ("importer_of_record", _text),
        "Consignee": ("consignee_name", _text),
        "Seller": ("seller", _text),
        "Buyer": ("buyer", _text),
        "Manufacturer": ("manufacturer_name", _dash_blank),
        "Manufacturer Address": ("manufacturer_address", _text),
        "Ship To Party": ("ship_to", _text),
        "Country of Origin": ("country_of_origin", _text),
        "HTS Number": ("hts_code", _text),
        "Container Stuffing Location": ("stuffing_location", _text),
        "Consolidator": ("consolidator", _text),
    },
    "customs_entry": {
        "Entry Number": ("entry_number", _text),
        "Importer of Record": ("importer_of_record", _text),
        "Master B/L": ("bl_number", _text),
        "Importing Vessel": ("vessel", _text),
        "Port of Entry": ("port_of_discharge", _text),
        "Import Date": ("eta", _text),
        "HTS Number": ("hts_code", _text),
        "Description": ("goods_description", _text),
        "Country of Origin": ("country_of_origin", _text),
        "Entered Value": ("declared_value", _money),
        "Duty Rate": ("duty_rate", _pct),
        "Estimated Duty": ("estimated_duty", _money),
    },
    "arrival_notice": {
        "Master B/L": ("bl_number", _text),
        "Container No": ("container_number", _text),
        "Seal No": ("seal_number", _text),
        "Vessel": ("vessel", _text),
        "Voyage": ("voyage", _text),
        "ETA": ("eta", _text),
        "Port of Discharge": ("port_of_discharge", _text),
        "FIRMS Code": ("firms_code", _text),
        "IT Number": ("it_number", _text),
        "Consignee": ("consignee_name", _text),
        "Packages": ("package_count", _intval),
        "Gross Weight": ("gross_weight_kg", _weight),
        "Ocean Freight": ("ocean_freight", _money),
        "Terminal Handling": ("terminal_handling", _money),
    },
    "delivery_order": {
        "Master B/L": ("bl_number", _text),
        "Container No": ("container_number", _text),
        "Deliver To": ("consignee_name", _text),
        "Delivery Address": ("consignee_address", _text),
        "FIRMS Code": ("firms_code", _text),
        "IT Number": ("it_number", _text),
        "Packages": ("package_count", _intval),
    },
}

_LINE_ITEM_RE = re.compile(
    r"Qty\s+([\d,]+)\s+x\s+Unit Price\s+([\d,]+\.\d{2})\s+=\s+Amount\s+([\d,]+\.\d{2})"
)


@dataclass
class ExtractedField:
    name: str
    value: Any
    raw: str
    page: int
    confidence: float


@dataclass
class ExtractedDoc:
    file: str
    doc_type: str
    shipment_id: str
    fields: dict[str, ExtractedField] = field(default_factory=dict)
    line_items: list[dict] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)

    def value(self, name: str, default=None):
        f = self.fields.get(name)
        return f.value if f else default


def detect_doc_type(first_page_text: str) -> str | None:
    head = first_page_text.strip().splitlines()[0].upper() if first_page_text.strip() else ""
    for title, doc_type in DOC_TYPE_TITLES.items():
        if title in head:
            return doc_type
    return None


def detect_shipment_ref(first_page_text: str) -> str:
    m = re.search(r"REF\s+([A-Z]{4}-\d{4})", first_page_text)
    return m.group(1) if m else "UNKNOWN"


def extract_document(path: str | Path) -> ExtractedDoc:
    path = Path(path)
    with pdfplumber.open(path) as pdf:
        pages = [p.extract_text() or "" for p in pdf.pages]

    doc_type = detect_doc_type(pages[0]) or "unknown"
    shipment_id = detect_shipment_ref(pages[0])
    doc = ExtractedDoc(file=str(path), doc_type=doc_type, shipment_id=shipment_id, pages=pages)
    label_map = LABEL_MAPS.get(doc_type, {})

    for page_no, text in enumerate(pages, start=1):
        for line in text.splitlines():
            if ":" not in line:
                continue
            label, _, raw_value = line.partition(":")
            label = label.strip()
            raw_value = raw_value.strip()
            if label in label_map:
                fname, parser = label_map[label]
                try:
                    value = parser(raw_value)
                    conf = 0.99 if raw_value else 0.90
                    ok = value == value if isinstance(value, float) else True  # NaN guard
                    if isinstance(value, int) and value == -1:
                        ok = False
                    if not ok:
                        value, conf = raw_value, 0.40
                except Exception:
                    value, conf = raw_value, 0.40
                # first occurrence wins (labels are unique per doc)
                if fname not in doc.fields:
                    doc.fields[fname] = ExtractedField(fname, value, raw_value, page_no, conf)
        if doc_type == "commercial_invoice":
            for m in _LINE_ITEM_RE.finditer(text):
                doc.line_items.append({
                    "qty": int(m.group(1).replace(",", "")),
                    "unit_price": float(m.group(2).replace(",", "")),
                    "amount": float(m.group(3).replace(",", "")),
                    "page": page_no,
                })
    _post_process(doc)
    return doc


def _post_process(doc: ExtractedDoc) -> None:
    """Derive fields that don't appear as their own labelled line."""
    # "No of Packages: 1200 CARTONS" carries both count and package type
    if "package_count" in doc.fields:
        f = doc.fields["package_count"]
        m = re.search(r"[\d,]+\s+([A-Z][A-Z ]+)$", f.raw.strip())
        if m and "package_type" not in doc.fields:
            doc.fields["package_type"] = ExtractedField(
                "package_type", m.group(1).strip(), f.raw, f.page, f.confidence)
    # invoice goods description lives in line item 1, not a labelled field
    if doc.doc_type == "commercial_invoice" and "goods_description" not in doc.fields:
        for page_no, text in enumerate(doc.pages, start=1):
            m = re.search(r"^1\.\s+(.+)$", text, re.MULTILINE)
            if m:
                doc.fields["goods_description"] = ExtractedField(
                    "goods_description", m.group(1).strip(), m.group(0), page_no, 0.95)
                break
    # ISF "Seller" is the commercial shipper — expose under both names
    if doc.doc_type == "isf_filing" and "seller" in doc.fields and "shipper_name" not in doc.fields:
        f = doc.fields["seller"]
        doc.fields["shipper_name"] = ExtractedField("shipper_name", f.value, f.raw, f.page, f.confidence)


def extract_pack(pack_dir: str | Path) -> dict[str, ExtractedDoc]:
    """Extract every PDF in one shipment folder → {doc_type: ExtractedDoc}."""
    pack_dir = Path(pack_dir)
    out: dict[str, ExtractedDoc] = {}
    for pdf_path in sorted(pack_dir.glob("*.pdf")):
        doc = extract_document(pdf_path)
        out[doc.doc_type] = doc
    return out
