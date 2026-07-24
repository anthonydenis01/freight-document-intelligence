# Freight Document Intelligence

**An evidence-backed freight-document intelligence engine** for freight
forwarders, customs brokers and compliance teams: structured extraction,
cited Q&A, cross-document reconciliation and compliance checks over a
shipment's document pack — **decision-support only**, with a human review
gate before anything filing-related, and an API-first design so the same
engine serves Copilot Studio, ChatGPT actions, Claude, or your internal
systems.

Built on experience inside the Import VIP desk of one of the world's
largest container carriers: these are exactly the mismatches that trigger
customs exams, penalties and demurrage when nobody catches them
pre-filing.

## What the engine proves

- **Document ingestion + structured extraction** — 8 document types per
  shipment (B/L, commercial invoice, packing list, certificate of origin,
  ISF, customs entry, arrival notice, delivery order); every extracted
  field keeps its page number and a parse-quality score
- **Evidence-backed Q&A** — *"What's the declared value vs invoice total
  for shipment MSKU-2847?"* — every answer carries `[document p.N]`
  citations that are generated from stored extraction objects and
  metadata, not written freehand by a model; unsupported questions get an
  explicit "not found", never a guess
- **Deterministic cross-document reconciliation** — 12 rule-based checks
  (values, HTS, origin, parties, containers, seals, vessels, weights,
  packages) with tolerance bands, severity policy, and an honest impact
  model (see below)
- **Compliance checklist** — ISF-10 completeness, HTS format +
  chapter/description plausibility, origin consistency → audit-ready
  report per shipment
- **Follow-up actions with one-click human approval** — every finding
  proposes its resolution: who to contact (shipper, broker, carrier,
  forwarder, terminal), what to ask for, and a ready-to-send draft email
  quoting the exact values and citations. Workflow:
  `PROPOSED → APPROVED (named reviewer, one click) → SENT_SIMULATED` —
  the agent has no mailbox access by design; dispatch is simulated and
  the draft is yours to copy
- **Human review gate + full traceability** — reports are always
  `PENDING_HUMAN_REVIEW`; only a named reviewer can approve, and every
  decision (passes included) is appended to `trace.jsonl`
- **API-first integration readiness** — a REST service with an OpenAPI
  schema ([docs/openapi.json](docs/openapi.json)) consumable by Copilot
  Studio, ChatGPT actions, Claude and internal tools
  ([docs/INTEGRATIONS.md](docs/INTEGRATIONS.md))

The architecture rule throughout: **Python and rules do the parts that
must be correct; the model helps where the source material is messy.**
Parsing, normalization, arithmetic validation, reconciliation, severity
and audit logging are deterministic and run with no API key (`--offline`).
The LLM layer (Claude by default, OpenAI/Azure via `--provider`) only
composes cited answers and executive summaries on top of tool results —
and business logic survives model replacement by construction
(`tools/providers.py`).

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

# Live mode — pick your model provider
export ANTHROPIC_API_KEY=sk-...   # Claude (default)
python freight_doc_agent.py audit
python freight_doc_agent.py --provider openai ask "Why is CMAU-7913 risky to file as-is?"

# Follow-up actions: list drafts, approve (1 command), simulate dispatch
python freight_doc_agent.py --offline actions list --shipment MSKU-2847 --drafts
python freight_doc_agent.py --offline actions approve --id "MSKU-2847:isf_completeness" --reviewer "A. Denis"
python freight_doc_agent.py --offline actions send --id "MSKU-2847:isf_completeness"

# Record the human sign-off (nothing is 'filed' without it)
python freight_doc_agent.py approve --shipment MSKU-2847 --reviewer "A. Denis"

# Or run it as a service (OpenAPI at /openapi.json, docs at /docs)
uvicorn api:app --reload
```

## Example output

```
$ python freight_doc_agent.py --offline ask "Declared value vs invoice total for MSKU-2847?"
MSKU-2847: declared value = $46,800.00 (per customs entry); invoice total =
$64,868.40 (per commercial invoice). Difference: $18,068.40 — MISMATCH,
review before filing.
  sources: [customs_entry p.1 — 06_customs_entry.pdf]; [commercial_invoice p.1 — 02_commercial_invoice.pdf]
  confidence (heuristic): answer 0.99 · extraction 0.99

