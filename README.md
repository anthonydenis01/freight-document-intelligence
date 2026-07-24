# Freight Document Intelligence

**Ask your shipping documents anything — and catch compliance gaps before
customs does.**

A RAG-powered agent for freight forwarders, customs brokers and compliance
teams. It ingests a shipment's full document pack (Bill of Lading,
commercial invoice, packing list, certificate of origin, ISF, customs
entry, arrival notice, delivery order), answers natural-language questions
across it **with mandatory source citations**, reconciles every document
against the others, and produces an audit-ready compliance report —
gated behind a human reviewer before anything would ever be filed.

Built on experience inside the Import VIP desk of one of the world's
largest container carriers: these are exactly the mismatches that trigger
customs exams, penalties and demurrage when nobody catches them pre-filing.

## What it does

- **Extracts + indexes** structured fields and full text from each PDF
  (pdfplumber + ChromaDB; every field keeps its page number and a
  confidence score)
- **Answers questions** across the doc set — *"What's the declared value
  vs invoice total for shipment MSKU-2847?"* — with a `[document p.N]`
  citation and confidence score on every answer. No citation, no claim.
- **Reconciles documents against each other**: container numbers, seals,
  vessels, weights, values, HTS codes, origin, consignee data — 12 checks,
  each mismatch flagged with a severity level and a dollar-impact estimate
- **Runs a compliance checklist**: ISF-10 completeness, HTS format +
  goods-description coherence, country-of-origin consistency → audit-ready
  report per shipment
- **Governance by design**: append-only decision trail (`trace.jsonl`),
  human-in-the-loop approval gate, synthetic data only

## Quick start

```bash
pip install -r requirements.txt

# Generate the synthetic document packs (10 shipments x 8 PDFs, sample set included)
python data/generate_sample_data.py

# Index every pack (local embeddings — nothing leaves your machine)
python freight_doc_agent.py ingest

# Ask questions — offline mode needs no API key
python freight_doc_agent.py --offline ask "Declared value vs invoice total for MSKU-2847?"

# Full audit: reconciliation + compliance, all shipments
python freight_doc_agent.py --offline audit

# With Claude orchestrating retrieval + writing the executive summary
export ANTHROPIC_API_KEY=sk-...
python freight_doc_agent.py ask "Why is CMAU-7913 risky to file as-is?"
python freight_doc_agent.py audit

# Record the human sign-off (nothing is 'filed' without it)
python freight_doc_agent.py approve --shipment MSKU-2847 --reviewer "A. Denis"
```

## Example output

```
$ python freight_doc_agent.py --offline ask "Declared value vs invoice total for MSKU-2847?"
MSKU-2847: declared value = $46,800.00 (per customs entry); invoice total =
$64,868.40 (per commercial invoice). Difference: $18,068.40 — MISMATCH,
review before filing.
  sources: [customs_entry p.1 — 06_customs_entry.pdf]; [commercial_invoice p.1 — 02_commercial_invoice.pdf]
  confidence: 0.99

$ python freight_doc_agent.py --offline audit --shipment MSKU-2847
===== SHIPMENT MSKU-2847 — DOCUMENT AUDIT =====
Reconciliation: 12 checks · 1 mismatch(es)
  ✖ [CRITICAL] entered value differs from commercial invoice total by $18,068.40
      est. impact: $18,068.40 (misdeclared value + duty delta at entry duty rate)
      sources: commercial_invoice p.1, customs_entry p.1
Compliance: 1 failed item(s)
  ⚠ [FAIL] ISF-10 element(s) missing: Manufacturer (or supplier)
Estimated exposure: $23,068.40
Recommendation: HOLD — resolve findings before filing
Governance status: PENDING_HUMAN_REVIEW
```

