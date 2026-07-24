"""Model-provider abstraction — business logic must survive model replacement.

Everything deterministic (parsing, normalization, reconciliation, severity,
audit logging) lives outside this module and never touches a model. The
provider layer is deliberately thin: one normalized chat-with-tools
interface, implemented for Anthropic (Claude) and OpenAI-compatible APIs
(OpenAI, Azure OpenAI, or any base_url-compatible server). OpenAI support
uses the Chat Completions tools API — not the deprecated Assistants API.

Selection (env or CLI --provider):
    FDI_PROVIDER = anthropic (default) | openai | azure
    FDI_MODEL    = model name override

    anthropic: ANTHROPIC_API_KEY
    openai:    OPENAI_API_KEY  (+ optional OPENAI_BASE_URL)
    azure:     AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT,
               AZURE_OPENAI_DEPLOYMENT (used as model), AZURE_OPENAI_API_VERSION

The normalized exchange format used by callers:
    messages: [{"role": "user"|"assistant"|"tool", ...}]
    step() returns StepResult(text, tool_calls=[ToolCall(id, name, arguments)])
    Callers append assistant turns and tool results via the helpers below,
    so the caller-side agent loop is provider-agnostic.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4.1",
    "azure": None,  # taken from AZURE_OPENAI_DEPLOYMENT
}


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class StepResult:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class ProviderError(RuntimeError):
    pass


# Tools are declared once, in a neutral schema:
#   {"name", "description", "input_schema": {json schema}}
# Adapters convert to each vendor's format.


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str | None = None):
        try:
            import anthropic
        except ImportError as e:
            raise ProviderError("pip install anthropic — or run with --offline") from e
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ProviderError("ANTHROPIC_API_KEY is not set (or run with --offline)")
        self.client = anthropic.Anthropic()
        self.model = model or DEFAULT_MODELS["anthropic"]
        self._history: list[dict] = []

    def start(self, system: str, user_message: str, tools: list[dict]):
        self.system = system
        self.tools = [
            {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
            for t in tools
        ]
        self._history = [{"role": "user", "content": user_message}]

    def step(self, max_tokens: int = 800) -> StepResult:
        resp = self.client.messages.create(
            model=self.model, max_tokens=max_tokens, system=self.system,
            tools=self.tools, messages=self._history,
        )
        self._history.append({"role": "assistant", "content": resp.content})
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in resp.content if b.type == "tool_use"]
        text = "".join(b.text for b in resp.content if b.type == "text")
        return StepResult(text=text, tool_calls=calls)

    def add_tool_results(self, results: list[tuple[ToolCall, Any]]):
        self._history.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": c.id, "content": json.dumps(r, default=str)}
            for c, r in results
        ]})

    def add_user(self, text: str):
        self._history.append({"role": "user", "content": text})


class OpenAIProvider:
    """OpenAI Chat Completions with tools. Also covers Azure OpenAI."""

    name = "openai"

    def __init__(self, model: str | None = None, azure: bool = False):
        try:
            import openai
        except ImportError as e:
            raise ProviderError("pip install openai — or run with --offline") from e
        if azure:
            endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
            key = os.environ.get("AZURE_OPENAI_API_KEY")
            if not (endpoint and key):
                raise ProviderError("AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY not set")
            self.client = openai.AzureOpenAI(
                azure_endpoint=endpoint, api_key=key,
                api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            )
            self.model = model or os.environ.get("AZURE_OPENAI_DEPLOYMENT")
            if not self.model:
                raise ProviderError("AZURE_OPENAI_DEPLOYMENT not set")
            self.name = "azure"
        else:
            if not os.environ.get("OPENAI_API_KEY"):
                raise ProviderError("OPENAI_API_KEY is not set (or run with --offline)")
            kwargs = {}
            if os.environ.get("OPENAI_BASE_URL"):
                kwargs["base_url"] = os.environ["OPENAI_BASE_URL"]
            self.client = openai.OpenAI(**kwargs)
            self.model = model or DEFAULT_MODELS["openai"]
        self._history: list[dict] = []

    def start(self, system: str, user_message: str, tools: list[dict]):
        self.tools = [{
            "type": "function",
            "function": {"name": t["name"], "description": t["description"],
                         "parameters": t["input_schema"]},
        } for t in tools]
        self._history = [{"role": "system", "content": system},
                         {"role": "user", "content": user_message}]

    def step(self, max_tokens: int = 800) -> StepResult:
        resp = self.client.chat.completions.create(
            model=self.model, max_tokens=max_tokens,
            tools=self.tools or None, messages=self._history,
        )
        msg = resp.choices[0].message
        self._history.append(msg.model_dump(exclude_none=True))
        calls = [ToolCall(tc.id, tc.function.name, json.loads(tc.function.arguments or "{}"))
                 for tc in (msg.tool_calls or [])]
        return StepResult(text=msg.content or "", tool_calls=calls)

    def add_tool_results(self, results: list[tuple[ToolCall, Any]]):
        for c, r in results:
            self._history.append({"role": "tool", "tool_call_id": c.id,
                                  "content": json.dumps(r, default=str)})

    def add_user(self, text: str):
        self._history.append({"role": "user", "content": text})


def get_provider(name: str | None = None, model: str | None = None):
    """Factory. name: anthropic | openai | azure (env FDI_PROVIDER if None)."""
    name = (name or os.environ.get("FDI_PROVIDER") or "anthropic").lower()
    model = model or os.environ.get("FDI_MODEL")
    if name == "anthropic":
        return AnthropicProvider(model)
    if name == "openai":
        return OpenAIProvider(model)
    if name == "azure":
        return OpenAIProvider(model, azure=True)
    raise ProviderError(f"Unknown provider '{name}' (anthropic | openai | azure)")


def complete(provider, system: str, user_message: str, max_tokens: int = 900) -> str:
    """Single completion, no tools — used for the audit executive summary."""
    provider.start(system, user_message, [])
    return provider.step(max_tokens=max_tokens).text


def run_tool_loop(provider, system: str, user_message: str, tools: list[dict],
                  tool_impls: dict, max_turns: int = 6, max_tokens: int = 800,
                  require_marker: str | None = None, retry_prompt: str | None = None) -> str:
    """Provider-agnostic agent loop.

    tool_impls: {tool_name: callable(**arguments) -> JSON-serializable}
    require_marker: if set, a final answer missing this substring triggers one
    retry with retry_prompt (used to enforce the citation contract).
    """
    provider.start(system, user_message, tools)
    retried = False
    for _ in range(max_turns):
        result = provider.step(max_tokens=max_tokens)
        if result.tool_calls:
            outs = []
            for call in result.tool_calls:
                impl = tool_impls.get(call.name)
                out = impl(**call.arguments) if impl else {"error": f"unknown tool {call.name}"}
                outs.append((call, out))
            provider.add_tool_results(outs)
            continue
        text = result.text
        if require_marker and require_marker not in text and retry_prompt and not retried:
            retried = True
            provider.add_user(retry_prompt)
            continue
        return text
    return ""
