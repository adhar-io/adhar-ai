"""Embedding backends.

Primary: the LLM gateway's `/v1/embeddings`, so one configured key drives both
chat and retrieval. The base URL is normalized the same way the agent loop
normalizes it, so the platform's `.../adhar-ai-gateway:8080/v1` and a bare local
`http://gateway:8080` both resolve to one `/v1/embeddings`.

Fallback: local sentence-transformers when the provider has
no embeddings endpoint (Anthropic) or no key is set — kept an OPTIONAL extra so
the base image stays small.
"""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

from ..config import openai_v1_base

log = logging.getLogger("adhar_ai.rag")

#: Matches the `vector(1536)` column the platform's CNPG bootstrap SQL creates.
EMBEDDING_DIM = 1536
LOCAL_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class EmbeddingBackend(Protocol):
    name: str
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _fit(vector: list[float], dim: int = EMBEDDING_DIM) -> list[float]:
    """Pad or truncate to the column width.

    A smaller local model (384-d MiniLM) is zero-padded into the 1536-d column
    so the schema the platform ships stays valid. Cosine similarity is unchanged
    by zero padding, so retrieval quality is unaffected — but never mix vectors
    from two different backends in one index.
    """
    if len(vector) == dim:
        return vector
    if len(vector) > dim:
        return vector[:dim]
    return vector + [0.0] * (dim - len(vector))


class GatewayEmbeddings:
    name = "gateway"
    dim = EMBEDDING_DIM

    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_base = openai_v1_base(base_url)
        self._client = client

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=120.0)
        return self._client

    async def embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self._http().post(f"{self.api_base}/embeddings", json={"input": texts})
        resp.raise_for_status()
        rows = sorted(resp.json().get("data") or [], key=lambda d: int(d.get("index", 0)))
        return [_fit([float(v) for v in row["embedding"]]) for row in rows]

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class LocalEmbeddings:
    """sentence-transformers fallback. Requires the `local-embeddings` extra."""

    name = "local"
    dim = EMBEDDING_DIM

    def __init__(self, model_name: str = LOCAL_MODEL) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ModuleNotFoundError as exc:  # pragma: no cover - optional extra
            raise RuntimeError(
                "local embeddings need the optional extra: "
                "`uv sync --extra local-embeddings` (or pip install 'adhar-ai[local-embeddings]')"
            ) from exc
        self._model = SentenceTransformer(model_name)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(texts, normalize_embeddings=True)
        return [_fit([float(v) for v in row]) for row in vectors]


async def load_embeddings(gateway_url: str) -> EmbeddingBackend:
    """Probe the gateway; fall back to local only if it cannot embed."""
    if gateway_url:
        backend = GatewayEmbeddings(gateway_url)
        try:
            await backend.embed(["adhar ai readiness probe"])
            return backend
        except Exception as exc:
            log.info("gateway embeddings unavailable (%s); trying local fallback", exc)
            await backend.aclose()
    return LocalEmbeddings()