$ python freight_doc_agent.py --offline audit --shipment MSKU-2847
===== SHIPMENT MSKU-2847 — DOCUMENT AUDIT =====
Reconciliation: 12 checks · 1 mismatch(es)
  ✖ [CRITICAL] entered value differs from commercial invoice total by $18,068.40
      exact delta: $18,068.40 (exact delta between entered value and invoice total)
      sources: commercial_invoice p.1, customs_entry p.1
Compliance: 1 failed item(s)
  ⚠ [FAIL] ISF-10 element(s) missing: Manufacturer (or supplier)
Quantified deltas: $18,068.40   Potential exposure (est.): $5,000.00
Recommendation: HOLD — resolve findings before filing
Governance status: PENDING_HUMAN_REVIEW
```

**Live demo:** [anthonydenis01.github.io/freight-document-intelligence/demo.html](https://anthonydenis01.github.io/freight-document-intelligence/demo.html)
— chat-style Q&A with cited answers, the reconciliation matrix, and the
compliance report view. The demo is static; its numbers mirror real CLI
outputs (`eval_results.json`, `reports/`).

## Architecture

```
data/shipments/<ID>/*.pdf   (8 docs per shipment, synthetic)
        │
        ▼
tools/pdf_extract.py   → typed fields + page + parse confidence
        │
        ├──────────────────────────────┐
        ▼                              ▼
tools/indexer.py                tools/reconcile.py   → 12 cross-doc checks
  ChromaDB, 1 chunk/page        tools/compliance.py  → ISF / HTS / origin
  local embeddings                     │
        ▼                              ▼
tools/rag.py           → cited  tools/reporting.py   → audit report, exposure split
  answers (Q&A)                        │
        └──────────────┬───────────────┘
                       ▼
        api.py                 → REST service + OpenAPI (integration surface)
        freight_doc_agent.py   → CLI
        tools/providers.py     → Claude / OpenAI / Azure abstraction (live mode only)
        tools/governance.py    → trace.jsonl + human approval gate
```

### Reconciliation checks & the impact model

Only three kinds of dollar figure are stated as numbers, and each finding
is labeled with its kind:

- **exact delta** — recomputable from the documents themselves (invoice
  arithmetic, entered-value vs invoice-total)
- **duty-rate delta** — rate difference × entered value, with the rate
  basis shown (demo rate table — verify against current HTSUS)
- **potential exposure** — rough flat estimates requiring broker review;
  reported separately and **never summed with exact deltas**

| Check | Compared across | Severity | Impact kind |
|---|---|---|---|
| Entered value vs invoice total | invoice ↔ entry | CRITICAL | exact delta (+ duty note) |
| HTS consistency | invoice ↔ ISF ↔ entry | CRITICAL | duty-rate delta |
| Country of origin | invoice ↔ certificate ↔ ISF ↔ entry | CRITICAL | potential exposure ($4,500) |
| Consignee / importer of record | B/L ↔ entry ↔ ISF | HIGH | potential exposure ($1,800) |
| Container number | B/L ↔ packing list ↔ arrival ↔ D/O | HIGH | potential exposure ($1,250) |
| B/L number | 5 documents | HIGH | potential exposure ($1,250) |
| Invoice math (lines vs total) | invoice internal | MEDIUM | exact delta |
| Gross weight (±2% tolerance) | B/L ↔ packing list ↔ arrival | MEDIUM | potential exposure ($850) |
| Net vs gross weight | packing list internal | MEDIUM | potential exposure ($850) |
| Package count | 5 documents | MEDIUM | potential exposure ($950) |
| Seal number | B/L ↔ arrival notice | LOW | potential exposure ($350) |
| Vessel name | B/L ↔ entry ↔ arrival | LOW | potential exposure ($150) |

Compliance adds **ISF-10 completeness** (missing element → potential
exposure wording referencing CBP's up-to-$5,000 liquidated damages, not a
guaranteed amount), **HTS format + chapter/description plausibility**
(a screen, not a classification engine), and **origin consistency**.

## Evaluation (measured, definitions disclosed)

`python eval.py` scores the engine against the generator's ground truth
and prints the formulas it uses (P = TP/(TP+FP), R = TP/(TP+FN)):

```
1. Field extraction (micro, 870 labeled fields, 10 shipments x 8 docs)
   precision 1.000 · recall 1.000 · F1 1.000   [TP 870 / FP 0 / FN 0]
   per family: identifiers 1.000 · parties 1.000 · values 1.000 ·
               classification 1.000 · quantities 1.000 · routing 1.000
2. Discrepancy detection (12 injected, 12 check types)
   precision 1.000 · recall 1.000 · F1 1.000   [TP 12 / FP 0 / FN 0]
   (documented cascades listed separately, not hidden)
3. Citation coverage 10/10 · citation correctness 10/10
   (a citation counts only if the cited page's stored text contains the
   source value — checked programmatically, not by the model)
4. Latency: mean 1 ms · p95 8 ms per question (offline); one full pack
   end-to-end in 0.27 s  [target < 60 s]
```

### Honest limitations

- **Synthetic, machine-generated PDFs only.** The corpus is 10 shipments
  produced by this repo's own generator; layouts are clean and text is
  extractable. Perfect scores reflect that controlled setting — they are
  an upper bound, not a field-performance claim.
- **No OCR.** Scanned or photographed documents are out of scope for v1.0
  (pdfplumber reads embedded text layers; text-free PDFs are also not
  citable).
- **No hidden test split yet**, and no adversarial cases (broken tables,
  duplicate docs, multi-currency, lb-vs-kg) — planned for v1.1.
- **Confidence scores are heuristic** (parse quality, retrieval
  similarity) — decomposed by subsystem, but not calibrated
  probabilities of truth.
- **Impact figures**: only exact/duty deltas are firm; flat estimates are
  labeled potential exposure and need broker review. The duty-rate table
  is a demo fixture, not a live HTSUS lookup.
- **Not a compliance authority.** The engine flags possible gaps,
  preserves evidence, and routes exceptions to a named human reviewer. It
  never files, and it makes no legal-sufficiency judgments.

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

Built for a domain where a wrong filing has legal consequences:

- **Synthetic data only.** Every company, vessel, container and value is
  invented by the generator. No real customer or employer data anywhere.
- **Human-in-the-loop by construction.** Reports can only be produced in
  `PENDING_HUMAN_REVIEW`; the single path to `APPROVED_BY_HUMAN` is an
  explicit approval with a named reviewer, recorded in the audit trail.
  The engine never files anything and never asserts filing sufficiency.
- **Full traceability.** Every decision — each extraction, each rule
  (passes included), each answer, each approval — is appended to
  `trace.jsonl` with a run ID.
- **Citations from evidence, not prose.** Offline-mode citations are
  emitted from stored extraction objects and index metadata; live-mode
  answers are built from tool results with a citation contract enforced by
  retry, and eval verifies cited pages actually contain the source values.
- **Local by default.** Embeddings are computed locally (zero model
  downloads); the only external call is the optional model provider in
  live mode.

## Running the tests

```bash
pip install pytest   # dev-only dependency
python -m pytest tests/ -v
```

The suite covers extraction vs ground truth, every reconciliation rule
(clean-pack controls and tolerance boundaries included), ISF/HTS/origin
compliance, citation guarantees, the impact-kind policy, the provider
abstraction (mocked), the REST API, and the approval gate.

## Tech

Python 3.11+ · [FastAPI](https://fastapi.tiangolo.com) + OpenAPI ·
[Anthropic Claude API](https://docs.claude.com) (default provider, tool
use) with OpenAI / Azure OpenAI adapters (Chat Completions — no deprecated
Assistants API) · [ChromaDB](https://www.trychroma.com) with local hashed
n-gram embeddings (swap-in point for any embedding model in
`tools/embeddings.py`) · [pdfplumber](https://github.com/jsvine/pdfplumber)
· [ReportLab](https://www.reportlab.com) (synthetic data generator).

## Roadmap (v1.1)

- Hybrid retrieval (exact-term + vector fusion) and optional reranking
- Expanded dataset with a hidden test split and adversarial cases
  (scans → OCR, duplicate/contradictory docs, multi-currency, unit
  conversions, no-answer questions, prompt-injection text inside a doc)
- Answer-faithfulness evaluation on the live path
- Optional MCP adapter over the REST API

---

Part of a 4-agent portfolio for AI-powered logistics automation.
See also: [Import Desk Automation Agent](https://github.com/anthonydenis01/import-desk-automation-agent),
[Container Visibility Agent](https://github.com/anthonydenis01/container-visibility-agent) and
[Carrier Scorecard Agent](https://github.com/anthonydenis01/carrier-scorecard-agent).
