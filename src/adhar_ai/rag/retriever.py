"""Grounding retrieval: pgvector first, lexical BM25 when it cannot answer.

Vector search is the primary path and needs two things the platform may not
have — an embedding endpoint (so, a provider key) and a populated pgvector
table. When either is missing, retrieval falls back to the in-process BM25 index
in `lexical.py` rather than returning nothing: an answer grounded in the right
ADR by keyword beats an ungrounded one, and a silent `[]` is indistinguishable
to a caller from "the docs say nothing about this".

Every `Hit` records which path produced it, so an answer's grounding can be
attributed and `GET /healthz` can say which mode is live.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .embeddings import EmbeddingBackend
from .index import _vector_literal
from .lexical import LexicalIndex

log = logging.getLogger("adhar_ai.rag")


@dataclass(slots=True)
class Hit:
    source: str
    kind: str
    text: str
    distance: float
    #: "vector" or "lexical" — which retrieval path found this chunk.
    retrieval: str = "vector"

    def as_grounding(self) -> str:
        return f"### {self.source} ({self.kind}, {self.retrieval})\n\n{self.text}"


class Retriever:
    def __init__(
        self,
        dsn: str,
        embeddings: EmbeddingBackend | None,
        table: str = "kb_chunk",
        lexical: LexicalIndex | None = None,
    ) -> None:
        self.dsn = dsn
        self.embeddings = embeddings
        self.table = table
        #: Built from the docs tree at start-up. Present even when pgvector is
        #: healthy, because it is also what answers when the table is still
        #: being indexed or the database starts failing mid-life.
        self.lexical = lexical

    @property
    def mode(self) -> str:
        """What `GET /healthz` reports about how grounding is being retrieved."""
        vector = bool(self.dsn and self.embeddings)
        size = self.lexical.size if self.lexical else 0
        if vector and size:
            return f"vector (pgvector) with lexical fallback over {size} chunks"
        if vector:
            return "vector (pgvector)"
        if size:
            return f"lexical only ({size} chunks) — no embeddings or database configured"
        return "unavailable (no database, no embeddings, no docs)"

    def _lexical(self, query: str, k: int) -> list[Hit]:
        if self.lexical is None:
            return []
        return [
            Hit(chunk.source, chunk.kind, chunk.text, distance=1.0 / (1.0 + score),
                retrieval="lexical")
            for chunk, score in self.lexical.search(query, k)
        ]

    async def search(self, query: str, k: int = 5) -> list[Hit]:
        hits = await self._vector_search(query, k)
        # Falls back on an empty result too, not just on an exception: an empty
        # pgvector table and a broken one look identical from here, and both
        # mean the lexical index is the better answer.
        return hits or self._lexical(query, k)

    async def _vector_search(self, query: str, k: int) -> list[Hit]:
        if not self.dsn or self.embeddings is None:
            return []
        try:
            import psycopg
        except ModuleNotFoundError:  # pragma: no cover - optional extra
            return []
        try:
            vectors = await self.embeddings.embed([query])
        except Exception as exc:  # noqa: BLE001 - unkeyed gateway, network, quota
            log.info("embedding unavailable (%s); using lexical retrieval", exc)
            return []
        if not vectors:
            return []
        literal = _vector_literal(vectors[0])
        try:
            async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"SELECT source, kind, chunk, embedding <=> %s::vector AS distance "  # noqa: S608
                        f"FROM {self.table} ORDER BY distance ASC LIMIT %s",
                        (literal, k),
                    )
                    rows = await cur.fetchall()
        except Exception as exc:
            # Retrieval is an enhancement, never a hard dependency: an answer
            # without grounding is far better than a 500.
            log.warning("RAG retrieval failed: %s", exc)
            return []
        return [Hit(str(r[0]), str(r[1]), str(r[2]), float(r[3])) for r in rows]

    async def grounding(self, query: str, k: int = 5) -> list[str]:
        return [hit.as_grounding() for hit in await self.search(query, k)]
