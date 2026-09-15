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
from collections.abc import Awaitable, Callable

#: Yields the bearer for the gateway — the runtime's service-account token.
TokenProvider = Callable[[], Awaitable[str]]

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

    def __init__(
        self,
        base_url: str,
        client: httpx.AsyncClient | None = None,
        token_provider: TokenProvider | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        #: Mints the bearer the gateway requires. Optional so a gateway with
        #: no JWT policy (the bundled local one) keeps working unchanged; when
        #: the provider yields an empty token no header is sent at all.
        self._token_provider = token_provider
        self.api_base = openai_v1_base(base_url)
        self._client = client
        #: Filled in from the gateway's own response. The knowledge store records
        #: it per row so that a change of embedding model BEHIND the gateway is
        #: detected and the affected rows are re-embedded — vectors from two
        #: models share no space, and mixing them degrades retrieval silently.
        self.model = ""

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=120.0)
        return self._client

    async def embed(self, texts: list[str]) -> list[list[float]]:
        headers: dict[str, str] = {}
        if self._token_provider is not None:
            token = await self._token_provider()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        resp = await self._http().post(
            f"{self.api_base}/embeddings", json={"input": texts}, headers=headers
        )
        resp.raise_for_status()
        payload = resp.json()
        if reported := str(payload.get("model") or ""):
            self.model = reported
        rows = sorted(payload.get("data") or [], key=lambda d: int(d.get("index", 0)))
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


async def load_embeddings(
    gateway_url: str, token_provider: TokenProvider | None = None
) -> EmbeddingBackend | None:
    """The best available embedder, or `None` if there is none.

    Returns `None` rather than raising, because "no embedder" is a degradation
    the knowledge base handles well and a crash is not. Without one, pgvector
    still answers from its full-text index over the SAME indexed corpus — the
    whole platform, not just the docs tree the in-process index covers — so an
    unkeyed install with a database keeps far more grounding than it would if a
    missing optional dependency took the runtime down.

    Order: the gateway (which is where a key would be), then a local model, then
    nothing.
    """
    if gateway_url:
        backend = GatewayEmbeddings(gateway_url, token_provider=token_provider)
        try:
            await backend.embed(["adhar ai readiness probe"])
            return backend
        except Exception as exc:  # noqa: BLE001
            log.info("gateway embeddings unavailable (%s); trying local fallback", exc)
            await backend.aclose()
    try:
        return LocalEmbeddings()
    except Exception as exc:  # noqa: BLE001 - the optional extra is absent
        log.info(
            "no embedding backend available (%s); the knowledge base will use "
            "full-text retrieval only",
            exc,
        )
        return None
