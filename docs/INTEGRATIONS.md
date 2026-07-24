# Integrations — Copilot Studio, ChatGPT, Claude, internal systems

The engine is **API-first**: one REST service (`api.py`, OpenAPI schema in
[`docs/openapi.json`](openapi.json) and live at `/openapi.json`) that any
assistant platform consumes as a tool. The business logic — parsing,
normalization, reconciliation rules, severity policy, audit logging — never
depends on which model or platform sits in front of it.

```
SharePoint / folder   Teams / Copilot UI   ChatGPT action   Claude client   MCP adapter (optional)
          \                  |                   |               |               /
           \                 |                   |               |              /
            +----------- Freight Document Intelligence API (OpenAPI) ---------+
                                         |
              ingestion → extraction → field store + vector index
                                         |
              deterministic reconciliation + compliance checklist
                                         |
              cited Q&A  ·  audit report  ·  human review gate  ·  trace.jsonl
```

## Endpoints (v1.0)

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | health + corpus size |
| GET | `/shipments` | list packs |
| GET | `/shipments/{id}/fields` | extracted fields + page + confidence |
| POST | `/ask` | cited Q&A (offline deterministic, or live via provider) |
| POST | `/shipments/{id}/reconcile` | 12 cross-document checks |
| GET | `/shipments/{id}/checklist` | ISF / HTS / origin compliance |
| GET | `/shipments/{id}/report` | full audit report, exposure split |
| POST | `/shipments/{id}/review` | record named human approval |
| POST | `/packs` | upload a new pack (PDFs) |
| POST | `/packs/reindex` | re-extract + re-index |

Responses are deliberately compact (IDs, findings, citations — never raw
PDF dumps): Microsoft documents a 500 KB response limit for Copilot Studio
connector actions, and conversational tools work best on narrow evidence
windows anyway.

## Microsoft Copilot Studio

Copilot Studio consumes REST APIs as agent tools via OpenAPI (and custom
connectors with Entra auth for production tenants).

1. Deploy the API somewhere reachable by your tenant (App Service,
   Container Apps, or a tunnel for a demo).
2. In Copilot Studio: **Add tool → REST API**, import `docs/openapi.json`,
   set the host URL.
3. Expose `POST /ask`, `GET /shipments/{id}/report`, `POST /shipments/{id}/review`
   as actions; the schema descriptions double as action descriptions.
4. Production notes: put the API behind Entra ID (custom connector),
   respect tenant access controls for any SharePoint-sourced documents,
   and keep the human-review action gated to named users.

A tenant-dependent full Copilot deployment is client work, not part of
this repo — what this repo provides is the clean service contract that
makes it a configuration exercise instead of a rebuild.

## ChatGPT (GPT actions)

GPT actions require an OpenAPI spec and an auth scheme:

1. Create a GPT → **Actions → Import from URL** (`https://<host>/openapi.json`)
   or paste `docs/openapi.json`.
2. Set auth (API key header for a demo; OAuth for anything real).
3. Instructions for the GPT: "Answer freight-document questions only via
   the API's /ask and /report actions; always surface the returned
   citations; never state filing sufficiency — route to /review."

The engine does not use the deprecated Assistants API anywhere; OpenAI
integration is plain Chat Completions (live `--provider openai`) plus
these OpenAPI actions.

## Claude

Two options:

- **Tool use (built in):** the CLI's live mode already runs a Claude
  tool-use loop over `search_documents` / `get_shipment_fields`
  (`--provider anthropic`, the default).
- **Remote tools:** point Claude at the same REST endpoints, or wrap them
  in an MCP server later — MCP is an optional adapter once the API is
  stable, not a foundation (both Copilot Studio and Claude can consume MCP
  servers when that becomes useful).

## Model providers (live answer generation)

```bash
# Claude (default)
export ANTHROPIC_API_KEY=... && python freight_doc_agent.py ask "..." 
# OpenAI
export OPENAI_API_KEY=...    && python freight_doc_agent.py --provider openai ask "..."
# Azure OpenAI
export AZURE_OPENAI_API_KEY=... AZURE_OPENAI_ENDPOINT=... AZURE_OPENAI_DEPLOYMENT=...
python freight_doc_agent.py --provider azure ask "..."
# No provider at all — deterministic engine + extractive Q&A
python freight_doc_agent.py --offline ask "..."
```

## curl examples

```bash
uvicorn api:app &

curl -s localhost:8000/health
curl -s -X POST localhost:8000/ask \
  -H 'Content-Type: application/json' \
  -d '{"question": "Declared value vs invoice total for MSKU-2847?"}'
curl -s localhost:8000/shipments/MSKU-2847/report
curl -s -X POST localhost:8000/shipments/MSKU-2847/review \
  -H 'Content-Type: application/json' \
  -d '{"reviewer": "A. Denis", "note": "value discrepancy resolved with shipper"}'
```
