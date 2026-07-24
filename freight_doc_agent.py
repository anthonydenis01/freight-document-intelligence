#!/usr/bin/env python3
"""Freight Document Intelligence Agent.

Ask your shipping documents anything — and catch compliance gaps before
customs does. RAG-powered Q&A + cross-document reconciliation + compliance
checklist over a shipment's document pack (B/L, commercial invoice, packing
list, certificate of origin, ISF, customs entry, arrival notice, delivery
order).

Run modes (matching the rest of the portfolio):
  --offline   Deterministic pipeline + extractive Q&A. No API key needed.
  (default)   Claude orchestrates retrieval/reconciliation via tool use and
              writes cited answers / an executive audit summary.
              Requires ANTHROPIC_API_KEY.

Usage:
    python freight_doc_agent.py ingest
    python freight_doc_agent.py ask "Declared value vs invoice total for MSKU-2847?" --offline
    python freight_doc_agent.py audit --offline
    python freight_doc_agent.py audit --shipment MSKU-2847 --offline
    python freight_doc_agent.py approve --shipment MSKU-2847 --reviewer "A. Denis"

Every decision is appended to trace.jsonl (full audit trail) and every
report ends in PENDING_HUMAN_REVIEW — the agent never files anything.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from tools import indexer, rag
from tools.actions import approve_action, build_actions, send_action
from tools.compliance import run_compliance
from tools.governance import ReviewGate, TraceLog
from tools.pdf_extract import extract_pack
from tools.providers import ProviderError, complete, get_provider
from tools.reconcile import reconcile_pack
from tools.reporting import build_report, print_report

DATA_DIR = Path("data/shipments")
REPORT_DIR = Path("reports")


# ---------------------------------------------------------------------------
# Pipeline steps (deterministic core)
# ---------------------------------------------------------------------------

def load_packs(data_dir: Path, trace: TraceLog, shipment: str | None = None) -> dict:
    if not data_dir.exists():
        print(f"No document packs found at {data_dir}/.")
        print("Generate the synthetic set first:  python data/generate_sample_data.py")
        sys.exit(2)
    packs = {}
    for pack_dir in sorted(data_dir.iterdir()):
        if not pack_dir.is_dir():
            continue
        if shipment and pack_dir.name != shipment:
            continue
        pack = extract_pack(pack_dir)
        packs[pack_dir.name] = pack
        n_fields = sum(len(d.fields) for d in pack.values())
        trace.log("extract.pack", shipment=pack_dir.name, documents=len(pack), fields=n_fields)
    if not packs:
        print(f"Shipment '{shipment}' not found under {data_dir}/.")
        sys.exit(2)
    return packs


def cmd_ingest(args, trace: TraceLog) -> None:
    t0 = time.time()
    packs = load_packs(Path(args.data), trace)
    docs = [d for pack in packs.values() for d in pack.values()]
    col = indexer.build_index(docs, persist_dir=args.chroma)
    trace.log("index.build", chunks=col.count(), shipments=len(packs))
    print(f"Indexed {col.count()} pages from {len(packs)} shipment packs "
          f"in {time.time() - t0:.1f}s → {args.chroma}/")


def cmd_ask(args, trace: TraceLog) -> None:
    packs = load_packs(Path(args.data), trace)
    try:
        col = indexer.load_index(persist_dir=args.chroma)
    except Exception:
        docs = [d for pack in packs.values() for d in pack.values()]
        col = indexer.build_index(docs, persist_dir=args.chroma)
        trace.log("index.build", chunks=col.count(), shipments=len(packs))
    t0 = time.time()
    if args.offline:
        ans = rag.answer_offline(args.question, col, packs, trace=trace)
    else:
        provider = _get_provider_or_exit(args)
        ans = rag.answer_live(args.question, col, packs, provider=provider, trace=trace)
    print(ans.render())
    print(f"  latency: {time.time() - t0:.2f}s ({ans.mode} mode)")


def run_audit(packs: dict, trace: TraceLog) -> list[dict]:
    gate = ReviewGate(trace)
    reports = []
    for sid, pack in packs.items():
        findings = reconcile_pack(pack, sid, trace=trace)
        compliance = run_compliance(pack, sid, trace=trace)
        follow_ups = build_actions(pack, sid, findings, compliance, trace=trace)
        report = build_report(sid, findings, compliance, gate.status, trace.run_id,
                              actions=follow_ups)
        reports.append(report)
    return reports


def cmd_audit(args, trace: TraceLog) -> None:
    t0 = time.time()
    packs = load_packs(Path(args.data), trace, shipment=args.shipment)
    reports = run_audit(packs, trace)
    REPORT_DIR.mkdir(exist_ok=True)
    for report in reports:
        out = REPORT_DIR / f"{report['shipment_id']}_audit.json"
        out.write_text(json.dumps(report, indent=2))
    total_findings = sum(r["reconciliation"]["mismatches"] for r in reports)
    total_fails = sum(r["compliance"]["failed"] for r in reports)
    elapsed = time.time() - t0

    if args.offline:
        for report in reports:
            print_report(report)
    else:
        provider = _get_provider_or_exit(args)
        _audit_summary_agent(reports, trace, provider)

    print(f"\n{'=' * 50}")
    print(f"Audited {len(reports)} shipment pack(s) in {elapsed:.1f}s — "
          f"{total_findings} reconciliation mismatch(es), {total_fails} compliance failure(s).")
    print(f"Reports: {REPORT_DIR}/  ·  Audit trail: trace.jsonl (run {trace.run_id})")
    print("Status: PENDING_HUMAN_REVIEW — nothing is filed without an approval "
          "(freight_doc_agent.py approve --reviewer \"Name\").")


def cmd_actions(args, trace: TraceLog) -> None:
    if args.subcmd == "approve":
        if not args.id:
            print("Missing --id (e.g. MSKU-2847:isf_completeness)")
            sys.exit(2)
        try:
            status = approve_action(args.id, args.reviewer or "", trace=trace)
        except ValueError as e:
            print(f"Refused: {e}")
            sys.exit(2)
        print(f"{args.id}: {status} by {args.reviewer} — recorded in trace.jsonl.")
        return
    if args.subcmd == "send":
        try:
            status = send_action(args.id, trace=trace)
        except PermissionError as e:
            print(f"Refused: {e}")
            sys.exit(2)
        print(f"{args.id}: {status} — no real email sent; copy the draft from the audit report.")
        return
    # list
    packs = load_packs(Path(args.data), trace, shipment=args.shipment)
    total = 0
    for sid, pack in packs.items():
        findings = reconcile_pack(pack, sid)
        compliance = run_compliance(pack, sid)
        for a in build_actions(pack, sid, findings, compliance):
            total += 1
            print(f"[{a.status}] {a.id}")
            print(f"    {a.summary} → {a.recipient_name} <{a.recipient_email}>")
            if args.drafts:
                print("    " + a.draft_email.replace("\n", "\n    ") + "\n")
    if not total:
        print("No follow-up actions — all audited packs are clean.")


def cmd_approve(args, trace: TraceLog) -> None:
    path = REPORT_DIR / f"{args.shipment}_audit.json"
    if not path.exists():
        print(f"No audit report for {args.shipment}. Run: python freight_doc_agent.py audit "
              f"--shipment {args.shipment}")
        sys.exit(2)
    report = json.loads(path.read_text())
    gate = ReviewGate(trace)
    gate.approve(args.reviewer, note=f"shipment {args.shipment}")
    report["status"] = gate.status
    report["approved_by"] = args.reviewer
    path.write_text(json.dumps(report, indent=2))
    print(f"{args.shipment}: {gate.status} by {args.reviewer} — recorded in trace.jsonl.")


# ---------------------------------------------------------------------------
# Claude orchestration layer (audit narrative)
# ---------------------------------------------------------------------------

AUDIT_SYSTEM_PROMPT = """\
You are a senior import-compliance analyst. You receive the JSON audit
reports (reconciliation findings + compliance checklist) for one or more
shipment document packs. Write a short executive summary (6-10 sentences)
for a customs-brokerage operations manager: lead with what would block
filing (CRITICAL first, with shipment IDs and dollar exposure), group the
rest by theme, note the clean shipments in one sentence, and close with the
single most urgent action. Cite documents as they appear in the findings'
citations (doc_type p.N). Do not invent anything not present in the JSON.
"""


def _get_provider_or_exit(args):
    try:
        return get_provider(getattr(args, "provider", None), getattr(args, "model", None))
    except ProviderError as e:
        print(f"Provider error: {e}")
        print("Run with --offline for the deterministic pipeline (no API key needed).")
        sys.exit(1)


def _audit_summary_agent(reports: list[dict], trace: TraceLog, provider) -> None:
    trace.log("agent.summary_request", provider=provider.name,
              shipments=[r["shipment_id"] for r in reports])
    text = complete(provider, AUDIT_SYSTEM_PROMPT, json.dumps(reports, default=str),
                    max_tokens=900)
    trace.log("agent.summary_written", provider=provider.name, characters=len(text))
    print(f"\n----- EXECUTIVE SUMMARY ({provider.name}) -----")
    print(text)


# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Freight Document Intelligence Agent")
    ap.add_argument("--data", default=str(DATA_DIR), help="Directory of shipment packs")
    ap.add_argument("--chroma", default=".chroma", help="ChromaDB persist directory")
    ap.add_argument("--offline", action="store_true",
                    help="Deterministic pipeline only — no model API call.")
    ap.add_argument("--provider", choices=["anthropic", "openai", "azure"],
                    help="Model provider for live mode (default: env FDI_PROVIDER or anthropic)")
    ap.add_argument("--model", help="Model name override (default: provider default / FDI_MODEL)")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("ingest", help="Extract + index every document pack")
    p_ask = sub.add_parser("ask", help="Ask a question across the document packs")
    p_ask.add_argument("question")
    p_audit = sub.add_parser("audit", help="Reconciliation + compliance audit")
    p_audit.add_argument("--shipment", help="Audit a single shipment, e.g. MSKU-2847")
    p_appr = sub.add_parser("approve", help="Record a human approval for an audited shipment")
    p_appr.add_argument("--shipment", required=True)
    p_appr.add_argument("--reviewer", required=True)
    p_act = sub.add_parser("actions", help="List / approve / simulate-send follow-up actions")
    p_act.add_argument("subcmd", choices=["list", "approve", "send"], nargs="?", default="list")
    p_act.add_argument("--shipment", help="Filter to one shipment")
    p_act.add_argument("--id", help="Action id, e.g. MSKU-2847:isf_completeness")
    p_act.add_argument("--reviewer", help="Named human reviewer (required for approve)")
    p_act.add_argument("--drafts", action="store_true", help="Print the draft emails")

    args = ap.parse_args()
    trace = TraceLog("trace.jsonl")
    trace.log("run.start", command=args.command, offline=bool(getattr(args, "offline", False)))

    {"ingest": cmd_ingest, "ask": cmd_ask, "audit": cmd_audit,
     "approve": cmd_approve, "actions": cmd_actions}[args.command](args, trace)


if __name__ == "__main__":
    main()
