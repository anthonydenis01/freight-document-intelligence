"""Local, dependency-free embedding function for ChromaDB.

Hashed character n-gram embeddings: deterministic, fully offline, zero model
downloads. On a corpus of structured freight documents — where queries lean
heavily on identifiers ("MSKU-2847", "declared value", "seal number") —
lexical n-gram similarity retrieves accurately without a neural model.

Swap-in point: any ChromaDB embedding function works. To use a neural model
instead, replace ``LocalHashEmbedding`` with e.g.::

    from chromadb.utils import embedding_functions
    ef = embedding_functions.SentenceTransformerEmbeddingFunction("all-MiniLM-L6-v2")

The rest of the pipeline is unchanged. We default to the local function so
the repo runs end-to-end with no network access and no GPU — a deliberate
governance choice: nothing leaves the machine at index or retrieval time.
"""
from __future__ import annotations

import hashlib
import math
import re

DIM = 512
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9./-]*")


def _ngrams(token: str, n_lo: int = 3, n_hi: int = 5):
    yield token  # whole token matters most for IDs like MSKU2847614
    if len(token) > n_lo:
        for n in range(n_lo, min(n_hi, len(token)) + 1):
            for i in range(len(token) - n + 1):
                yield token[i:i + n]


def embed_text(text: str) -> list[float]:
    vec = [0.0] * DIM
    tokens = _TOKEN_RE.findall(text.lower())
    for tok in tokens:
        for gram in _ngrams(tok):
            h = int.from_bytes(hashlib.blake2b(gram.encode(), digest_size=8).digest(), "big")
            idx = h % DIM
            sign = 1.0 if (h >> 63) & 1 else -1.0
            # whole tokens weighted above sub-grams
            weight = 2.0 if gram == tok else 1.0 / math.sqrt(len(tokens) + 1)
            vec[idx] += sign * weight
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class LocalHashEmbedding:
    """ChromaDB-compatible embedding function (implements __call__/name)."""

    def __call__(self, input: list[str]) -> list[list[float]]:  # noqa: A002 (chroma API)
        return [embed_text(t) for t in input]

    # chromadb >= 1.x calls these; both delegate to __call__
    def embed_documents(self, input: list[str]) -> list[list[float]]:  # noqa: A002
        return self(input)

    def embed_query(self, input: list[str]) -> list[list[float]]:  # noqa: A002
        return self(input)

    @staticmethod
    def name() -> str:
        return "local-hash-ngram-v1"

    def is_legacy(self) -> bool:
        return True
