"""Tests for the REST API surface and the model-provider abstraction.

Everything runs offline: the API tests exercise the deterministic engine
through FastAPI's TestClient, and the provider tests use a fake provider —
no API keys, no network.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import api
from tools.providers import ProviderError, StepResult, ToolCall, get_provider, run_tool_loop
from tools.reconcile import DUTY_DELTA, EXACT_DELTA, POTENTIAL


@pytest.fixture(scope="module")
def client():
    with TestClient(api.app) as c:
        yield c


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def test_health_reports_corpus(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["shipments"] == 10
    assert body["indexed_pages"] == 80


def test_ask_returns_citations_and_decomposed_confidence(client):
    r = client.post("/ask", json={
        "question": "What's the declared value vs invoice total for shipment MSKU-2847?"})
    body = r.json()
    assert r.status_code == 200
    assert "MISMATCH" in body["answer"]
    assert len(body["citations"]) == 2
    assert set(body["confidence"]) == {"extraction", "retrieval", "answer"}


def test_reconcile_labels_impact_kinds(client):
    body = client.post("/shipments/MSKU-2847/reconcile").json()
    kinds = {f["check"]: f["impact_kind"] for f in body["findings"]}
    assert kinds["declared_value_vs_invoice_total"] == EXACT_DELTA
    body2 = client.post("/shipments/TCLU-5015/reconcile").json()
    kinds2 = {f["check"]: f["impact_kind"] for f in body2["findings"]}
    assert kinds2["hts_consistency"] == DUTY_DELTA
    assert kinds2["gross_weight_consistency"] == POTENTIAL


def test_report_separates_quantified_from_potential(client):
    rep = client.get("/shipments/MSKU-2847/report").json()
    assert rep["quantified_delta_usd"] == pytest.approx(18068.40, abs=1.0)
    assert rep["potential_exposure_usd"] == pytest.approx(5000.0)
    # buckets are reported separately, never merged
    assert "estimated_exposure_usd" not in rep


def test_review_requires_named_reviewer(client):
    r = client.post("/shipments/TCLU-5015/review", json={"reviewer": "   "})
    assert r.status_code == 422
    r = client.post("/shipments/TCLU-5015/review", json={"reviewer": "A. Denis"})
    assert r.json()["status"] == "APPROVED_BY_HUMAN"
    assert client.get("/shipments/TCLU-5015/report").json()["approved_by"] == "A. Denis"


def test_unknown_shipment_404(client):
    assert client.get("/shipments/XXXX-0000/report").status_code == 404


# ---------------------------------------------------------------------------
# provider abstraction
# ---------------------------------------------------------------------------

class FakeProvider:
    """Scripted provider: calls a tool, then answers (optionally uncited)."""

    name = "fake"

    def __init__(self, answers):
        self.answers = list(answers)
        self.tool_results = []
        self.user_msgs = []
        self._called_tool = False

    def start(self, system, user_message, tools):
        self.system, self.tools = system, tools

    def step(self, max_tokens=800):
        if not self._called_tool:
            self._called_tool = True
            return StepResult(tool_calls=[ToolCall("t1", "search_documents",
                                                   {"query": "declared value"})])
        return StepResult(text=self.answers.pop(0))

    def add_tool_results(self, results):
        self.tool_results.extend(results)

    def add_user(self, text):
        self.user_msgs.append(text)


def test_run_tool_loop_executes_tools_and_returns_text():
    p = FakeProvider(["Declared value is $46,800 [customs_entry p.1]. confidence: 0.90"])
    out = run_tool_loop(p, "sys", "q", [], {"search_documents": lambda query: [{"hit": query}]})
    assert "[customs_entry p.1]" in out
    assert p.tool_results and p.tool_results[0][1] == [{"hit": "declared value"}]


def test_run_tool_loop_enforces_citation_contract():
    p = FakeProvider(["The value is 46800.",                       # uncited → retried
                      "The value is $46,800 [customs_entry p.1]."])
    out = run_tool_loop(p, "sys", "q", [], {"search_documents": lambda query: []},
                        require_marker="[", retry_prompt="cite your sources")
    assert "[customs_entry p.1]" in out
    assert p.user_msgs == ["cite your sources"]


def test_get_provider_fails_cleanly_without_keys(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "FDI_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ProviderError):
        get_provider("anthropic")
    with pytest.raises(ProviderError):
        get_provider("nonexistent")


def test_live_answer_is_provider_agnostic(client):
    """rag.answer_live must work with any provider implementing the protocol."""
    from tools import rag
    p = FakeProvider(["MSKU-2847 entered value $46,800.00 [customs_entry p.1]. confidence: 0.95"])
    ans = rag.answer_live("declared value MSKU-2847?", api.S.col, api.S.packs, provider=p)
    assert ans.mode == "live/fake"
    assert ans.citations, "citations must be captured from tool results"
    assert ans.answer_confidence <= 0.95
