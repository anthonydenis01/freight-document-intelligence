"""Retrieval-augmented Q&A over the document packs — citations mandatory.

Two modes, mirroring the rest of the portfolio:

* offline  — deterministic: direct field lookups (and the declared-value vs
             invoice-total comparison) are answered from extracted fields;
             anything else returns the top retrieved passages. Both carry
             citations (document + page) and a confidence score.
* live     — a Claude tool-use loop with `search_documents` and
             `get_shipment_fields` tools. The system prompt hard-requires a
             [document, p.N] citation on every factual claim; answers
             without citations are rejected and retried.

No answer leaves this module without at least one citation. If nothing
relevant is retrieved, the honest answer is "not found in the document
pack" — never a guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from tools import indexer
from tools.pdf_extract import ExtractedDoc

SHIPMENT_RE = re.compile(r"\b([A-Z]{4}-\d{4})\b")

# question keyword -> (field, preferred doc order)
FIELD_SYNONYMS: dict[str, tuple[str, list[str]]] = {
    "declared value": ("declared_value", ["customs_entry"]),
    "entered value": ("declared_value", ["customs_entry"]),
    "invoice total": ("invoice_total", ["commercial_invoice"]),
    "container": ("container_number", ["bill_of_lading", "arrival_notice", "packing_list"]),
    "seal": ("seal_number", ["bill_of_lading", "arrival_notice"]),
    "vessel": ("vessel", ["bill_of_lading", "arrival_notice", "customs_entry"]),
    "voyage": ("voyage", ["bill_of_lading", "arrival_notice"]),
    "eta": ("eta", ["arrival_notice", "customs_entry"]),
    "gross weight": ("gross_weight_kg", ["bill_of_lading", "packing_list", "arrival_notice"]),
    "net weight": ("net_weight_kg", ["packing_list"]),
    "hts": ("hts_code", ["commercial_invoice", "customs_entry", "isf_filing"]),
    "tariff": ("hts_code", ["commercial_invoice", "customs_entry"]),
    "origin": ("country_of_origin", ["certificate_of_origin", "commercial_invoice", "customs_entry"]),
    "consignee": ("consignee_name", ["bill_of_lading", "arrival_notice"]),
    "importer": ("importer_of_record", ["customs_entry", "isf_filing"]),
    "packages": ("package_count", ["bill_of_lading", "packing_list"]),
    "carton": ("package_count", ["packing_list", "bill_of_lading"]),
    "b/l": ("bl_number", ["bill_of_lading"]),
    "bill of lading number": ("bl_number", ["bill_of_lading"]),
    "duty": ("estimated_duty", ["customs_entry"]),
    "entry number": ("entry_number", ["customs_entry"]),
    "seller": ("shipper_name", ["commercial_invoice", "bill_of_lading"]),
    "shipper": ("shipper_name", ["bill_of_lading"]),
}


@dataclass
class Answer:
    """A cited answer with confidence decomposed by subsystem.

    Confidence is heuristic, not a probability of truth:
    - extraction: min per-field parse confidence of the fields used
    - retrieval: top passage similarity (when retrieval was involved)
    - answer: overall heuristic = min of the available components
    Each component is None when that subsystem wasn't involved.
    """
    question: str
    text: str
    citations: list[dict] = field(default_factory=list)
    extraction_confidence: float | None = None
    retrieval_confidence: float | None = None
    answer_confidence: float = 0.0
    mode: str = "offline"

    @property
    def confidence(self) -> float:  # backward-compatible overall score
        return self.answer_confidence

    def confidences(self) -> dict:
        return {"extraction": self.extraction_confidence,
                "retrieval": self.retrieval_confidence,
                "answer": self.answer_confidence}

    def render(self) -> str:
        cites = "; ".join(
            f"[{c['doc_type']} p.{c['page']} — {Path(c['file']).name}]" for c in self.citations
        ) or "[no source — answer withheld]"
        parts = [f"answer {self.answer_confidence:.2f}"]
        if self.extraction_confidence is not None:
            parts.append(f"extraction {self.extraction_confidence:.2f}")
        if self.retrieval_confidence is not None:
            parts.append(f"retrieval {self.retrieval_confidence:.2f}")
        return (f"{self.text}\n  sources: {cites}\n"
                f"  confidence (heuristic): {' · '.join(parts)}")


def _fmt(field_name: str, value) -> str:
    if field_name.endswith("_kg"):
        return f"{float(value):,.1f} kg"
    if field_name in ("declared_value", "invoice_total", "estimated_duty"):
        return f"${float(value):,.2f}"
    return str(value)


def _cite(doc: ExtractedDoc, fname: str) -> dict:
    f = doc.fields[fname]
    return {"doc_type": doc.doc_type, "file": doc.file, "page": f.page}


def _field_lookups(question: str, pack: dict[str, ExtractedDoc]) -> list[tuple[str, ExtractedDoc]]:
    q = question.lower()
    hits, seen = [], set()
    for keyword, (fname, doc_order) in FIELD_SYNONYMS.items():
        if keyword in q and fname not in seen:
            for dt in doc_order:
                if dt in pack and fname in pack[dt].fields:
                    hits.append((fname, pack[dt]))
                    seen.add(fname)
                    break
    return hits


def answer_offline(question: str, col, packs: dict[str, dict[str, ExtractedDoc]],
                   top_k: int = 4, trace=None) -> Answer:
    sid_match = SHIPMENT_RE.search(question.upper())
    sid = sid_match.group(1) if sid_match and sid_match.group(1) in packs else None

    # 1) structured field answer when the question names a shipment
    if sid:
        lookups = _field_lookups(question, packs[sid])
        if lookups:
            parts, cites, confs = [], [], []
            for fname, doc in lookups:
                f = doc.fields[fname]
                parts.append(f"{fname.replace('_', ' ')} = {_fmt(fname, f.value)} "
                             f"(per {doc.doc_type.replace('_', ' ')})")
                cites.append(_cite(doc, fname))
                confs.append(f.confidence)
            text = f"{sid}: " + "; ".join(parts) + "."
            if {"declared_value", "invoice_total"} <= {f for f, _ in lookups}:
                dv = next(d.fields["declared_value"].value for f, d in lookups if f == "declared_value")
                it = next(d.fields["invoice_total"].value for f, d in lookups if f == "invoice_total")
                delta = round(float(it) - float(dv), 2)
                text += (f" Difference: ${delta:,.2f}"
                         + (" — MISMATCH, review before filing." if abs(delta) > 1 else " (values agree)."))
            ec = round(min(confs), 2)
            ans = Answer(question, text, cites, extraction_confidence=ec,
                         answer_confidence=ec, mode="offline")
            if trace:
                trace.log("rag.answer", mode="offline-structured", question=question,
                          shipment=sid, citations=len(cites), **ans.confidences())
            return ans

    # 2) retrieval answer
    hits = indexer.search(col, question, top_k=top_k, shipment_id=sid)
    hits = [h for h in hits if h["similarity"] > 0.05]
    if not hits:
        ans = Answer(question, "Not found in the document pack — no relevant passage retrieved.",
                     [], answer_confidence=0.0, mode="offline")
    else:
        best = hits[0]
        snippet_lines = [ln for ln in best["text"].splitlines()[1:] if ln.strip()][:6]
        others = "".join(
            f"\n  also relevant: {h['meta']['doc_type']} p.{h['meta']['page']} "
            f"({h['meta']['shipment_id']}, sim {h['similarity']:.2f})" for h in hits[1:3]
        )
        text = ("Most relevant passage "
                f"({best['meta']['shipment_id']}, {best['meta']['doc_type'].replace('_', ' ')}):\n  "
                + "\n  ".join(snippet_lines) + others)
        rc = hits[0]["similarity"]
        ans = Answer(question, text,
                     [{"doc_type": h["meta"]["doc_type"], "file": h["meta"]["file"],
                       "page": h["meta"]["page"]} for h in hits[:3]],
                     retrieval_confidence=rc, answer_confidence=rc, mode="offline")
    if trace:
        trace.log("rag.answer", mode="offline-retrieval", question=question,
                  citations=len(ans.citations), **ans.confidences())
    return ans


# ---------------------------------------------------------------------------
# Claude live mode
# ---------------------------------------------------------------------------

RAG_SYSTEM_PROMPT = """\
You are a freight-document intelligence analyst for a customs brokerage.
You answer questions about a shipment document pack using ONLY the two
tools provided. Rules, non-negotiable:

