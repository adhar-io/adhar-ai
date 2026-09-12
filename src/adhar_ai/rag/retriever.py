"""Top-k cosine retrieval over `kb_chunk`, returning cited grounding blocks."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .embeddings import EmbeddingBackend
from .index import _vector_literal

log = logging.getLogger("adhar_ai.rag")


@dataclass(slots=True)
class Hit:
    source: str
    kind: str
    text: str
    distance: float

    def as_grounding(self) -> str:
        return f"### {self.source} ({self.kind})\n\n{self.text}"


class Retriever:
    def __init__(self, dsn: str, embeddings: EmbeddingBackend, table: str = "kb_chunk") -> None:
        self.dsn = dsn
        self.embeddings = embeddings
        self.table = table

    async def search(self, query: str, k: int = 5) -> list[Hit]:
        if not self.dsn:
            return []
        try:
            import psycopg
        except ModuleNotFoundError:  # pragma: no cover - optional extra
            return []
        vectors = await self.embeddings.embed([query])
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