**Live demo:** [anthonydenis01.github.io/freight-document-intelligence/demo.html](https://anthonydenis01.github.io/freight-document-intelligence/demo.html)
— chat-style Q&A with cited answers, the reconciliation matrix, and the
compliance report view (static data mirroring the agent's real output).

## Architecture

```
data/shipments/<ID>/*.pdf   (8 docs per shipment, synthetic)
        │
        ▼
tools/pdf_extract.py   → typed fields + page + confidence per field
        │
        ├──────────────────────────────┐
        ▼                              ▼
tools/indexer.py                tools/reconcile.py   → 12 cross-doc checks
  ChromaDB, 1 chunk/page        tools/compliance.py  → ISF / HTS / origin
  local embeddings                     │
        ▼                              ▼
tools/rag.py           → cited  tools/reporting.py   → audit report + $ exposure
  answers (Q&A)                        │
        └──────────────┬───────────────┘
                       ▼
        freight_doc_agent.py   → CLI + Claude tool-use loop
        tools/governance.py    → trace.jsonl + human approval gate
```

The deterministic core (extraction, reconciliation, compliance) is
deliberately separated from the LLM layer. It runs with `--offline` and no
API key, and is fully unit-tested. Claude sits on top for retrieval-
augmented Q&A and executive summaries — it reasons *about* the findings,
it never replaces the rules that produce them.

### Reconciliation checks & impact model

| Check | Compared across | Severity | $ impact basis |
|---|---|---|---|
| Entered value vs invoice total | invoice ↔ entry | CRITICAL | value delta + duty-rate delta |
| HTS consistency | invoice ↔ ISF ↔ entry | CRITICAL | duty-rate difference × entered value |
| Country of origin | invoice ↔ certificate ↔ ISF ↔ entry | CRITICAL | exam + origin verification ($4,500 flat) |
| Consignee / importer of record | B/L ↔ entry ↔ ISF | HIGH | entry amendment + delay ($1,800 flat) |
| Container number | B/L ↔ packing list ↔ arrival notice ↔ D/O | HIGH | misrelease / detention ($1,250 flat) |
| B/L number | 5 documents | HIGH | ($1,250 flat) |
| Invoice math (lines vs total) | invoice internal | MEDIUM | arithmetic difference |
| Gross weight (±2% tolerance) | B/L ↔ packing list ↔ arrival notice | MEDIUM | reweigh ($850 flat) |
| Net vs gross weight | packing list internal | MEDIUM | ($850 flat) |
| Package count | 5 documents | MEDIUM | tally / shortage ($950 flat) |
| Seal number | B/L ↔ arrival notice | LOW | seal verification ($350 flat) |
| Vessel name | B/L ↔ entry ↔ arrival notice | LOW | doc correction ($150 flat) |

Compliance adds: **ISF-10 completeness** (missing element → up to $5,000
liquidated damages), **HTS format + chapter/description coherence**, and
**origin consistency**. Flat estimates are deliberately conservative
placeholders — swap in your own tariff/penalty tables in
`tools/reconcile.py`.

## Evaluation (measured, not aspirational)

`python eval.py` scores the agent against the generator's ground truth:

```
1. Field extraction accuracy : 100.0%  (870/870 fields)   [target ≥ 95%]  PASS
2. Injected discrepancies    : 12/12 caught (100%)        [target 100%]   PASS
3. RAG answers with citation : 10/10                      [target 100%]   PASS
4. One-pack end-to-end       : 0.24s                      [target < 60s]  PASS
```

## Data schema

`data/generate_sample_data.py` builds 10 shipments × 8 PDFs with **12
discrepancies deliberately injected** into 6 of the packs (the other 4 are
clean controls). `data/ground_truth.json` records, per shipment:

- `canonical` — the true value of every field (B/L number, container,
  vessel/voyage, ports, parties, HTS + duty rate, origin, weights,
  packages, invoice lines/total, entry + ISF data)
- `documents` — per document: file path + the exact field values printed
  on it (including the injected wrong ones)
- `injected_discrepancies` — `{check, doc, field, severity, note}` for
  each planted mismatch

Document types: `bill_of_lading`, `commercial_invoice`, `packing_list`,
`certificate_of_origin`, `isf_filing`, `customs_entry`, `arrival_notice`,
`delivery_order`.

## AI Governance

This agent is built for a domain where a wrong filing has legal
consequences, so governance is a feature, not an afterthought:

- **Synthetic data only.** Every company, vessel, container and value is
  invented by the generator. No real customer or employer data anywhere.
- **Human-in-the-loop by construction.** The pipeline can only produce
  reports in `PENDING_HUMAN_REVIEW`. The single path to `APPROVED_BY_HUMAN`
  is an explicit `approve` command with a named reviewer — recorded in the
  audit trail. The agent never files anything.
- **Full traceability.** Every decision — each extraction, each
  reconciliation rule (passes included), each compliance check, each
  answer — is appended to `trace.jsonl` with a run ID.
- **Citations everywhere.** Answers must cite document + page or state
  "not found in the document pack". The live-mode system prompt rejects
  and retries uncited answers.
- **Local by default.** Embeddings are computed locally (no model
  downloads, no third-party calls at index/retrieval time). The only
  external call is the optional Claude API in live mode.

## Running the tests

26 pytest tests cover extraction accuracy vs ground truth, every
reconciliation rule (including the clean-pack controls and the weight
tolerance boundary), ISF/HTS/origin compliance, citation guarantees, and
the approval gate.

```bash
pip install pytest   # dev-only dependency
python -m pytest tests/ -v
```

## Tech

Python 3.11+, [Anthropic Claude API](https://docs.claude.com) (tool use)
for RAG orchestration + audit narrative, [ChromaDB](https://www.trychroma.com)
with local hashed n-gram embeddings (swap-in point for any embedding
model in `tools/embeddings.py`), [pdfplumber](https://github.com/jsvine/pdfplumber)
for PDF parsing, [ReportLab](https://www.reportlab.com) for the synthetic
document generator.

## Possible extensions

- OCR ingestion for scanned/photographed documents (Claude vision)
- Real HTS duty-rate lookups (USITC HTS REST API) instead of the hint table
- Watch-folder mode: audit each pack as documents arrive from email/EDI
- Neural embeddings (one-line swap) for fuzzier semantic retrieval
- FastAPI endpoint + webhook delivery of audit reports

---

Part of a 4-agent portfolio for AI-powered logistics automation.
See also: [Import Desk Automation Agent](https://github.com/anthonydenis01/import-desk-automation-agent),
[Container Visibility Agent](https://github.com/anthonydenis01/container-visibility-agent) and
[Carrier Scorecard Agent](https://github.com/anthonydenis01/carrier-scorecard-agent).