1. Every factual claim in your answer MUST carry a citation in the form
   [doc_type p.N] taken from tool results. No citation, no claim.
2. If the tools return nothing relevant, answer exactly:
   "Not found in the document pack." Do not guess, ever.
3. When two documents disagree, surface the disagreement — do not pick one.
4. End with one line: "confidence: 0.xx" reflecting retrieval quality and
   field extraction confidence from the tool results.
Keep answers under 120 words."""

RAG_TOOLS = [
    {
        "name": "search_documents",
        "description": "Semantic search across all indexed document pages. Returns passages with shipment_id, doc_type, page and similarity.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "shipment_id": {"type": "string", "description": "Optional filter like MSKU-2847"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_shipment_fields",
        "description": "Return the structured fields extracted from every document of one shipment, with page numbers and per-field confidence.",
        "input_schema": {
            "type": "object",
            "properties": {"shipment_id": {"type": "string"}},
            "required": ["shipment_id"],
        },
    },
]


def answer_live(question: str, col, packs: dict[str, dict[str, ExtractedDoc]],
                provider, trace=None, max_turns: int = 6) -> Answer:
    """Provider-agnostic live Q&A (Claude / OpenAI / Azure via tools/providers.py).

    The provider only composes the answer; retrieval and field access run
    through the same deterministic tools as offline mode, and the citation
    contract is enforced with a retry.
    """
    from tools.providers import run_tool_loop

    citations: list[dict] = []
    retrieval_top: list[float] = []
    extraction_confs: list[float] = []

    def tool_search(query: str, shipment_id: str | None = None):
        hits = indexer.search(col, query, top_k=5, shipment_id=shipment_id)
        if hits:
            retrieval_top.append(hits[0]["similarity"])
        for h in hits[:3]:
            citations.append({"doc_type": h["meta"]["doc_type"], "file": h["meta"]["file"],
                              "page": h["meta"]["page"]})
        if trace:
            trace.log("rag.tool", tool="search_documents", query=query, hits=len(hits))
        return [{"shipment_id": h["meta"]["shipment_id"], "doc_type": h["meta"]["doc_type"],
                 "page": h["meta"]["page"], "similarity": h["similarity"],
                 "text": h["text"][:900]} for h in hits]

    def tool_fields(shipment_id: str):
        pack = packs.get(shipment_id.upper(), {})
        for doc in pack.values():
            extraction_confs.extend(f.confidence for f in doc.fields.values())
        if trace:
            trace.log("rag.tool", tool="get_shipment_fields", shipment=shipment_id, found=bool(pack))
        return {dt: {n: {"value": f.value, "page": f.page, "confidence": f.confidence}
                     for n, f in doc.fields.items()} for dt, doc in pack.items()}

    final_text = run_tool_loop(
        provider, RAG_SYSTEM_PROMPT, question, RAG_TOOLS,
        {"search_documents": tool_search, "get_shipment_fields": tool_fields},
        max_turns=max_turns, max_tokens=700,
        require_marker="[",
        retry_prompt="Your answer lacked [doc_type p.N] citations. "
                     "Re-answer with a citation on every claim.",
    ) or "Not found in the document pack."

    conf_m = re.search(r"confidence:\s*([01]\.\d+)", final_text)
    model_conf = float(conf_m.group(1)) if conf_m else (0.8 if citations else 0.0)
    rc = max(retrieval_top) if retrieval_top else None
    ec = round(min(extraction_confs), 2) if extraction_confs else None
    overall = min([v for v in (model_conf, rc, ec) if v is not None] or [0.0])
    ans = Answer(question, final_text, citations[:4],
                 extraction_confidence=ec, retrieval_confidence=rc,
                 answer_confidence=round(overall, 2), mode=f"live/{provider.name}")
    if trace:
        trace.log("rag.answer", mode=ans.mode, question=question,
                  citations=len(ans.citations), **ans.confidences())
    return ans
