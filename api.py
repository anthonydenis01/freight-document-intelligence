#!/usr/bin/env python3
"""Freight Document Intelligence — REST API (API-first core).

The same deterministic engine as the CLI, exposed as a small, stable service
contract so any client can consume it: Microsoft Copilot Studio (REST tools
via OpenAPI), ChatGPT actions (OpenAPI), Claude (tool use), Teams bots, or
plain internal systems. The OpenAPI schema is auto-generated at /openapi.json
(a committed copy lives in docs/openapi.json).

Design rules:
- Responses are compact (IDs, fields, citations, findings) — no raw PDF
  dumps — to stay well under connector payload limits (Copilot Studio
  documents a 500 KB response limit for connector actions).
- Everything filing-related is decision-support only: reports are always
  PENDING_HUMAN_REVIEW until POST /shipments/{id}/review records a named
  human reviewer.
- The model provider (Claude / OpenAI / Azure) is only used by /ask when
  mode=live; every other endpoint is fully deterministic.

Run locally:
    uvicorn api:app --reload
    open http://127.0.0.1:8000/docs
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from tools import indexer, rag
from tools.actions import approve_action, build_actions, send_action
from tools.compliance import run_compliance
from tools.governance import ReviewGate, TraceLog
from tools.pdf_extract import extract_pack
from tools.providers import ProviderError, get_provider
from tools.reconcile import reconcile_pack
from tools.reporting import build_report

DATA_DIR = Path("data/shipments")
CHROMA_DIR = ".chroma"


from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _load()
    yield


app = FastAPI(
    lifespan=_lifespan,
    title="Freight Document Intelligence API",
    version="1.0.0",
    description=(
        "Evidence-backed freight-document intelligence engine: structured "
        "extraction, cited Q&A, cross-document reconciliation and compliance "
        "checks over shipment document packs. Decision-support only — nothing "
        "is filed without a recorded human approval. All bundled data is "
        "synthetic."
    ),
)

# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

class _State:
    packs: dict = {}
    col = None
    trace: TraceLog | None = None
    approvals: dict[str, str] = {}


S = _State()


def _load(rebuild_index: bool = False) -> None:
    S.trace = S.trace or TraceLog("trace.jsonl")
    S.packs = {}
    if DATA_DIR.exists():
        for pack_dir in sorted(DATA_DIR.iterdir()):
            if pack_dir.is_dir():
                S.packs[pack_dir.name] = extract_pack(pack_dir)
    docs = [d for pack in S.packs.values() for d in pack.values()]
    if docs:
        if rebuild_index:
            S.col = indexer.build_index(docs, persist_dir=CHROMA_DIR)
        else:
            try:
                S.col = indexer.load_index(persist_dir=CHROMA_DIR)
            except Exception:
                S.col = indexer.build_index(docs, persist_dir=CHROMA_DIR)
    S.trace.log("api.load", shipments=len(S.packs), rebuilt=rebuild_index)


def _pack_or_404(shipment_id: str):
    pack = S.packs.get(shipment_id.upper())
    if not pack:
        raise HTTPException(404, f"Shipment '{shipment_id}' not found")
    return pack


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------

class AskRequest(BaseModel):
    question: str = Field(..., examples=["Declared value vs invoice total for MSKU-2847?"])
    shipment_id: str | None = Field(None, description="Optional filter, e.g. MSKU-2847")
    mode: str = Field("offline", description="offline (deterministic) | live (model provider)")
    provider: str | None = Field(None, description="anthropic | openai | azure (live mode)")


class ReviewRequest(BaseModel):
    reviewer: str = Field(..., description="Named human reviewer — required, no auto-approve")
    note: str = ""


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------

@app.get("/health", summary="Service health + corpus size")
def health():
    return {"status": "ok", "shipments": len(S.packs),
            "indexed_pages": S.col.count() if S.col else 0}


@app.get("/shipments", summary="List shipment packs")
def list_shipments():
    return [{"shipment_id": sid, "documents": sorted(pack.keys())}
            for sid, pack in S.packs.items()]


@app.get("/shipments/{shipment_id}/fields", summary="Extracted fields with provenance")
def get_fields(shipment_id: str):
    pack = _pack_or_404(shipment_id)
    return {dt: {n: {"value": f.value, "page": f.page, "confidence": f.confidence,
                     "file": Path(doc.file).name}
                 for n, f in doc.fields.items()}
            for dt, doc in pack.items()}


@app.post("/ask", summary="Cited Q&A across the document packs")
def ask(req: AskRequest):
    if S.col is None:
        raise HTTPException(503, "No documents indexed — POST /packs/reindex first")
    q = req.question if not req.shipment_id else f"{req.question} ({req.shipment_id})"
    t0 = time.time()
    if req.mode == "live":
        try:
            provider = get_provider(req.provider)
        except ProviderError as e:
            raise HTTPException(400, f"Provider error: {e}") from e
        ans = rag.answer_live(q, S.col, S.packs, provider=provider, trace=S.trace)
    else:
        ans = rag.answer_offline(q, S.col, S.packs, trace=S.trace)
    return {"question": req.question, "answer": ans.text, "citations": ans.citations,
            "confidence": ans.confidences(), "mode": ans.mode,
            "latency_ms": round((time.time() - t0) * 1000)}


@app.post("/shipments/{shipment_id}/reconcile", summary="Run the 12 cross-document checks")
def reconcile(shipment_id: str):
    pack = _pack_or_404(shipment_id)
    findings = reconcile_pack(pack, shipment_id.upper(), trace=S.trace)
    return {"shipment_id": shipment_id.upper(), "checks_run": 12,
            "mismatches": len(findings), "findings": [f.to_dict() for f in findings]}


@app.get("/shipments/{shipment_id}/checklist", summary="Compliance checklist (ISF / HTS / origin)")
def checklist(shipment_id: str):
    pack = _pack_or_404(shipment_id)
    items = run_compliance(pack, shipment_id.upper(), trace=S.trace)
    return {"shipment_id": shipment_id.upper(),
            "failed": sum(1 for c in items if c.status == "FAIL"),
            "items": [c.to_dict() for c in items]}


@app.get("/shipments/{shipment_id}/report", summary="Full audit report (reconciliation + compliance)")
def report(shipment_id: str):
    pack = _pack_or_404(shipment_id)
    sid = shipment_id.upper()
    findings = reconcile_pack(pack, sid, trace=S.trace)
    items = run_compliance(pack, sid, trace=S.trace)
    gate = ReviewGate(S.trace)
    status = ReviewGate.APPROVED if sid in S.approvals else gate.status
    follow_ups = build_actions(pack, sid, findings, items, trace=S.trace)
    rep = build_report(sid, findings, items, status, S.trace.run_id, actions=follow_ups)
    if sid in S.approvals:
        rep["approved_by"] = S.approvals[sid]
    return rep


@app.get("/shipments/{shipment_id}/actions",
         summary="Proposed follow-up actions with draft emails")
def list_actions(shipment_id: str):
    pack = _pack_or_404(shipment_id)
    sid = shipment_id.upper()
    findings = reconcile_pack(pack, sid, trace=S.trace)
    items = run_compliance(pack, sid, trace=S.trace)
    acts = build_actions(pack, sid, findings, items, trace=S.trace)
    return {"shipment_id": sid, "actions": [a.to_dict() for a in acts]}


@app.post("/actions/{action_id}/approve",
          summary="One-click human approval of a follow-up action")
def approve_follow_up(action_id: str, req: ReviewRequest):
    try:
        status = approve_action(action_id, req.reviewer, trace=S.trace)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return {"action_id": action_id, "status": status, "reviewer": req.reviewer}


@app.post("/actions/{action_id}/send",
          summary="Simulated dispatch (requires prior approval; sends nothing)")
def send_follow_up(action_id: str):
    try:
        status = send_action(action_id, trace=S.trace)
    except PermissionError as e:
        raise HTTPException(409, str(e)) from e
    return {"action_id": action_id, "status": status,
            "note": "No real email sent — copy the draft from the action into your mail client."}


@app.post("/shipments/{shipment_id}/review", summary="Record a named human approval")
def review(shipment_id: str, req: ReviewRequest):
    _pack_or_404(shipment_id)
    gate = ReviewGate(S.trace)
    try:
        gate.approve(req.reviewer, note=f"shipment {shipment_id.upper()} {req.note}".strip())
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    S.approvals[shipment_id.upper()] = req.reviewer
    return {"shipment_id": shipment_id.upper(), "status": gate.status,
            "reviewer": req.reviewer, "recorded_in": "trace.jsonl"}


@app.post("/packs", summary="Upload a new shipment pack (PDFs)")
async def upload_pack(shipment_id: str, files: list[UploadFile] = File(...)):
    sid = shipment_id.upper()
    dest = DATA_DIR / sid
    dest.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        if not (f.filename or "").lower().endswith(".pdf"):
            raise HTTPException(422, f"Only PDF files accepted (got {f.filename})")
        data = await f.read()
        if len(data) > 20 * 1024 * 1024:
            raise HTTPException(413, f"{f.filename} exceeds the 20 MB per-file limit")
        (dest / Path(f.filename).name).write_bytes(data)
        saved.append(Path(f.filename).name)
    _load(rebuild_index=True)
    return {"shipment_id": sid, "saved": saved,
            "extracted_documents": sorted(S.packs.get(sid, {}).keys())}


@app.post("/packs/reindex", summary="Re-extract and re-index every pack")
def reindex():
    _load(rebuild_index=True)
    return {"shipments": len(S.packs), "indexed_pages": S.col.count() if S.col else 0}
