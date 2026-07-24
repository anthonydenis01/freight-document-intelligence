#!/usr/bin/env python3
"""Synthetic freight document pack generator.

Generates 10 shipments x 8 PDF documents each (Bill of Lading, Commercial
Invoice, Packing List, Certificate of Origin, ISF Filing, Customs Entry,
Arrival Notice, Delivery Order), plus a ground-truth JSON file describing
every printed field value and every deliberately injected discrepancy.

Everything is synthetic. Company names, addresses, vessels, B/L numbers and
container numbers are invented. No real shipper, consignee or carrier data
is used anywhere.

Usage:
    python data/generate_sample_data.py            # writes data/shipments/ + data/ground_truth.json
    python data/generate_sample_data.py --outdir /tmp/packs
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas as pdfcanvas

SEED = 2026

DOC_ORDER = [
    "bill_of_lading",
    "commercial_invoice",
    "packing_list",
    "certificate_of_origin",
    "isf_filing",
    "customs_entry",
    "arrival_notice",
    "delivery_order",
]

DOC_TITLES = {
    "bill_of_lading": "BILL OF LADING",
    "commercial_invoice": "COMMERCIAL INVOICE",
    "packing_list": "PACKING LIST",
    "certificate_of_origin": "CERTIFICATE OF ORIGIN",
    "isf_filing": "IMPORTER SECURITY FILING (ISF 10+2)",
    "customs_entry": "ENTRY SUMMARY (CBP FORM 7501 STYLE)",
    "arrival_notice": "ARRIVAL NOTICE / FREIGHT INVOICE",
    "delivery_order": "DELIVERY ORDER",
}

# ---------------------------------------------------------------------------
# Synthetic reference data (all invented)
# ---------------------------------------------------------------------------

PRODUCTS = [
    ("Wireless network routers, 802.11ax", "8517.62.0090", 0.000, "China", "SHENZHEN NETCORE ELECTRONICS CO LTD", "12F Huaqiang Tower, Futian District, Shenzhen, Guangdong, CN"),
    ("Upholstered dining chairs, wooden frame", "9401.61.4011", 0.000, "Vietnam", "BINH DUONG FURNITURE MFG JSC", "Lot C4, VSIP II Industrial Park, Binh Duong, VN"),
    ("Men's cotton knit t-shirts", "6109.10.0012", 0.165, "Bangladesh", "DHAKA APPAREL WORKS LTD", "Plot 114, DEPZ, Savar, Dhaka, BD"),
    ("Brake pads, disc, for passenger vehicles", "8708.30.5090", 0.025, "South Korea", "BUSAN AUTOPARTS INDUSTRIAL CO", "88 Noksan Sandan-ro, Gangseo-gu, Busan, KR"),
    ("Green coffee beans, arabica, not roasted", "0901.11.0025", 0.000, "Colombia", "CAFETERA DEL HUILA S.A.S.", "Km 4 Via Palermo, Neiva, Huila, CO"),
    ("Plastic construction toy sets", "9503.00.0073", 0.000, "China", "NINGBO BRIGHTPLAY TOYS CO LTD", "No. 55 Cixi East Rd, Ningbo, Zhejiang, CN"),
    ("Centrifugal water pumps, 5-15 kW", "8413.70.2004", 0.000, "Italy", "POMPE ADRIATICA S.R.L.", "Via dell'Industria 22, Ancona, IT"),
    ("Disposable nitrile examination gloves", "4015.12.1000", 0.030, "Malaysia", "SELANGOR MEDIGLOVE SDN BHD", "Jalan Perusahaan 6, Klang, Selangor, MY"),
    ("Stainless steel cookware sets", "7323.93.0045", 0.020, "India", "CHENNAI STEELWARE EXPORTS PVT LTD", "SIPCOT Industrial Park, Sriperumbudur, IN"),
    ("Monocrystalline solar panel modules, 450W", "8541.43.0010", 0.000, "Thailand", "RAYONG SOLARTECH CO LTD", "Hemaraj Eastern Industrial Estate, Rayong, TH"),
]

CONSIGNEES = [
    ("SUNBELT TRADING GROUP LLC", "2110 NW 82ND AVE, DORAL, FL 33122"),
    ("GULFSTREAM HOME & LIVING INC", "4500 OAKES RD, DAVIE, FL 33314"),
    ("BISCAYNE APPAREL DISTRIBUTORS", "7801 NW 37TH ST, MIAMI, FL 33166"),
    ("EVERGLADES AUTOMOTIVE SUPPLY CO", "1290 SW 30TH AVE, POMPANO BEACH, FL 33069"),
    ("PORT CITY COFFEE ROASTERS LLC", "3350 NW 60TH ST, MIAMI, FL 33142"),
    ("ATLANTIC TOY & NOVELTY CORP", "9600 NW 25TH ST, DORAL, FL 33172"),
    ("FLORIDA INDUSTRIAL PUMPS INC", "5501 ANGLERS AVE, FORT LAUDERDALE, FL 33312"),
    ("CORAL MEDICAL SUPPLY GROUP", "8200 NW 52ND TER, DORAL, FL 33166"),
    ("SOUTHEAST KITCHEN IMPORTS LLC", "1150 NW 72ND AVE, MIAMI, FL 33126"),
    ("SOLARIS ENERGY DISTRIBUTORS", "2901 SW 149TH AVE, MIRAMAR, FL 33027"),
]

VESSELS = [
    ("MSC ALTAIR VII", "426A"), ("EVER LUCENT", "1142E"), ("CMA CGM ALMAVIVA", "0FL8W"),
    ("MAERSK KOWLOON", "618N"), ("ONE HARMONIA", "077E"), ("COSCO PACIFIC DAWN", "052W"),
    ("BREMEN EXPRESS II", "2611"), ("OOCL VALENCIA", "089W"), ("ZIM CARIBBEAN STAR", "34S"),
    ("HYUNDAI COURAGE", "119E"),
]

POLS = ["YANTIAN, CN", "HO CHI MINH (CAT LAI), VN", "CHITTAGONG, BD", "BUSAN, KR", "CARTAGENA, CO",
        "NINGBO, CN", "GENOA, IT", "PORT KLANG, MY", "CHENNAI, IN", "LAEM CHABANG, TH"]

SHIPMENT_PREFIXES = ["MSKU", "MSCU", "TCLU", "CMAU", "OOLU", "HLXU", "TGHU", "APZU", "FCIU", "TRLU"]

CUSTOMS_BROKER = "PANAMERICAN CUSTOMS BROKERS INC, 8300 NW 53RD ST STE 350, DORAL, FL 33166"
FORWARDER = "MERIDIAN GLOBAL FORWARDING (USA) LLC"

# ---------------------------------------------------------------------------


def _check_digit(prefix: str, serial: str) -> str:
    """ISO 6346 check digit so container numbers look real."""
    values = {c: v for c, v in zip("0123456789", range(10))}
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    lv, skip = 10, {11, 22, 33}
    for ch in letters:
        while lv in skip:
            lv += 1
        values[ch] = lv
        lv += 1
    s = sum(values[ch] * (2 ** i) for i, ch in enumerate(prefix + serial))
    return str(s % 11 % 10)


def make_shipment(i: int, rng: random.Random) -> dict:
    desc, hts, duty_rate, coo, shipper_name, shipper_addr = PRODUCTS[i]
    cons_name, cons_addr = CONSIGNEES[i]
    vessel, voyage = VESSELS[i]
    prefix = SHIPMENT_PREFIXES[i]
    serial = "284761" if i == 0 else f"{rng.randint(200000, 899999)}"
    container = f"{prefix}{serial}{_check_digit(prefix, serial)}"
    shipment_id = f"{prefix}-{serial[:4]}"  # e.g. MSKU-2847

    unit_price = round(rng.uniform(3.5, 220.0), 2)
    qty = rng.randint(80, 2400)
    line2_qty = rng.randint(40, 800)
    line2_price = round(unit_price * rng.uniform(0.6, 1.4), 2)
    if i == 0:  # pin the case-study shipment to a memorable total
        unit_price, qty, line2_price, line2_qty = 42.50, 1200, 27.30, 508
    line1_amt = round(unit_price * qty, 2)
    line2_amt = round(line2_price * line2_qty, 2)
    invoice_total = round(line1_amt + line2_amt, 2)

    gross = round(rng.uniform(3800, 21500), 1)
    net = round(gross * rng.uniform(0.82, 0.93), 1)
    cbm = round(rng.uniform(18, 66), 2)
    pkgs = rng.randint(120, 1450)

    month = rng.randint(1, 5)
    etd = f"2026-{month:02d}-{rng.randint(2, 26):02d}"
    eta = f"2026-{month + 1:02d}-{rng.randint(2, 27):02d}"

    return {
        "shipment_id": shipment_id,
        "canonical": {
            "bl_number": f"MGF{rng.randint(10000000, 99999999)}",
            "container_number": container,
            "seal_number": f"SL{rng.randint(1000000, 9999999)}",
            "vessel": vessel,
            "voyage": voyage,
            "port_of_loading": POLS[i],
            "port_of_discharge": "MIAMI, FL, US",
            "etd": etd,
            "eta": eta,
            "shipper_name": shipper_name,
            "shipper_address": shipper_addr,
            "consignee_name": cons_name,
            "consignee_address": cons_addr,
            "notify_party": CUSTOMS_BROKER,
            "goods_description": desc,
            "hts_code": hts,
            "duty_rate": duty_rate,
            "country_of_origin": coo,
            "package_count": pkgs,
            "package_type": "CARTONS",
            "gross_weight_kg": gross,
            "net_weight_kg": net,
            "volume_cbm": cbm,
            "invoice_number": f"CI-2026-{rng.randint(1000, 9899)}",
            "invoice_total": invoice_total,
            "currency": "USD",
            "incoterm": "FOB " + POLS[i].split(",")[0].title(),
            "line_items": [
                {"description": desc, "qty": qty, "unit_price": unit_price, "amount": line1_amt},
                {"description": desc + " (spare/accessory units)", "qty": line2_qty,
                 "unit_price": line2_price, "amount": line2_amt},
            ],
            "entry_number": f"ABI-{rng.randint(10000000, 99999999)}-{rng.randint(0, 9)}",
            "declared_value": invoice_total,
            "importer_of_record": cons_name,
            "manufacturer_name": shipper_name,
            "manufacturer_address": shipper_addr,
            "stuffing_location": shipper_addr,
            "consolidator": FORWARDER,
            "bond_type": "CONTINUOUS (TYPE 8)",
            "isf_transaction": f"ISF{rng.randint(100000000, 999999999)}",
            "firms_code": f"F{rng.randint(100, 999)}",
            "it_number": f"V{rng.randint(1000000, 9999999)}",
        },
    }


# ---------------------------------------------------------------------------
# Discrepancy injection
# ---------------------------------------------------------------------------
# Each entry: (shipment_index, doc_type, field, mutate_fn, check_id, severity)
# The reconciliation engine in tools/reconcile.py must catch every one.

def build_injection_plan() -> list[dict]:
    return [
        # --- MSKU-2847 : the case-study shipment ---
        {"ship": 0, "doc": "customs_entry", "field": "declared_value",
         "value": lambda c: 46800.00, "check": "declared_value_vs_invoice_total", "severity": "CRITICAL",
         "note": "Entry declares $46,800.00 against a $64,868.40 commercial invoice (-$18,068.40)."},
        {"ship": 0, "doc": "isf_filing", "field": "manufacturer_name",
         "value": lambda c: "", "check": "isf_completeness", "severity": "HIGH",
         "note": "ISF filed with manufacturer name blank (ISF-10 element missing)."},
        # --- TCLU pack ---
        {"ship": 2, "doc": "customs_entry", "field": "hts_code",
         "value": lambda c: "6104.62.2011", "check": "hts_consistency", "severity": "CRITICAL",
         "note": "Entry HTS (6104.62, 14.9%) differs from invoice HTS (6109.10, 16.5%) — duty underpayment."},
        {"ship": 2, "doc": "packing_list", "field": "gross_weight_kg",
         "value": lambda c: round(c["gross_weight_kg"] * 1.149, 1), "check": "gross_weight_consistency",
         "severity": "MEDIUM", "note": "Packing list gross weight ~15% above B/L weight."},
        # --- CMAU pack ---
        {"ship": 3, "doc": "certificate_of_origin", "field": "country_of_origin",
         "value": lambda c: "China", "check": "country_of_origin_consistency", "severity": "CRITICAL",
         "note": "CO certificate shows China; invoice and entry show South Korea."},
        {"ship": 3, "doc": "arrival_notice", "field": "container_number",
         "value": lambda c: c["container_number"][:6] + "89" + c["container_number"][8:],
         "check": "container_number_consistency", "severity": "HIGH",
         "note": "Arrival notice container number has transposed digits."},
        # --- OOLU pack ---
        {"ship": 4, "doc": "customs_entry", "field": "importer_of_record",
         "value": lambda c: "PORT CITY COFFEE TRADERS LLC", "check": "consignee_consistency",
         "severity": "HIGH", "note": "Entry importer of record does not match B/L consignee."},
        {"ship": 4, "doc": "commercial_invoice", "field": "invoice_total",
         "value": lambda c: round(c["invoice_total"] + 1010.00, 2), "check": "invoice_math",
         "severity": "MEDIUM", "note": "Invoice stated total exceeds the sum of its line items."},
        # --- HLXU pack ---
        {"ship": 5, "doc": "packing_list", "field": "package_count",
         "value": lambda c: c["package_count"] - 36, "check": "package_count_consistency",
         "severity": "MEDIUM", "note": "Packing list carton count short of B/L count."},
        {"ship": 5, "doc": "arrival_notice", "field": "seal_number",
         "value": lambda c: "SL" + c["seal_number"][3:] + "9", "check": "seal_number_consistency",
         "severity": "LOW", "note": "Arrival notice seal number differs from B/L seal."},
        # --- TGHU pack ---
        {"ship": 6, "doc": "packing_list", "field": "net_weight_kg",
         "value": lambda c: round(c["gross_weight_kg"] * 1.06, 1), "check": "net_vs_gross_weight",
         "severity": "MEDIUM", "note": "Packing list net weight exceeds its own gross weight."},
        {"ship": 6, "doc": "arrival_notice", "field": "vessel",
         "value": lambda c: "BREMEN EXPRESS", "check": "vessel_consistency", "severity": "LOW",
         "note": "Arrival notice vessel name does not match B/L vessel."},
    ]


# ---------------------------------------------------------------------------
# Which canonical fields appear on which document
# ---------------------------------------------------------------------------

DOC_FIELDS = {
    "bill_of_lading": [
        "bl_number", "shipper_name", "shipper_address", "consignee_name", "consignee_address",
        "notify_party", "vessel", "voyage", "port_of_loading", "port_of_discharge", "etd",
        "container_number", "seal_number", "package_count", "package_type", "gross_weight_kg",
        "volume_cbm", "goods_description",
    ],
    "commercial_invoice": [
        "invoice_number", "shipper_name", "shipper_address", "consignee_name", "consignee_address",
        "incoterm", "currency", "country_of_origin", "hts_code", "goods_description",
        "invoice_total",
    ],
    "packing_list": [
        "invoice_number", "container_number", "package_count", "package_type",
        "gross_weight_kg", "net_weight_kg", "volume_cbm", "goods_description",
    ],
    "certificate_of_origin": [
        "shipper_name", "shipper_address", "consignee_name", "consignee_address",
        "country_of_origin", "goods_description", "invoice_number", "package_count",
    ],
    "isf_filing": [
        "isf_transaction", "bl_number", "importer_of_record", "consignee_name", "shipper_name",
        "manufacturer_name", "manufacturer_address", "country_of_origin", "hts_code",
        "stuffing_location", "consolidator", "bond_type",
    ],
    "customs_entry": [
        "entry_number", "importer_of_record", "bl_number", "hts_code", "goods_description",
        "country_of_origin", "declared_value", "duty_rate", "vessel", "port_of_discharge", "eta",
    ],
    "arrival_notice": [
        "bl_number", "container_number", "seal_number", "vessel", "voyage", "eta",
        "port_of_discharge", "consignee_name", "package_count", "gross_weight_kg", "firms_code",
        "it_number",
    ],
    "delivery_order": [
        "bl_number", "container_number", "consignee_name", "consignee_address",
        "firms_code", "it_number", "package_count",
    ],
}


def printed_fields(shipment: dict, injections: list[dict], ship_idx: int) -> dict:
    """Per-document printed values = canonical values + injected mutations."""
    docs = {}
    for doc_type, fields in DOC_FIELDS.items():
        vals = {f: shipment["canonical"][f] for f in fields}
        if doc_type == "commercial_invoice":
            vals["line_items"] = shipment["canonical"]["line_items"]
        docs[doc_type] = vals
    for inj in injections:
        if inj["ship"] == ship_idx:
            docs[inj["doc"]][inj["field"]] = inj["value"](shipment["canonical"])
    return docs


# ---------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------

W, H = letter


class Doc:
    def __init__(self, path: Path, title: str, ref: str):
        self.c = pdfcanvas.Canvas(str(path), pagesize=letter)
        self.title = title
        self.ref = ref
        self.y = 0.0
        self._page_head()

    def _page_head(self):
        c = self.c
        c.setFont("Helvetica-Bold", 15)
        c.drawString(0.75 * inch, H - 0.85 * inch, self.title)
        c.setFont("Helvetica", 8)
        c.drawRightString(W - 0.75 * inch, H - 0.85 * inch, f"REF {self.ref}  |  SYNTHETIC DOCUMENT - DEMO ONLY")
        c.setLineWidth(1.2)
        c.line(0.75 * inch, H - 0.98 * inch, W - 0.75 * inch, H - 0.98 * inch)
        self.y = H - 1.3 * inch

    def _need(self, dy: float):
        if self.y - dy < 0.9 * inch:
            self.c.showPage()
            self._page_head()

    def section(self, name: str):
        self._need(0.42 * inch)
        self.y -= 0.16 * inch
        self.c.setFont("Helvetica-Bold", 9.5)
        self.c.drawString(0.75 * inch, self.y, name.upper())
        self.y -= 0.06 * inch
        self.c.setLineWidth(0.5)
        self.c.line(0.75 * inch, self.y, W - 0.75 * inch, self.y)
        self.y -= 0.18 * inch

    def kv(self, label: str, value):
        self._need(0.22 * inch)
        self.c.setFont("Helvetica", 9)
        self.c.drawString(0.75 * inch, self.y, f"{label}:")
        self.c.setFont("Helvetica-Bold", 9)
        self.c.drawString(2.65 * inch, self.y, str(value))
        self.y -= 0.205 * inch

    def para(self, text: str):
        self._need(0.22 * inch)
        self.c.setFont("Helvetica", 8.5)
        self.c.drawString(0.75 * inch, self.y, text)
        self.y -= 0.19 * inch

    def save(self):
        self.c.setFont("Helvetica-Oblique", 7.5)
        self.c.drawString(0.75 * inch, 0.55 * inch,
                          "Generated by freight-document-intelligence sample data generator. All parties, values and documents are fictitious.")
        self.c.save()


def fmt_w(v):
    return f"{v:,.1f} KGS"


def fmt_money(v, cur="USD"):
    return f"{cur} {v:,.2f}"


def render_pdf(path: Path, doc_type: str, f: dict, ship: dict):
    d = Doc(path, DOC_TITLES[doc_type], ship["shipment_id"])
    if doc_type == "bill_of_lading":
        d.section("Parties")
        d.kv("B/L Number", f["bl_number"])
        d.kv("Shipper", f["shipper_name"])
        d.kv("Shipper Address", f["shipper_address"])
        d.kv("Consignee", f["consignee_name"])
        d.kv("Consignee Address", f["consignee_address"])
        d.kv("Notify Party", f["notify_party"])
        d.section("Voyage")
        d.kv("Ocean Vessel", f["vessel"])
        d.kv("Voyage No", f["voyage"])
        d.kv("Port of Loading", f["port_of_loading"])
        d.kv("Port of Discharge", f["port_of_discharge"])
        d.kv("Shipped on Board", f["etd"])
        d.section("Cargo")
        d.kv("Container No", f["container_number"])
        d.kv("Seal No", f["seal_number"])
        d.kv("No of Packages", f'{f["package_count"]} {f["package_type"]}')
        d.kv("Gross Weight", fmt_w(f["gross_weight_kg"]))
        d.kv("Measurement", f'{f["volume_cbm"]} CBM')
        d.kv("Description of Goods", f["goods_description"])
        d.para("SHIPPER'S LOAD, STOWAGE AND COUNT. FREIGHT PREPAID. CLEAN ON BOARD.")
    elif doc_type == "commercial_invoice":
        d.section("Invoice Header")
        d.kv("Invoice Number", f["invoice_number"])
        d.kv("Seller", f["shipper_name"])
        d.kv("Seller Address", f["shipper_address"])
        d.kv("Buyer", f["consignee_name"])
        d.kv("Buyer Address", f["consignee_address"])
        d.kv("Incoterm", f["incoterm"])
        d.kv("Currency", f["currency"])
        d.kv("Country of Origin", f["country_of_origin"])
        d.kv("HTS Code", f["hts_code"])
        d.section("Line Items")
        for n, li in enumerate(f["line_items"], 1):
            d.para(f'{n}. {li["description"]}')
            d.para(f'   Qty {li["qty"]}  x  Unit Price {li["unit_price"]:,.2f}  =  Amount {li["amount"]:,.2f}')
        d.section("Totals")
        d.kv("Invoice Total", fmt_money(f["invoice_total"], f["currency"]))
        d.para("We certify this invoice is true and correct and prices reflect the transaction value.")
    elif doc_type == "packing_list":
        d.section("Reference")
        d.kv("Invoice Number", f["invoice_number"])
        d.kv("Container No", f["container_number"])
        d.section("Packing Detail")
        d.kv("Total Packages", f'{f["package_count"]} {f["package_type"]}')
        d.kv("Gross Weight", fmt_w(f["gross_weight_kg"]))
        d.kv("Net Weight", fmt_w(f["net_weight_kg"]))
        d.kv("Total Volume", f'{f["volume_cbm"]} CBM')
        d.kv("Goods", f["goods_description"])
        d.para("Marks & numbers: as addressed. Packed in export cartons on heat-treated pallets.")
    elif doc_type == "certificate_of_origin":
        d.section("Certification")
        d.kv("Exporter", f["shipper_name"])
        d.kv("Exporter Address", f["shipper_address"])
        d.kv("Consignee", f["consignee_name"])
        d.kv("Consignee Address", f["consignee_address"])
        d.kv("Invoice Reference", f["invoice_number"])
        d.kv("Packages", f["package_count"])
        d.kv("Goods", f["goods_description"])
        d.kv("Country of Origin", f["country_of_origin"])
        d.para("The undersigned chamber certifies that the goods above originate in the stated country.")
    elif doc_type == "isf_filing":
        d.section("Filing")
        d.kv("ISF Transaction No", f["isf_transaction"])
        d.kv("Master B/L", f["bl_number"])
        d.kv("Bond Type", f["bond_type"])
        d.section("ISF-10 Data Elements")
        d.kv("Importer of Record", f["importer_of_record"])
        d.kv("Consignee", f["consignee_name"])
        d.kv("Seller", f["shipper_name"])
        d.kv("Buyer", f["consignee_name"])
        d.kv("Manufacturer", f["manufacturer_name"] if f["manufacturer_name"] else "-")
        d.kv("Manufacturer Address", f["manufacturer_address"])
        d.kv("Ship To Party", f["consignee_name"])
        d.kv("Country of Origin", f["country_of_origin"])
        d.kv("HTS Number", f["hts_code"])
        d.kv("Container Stuffing Location", f["stuffing_location"])
        d.kv("Consolidator", f["consolidator"])
    elif doc_type == "customs_entry":
        d.section("Entry")
        d.kv("Entry Number", f["entry_number"])
        d.kv("Importer of Record", f["importer_of_record"])
        d.kv("Master B/L", f["bl_number"])
        d.kv("Importing Vessel", f["vessel"])
        d.kv("Port of Entry", f["port_of_discharge"])
        d.kv("Import Date", f["eta"])
        d.section("Classification & Value")
        d.kv("HTS Number", f["hts_code"])
        d.kv("Description", f["goods_description"])
        d.kv("Country of Origin", f["country_of_origin"])
        d.kv("Entered Value", fmt_money(f["declared_value"]))
        d.kv("Duty Rate", f'{f["duty_rate"] * 100:.1f} %')
        d.kv("Estimated Duty", fmt_money(round(f["declared_value"] * f["duty_rate"], 2)))
        d.para("DRAFT - NOT FILED. Pending broker review and importer approval.")
    elif doc_type == "arrival_notice":
        d.section("Arrival Details")
        d.kv("Master B/L", f["bl_number"])
        d.kv("Container No", f["container_number"])
        d.kv("Seal No", f["seal_number"])
        d.kv("Vessel", f["vessel"])
        d.kv("Voyage", f["voyage"])
        d.kv("ETA", f["eta"])
        d.kv("Port of Discharge", f["port_of_discharge"])
        d.kv("FIRMS Code", f["firms_code"])
        d.kv("IT Number", f["it_number"])
        d.section("Cargo & Charges")
        d.kv("Consignee", f["consignee_name"])
        d.kv("Packages", f["package_count"])
        d.kv("Gross Weight", fmt_w(f["gross_weight_kg"]))
        d.kv("Ocean Freight", fmt_money(2850.00))
        d.kv("Terminal Handling", fmt_money(465.00))
        d.para("Cargo must be picked up within free time to avoid demurrage.")
    elif doc_type == "delivery_order":
        d.section("Release")
        d.kv("Master B/L", f["bl_number"])
        d.kv("Container No", f["container_number"])
        d.kv("Deliver To", f["consignee_name"])
        d.kv("Delivery Address", f["consignee_address"])
        d.kv("FIRMS Code", f["firms_code"])
        d.kv("IT Number", f["it_number"])
        d.kv("Packages", f["package_count"])
        d.para("Release subject to customs clearance and payment of all charges.")
    d.save()


# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--outdir", default=str(Path(__file__).parent), help="Output directory (default: data/)")
    args = ap.parse_args()
    out = Path(args.outdir)
    rng = random.Random(SEED)

    shipments = [make_shipment(i, rng) for i in range(10)]
    plan = build_injection_plan()

    ground_truth = {"seed": SEED, "note": "All data synthetic.", "shipments": []}
    for i, ship in enumerate(shipments):
        docs = printed_fields(ship, plan, i)
        ship_dir = out / "shipments" / ship["shipment_id"]
        ship_dir.mkdir(parents=True, exist_ok=True)
        doc_entries = {}
        for n, doc_type in enumerate(DOC_ORDER, 1):
            fname = f"{n:02d}_{doc_type}.pdf"
            render_pdf(ship_dir / fname, doc_type, docs[doc_type], ship)
            doc_entries[doc_type] = {
                "file": f"shipments/{ship['shipment_id']}/{fname}",
                "fields": {k: v for k, v in docs[doc_type].items() if k != "line_items"},
            }
            if doc_type == "commercial_invoice":
                doc_entries[doc_type]["line_items"] = docs[doc_type]["line_items"]
        injected = [
            {"check": p["check"], "doc": p["doc"], "field": p["field"], "severity": p["severity"], "note": p["note"]}
            for p in plan if p["ship"] == i
        ]
        ground_truth["shipments"].append({
            "shipment_id": ship["shipment_id"],
            "clean": not injected,
            "canonical": ship["canonical"],
            "documents": doc_entries,
            "injected_discrepancies": injected,
        })

    gt_path = out / "ground_truth.json"
    gt_path.write_text(json.dumps(ground_truth, indent=2))
    n_docs = sum(len(s["documents"]) for s in ground_truth["shipments"])
    n_inj = sum(len(s["injected_discrepancies"]) for s in ground_truth["shipments"])
    print(f"Generated {len(shipments)} shipments, {n_docs} PDFs, {n_inj} injected discrepancies.")
    print(f"Ground truth written to {gt_path}")


if __name__ == "__main__":
    main()
