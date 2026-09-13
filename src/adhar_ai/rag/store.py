"""The platform's knowledge store: pgvector on the CNPG `adhar-ai-rag` database.

Three properties this is built around, because they are what separate a
knowledge base that stays true from one that quietly rots:

**Incremental, not rebuilt.** Ingestion upserts by `(doc_id, chunk_index)` and
skips the embedding call entirely when the content hash is unchanged. A nightly
refresh of a corpus that barely moved costs almost nothing, which is what makes
it affordable to run a nightly refresh at all. The previous implementation
`TRUNCATE`d and re-embedded everything, so keeping the index current and keeping
the bill down were in direct conflict.

**Deletions propagate.** Each ingest run reports which `doc_id`s an origin still
has; anything else from that origin is removed. Without this a decommissioned
package or a deleted runbook stays retrievable forever, and the agent confidently
cites something that no longer exists — worse than not knowing.

**Hybrid retrieval.** Vector similarity finds things phrased differently from the
question; Postgres full-text finds the exact identifier the user typed
(`CreateContainerConfigError`, `adhar-ai-mcp-gitops`) that an embedding blurs
away. Platform questions contain a lot of exact identifiers, so the two are fused
rather than chosen between, and the fusion degrades to whichever half is
available.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from .documents import Chunk, Document, kind_weight

log = logging.getLogger("adhar_ai.rag")

#: The embedding width the platform's schema is built for. 1536 matches
#: OpenAI's text-embedding-3-small, which is what the gateway serves by default;
#: shorter vectors from a local model are zero-padded to fit (see embeddings.py).
EMBEDDING_DIM = 1536

#: Run separately, and allowed to fail. In the platform the extension is created
#: by CNPG's `postInitApplicationSQL` as a superuser; the application user
#: usually cannot create extensions, and an error here would otherwise abort the
#: whole schema statement and leave the store unusable for no reason.
EXTENSION_SQL = "CREATE EXTENSION IF NOT EXISTS vector"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS {table} (
  id            BIGSERIAL PRIMARY KEY,
  doc_id        TEXT NOT NULL,
  chunk_index   INTEGER NOT NULL,
  source        TEXT NOT NULL,
  kind          TEXT NOT NULL DEFAULT 'doc',
  origin        TEXT NOT NULL DEFAULT 'unknown',
  chunk         TEXT NOT NULL,
  content_hash  TEXT NOT NULL,
  -- WHICH MODEL produced `embedding`. Vectors from different models are not
  -- comparable: cosine distance between them is noise, so a table holding two
  -- models silently returns whichever rows happen to share the query's model
  -- and ignores the rest. That failure is invisible — no error, no empty
  -- result, just consistently wrong retrieval — so the model is recorded per
  -- row and a row embedded by a different one is treated as stale.
  embed_model   TEXT NOT NULL DEFAULT 'unknown',
  metadata      JSONB NOT NULL DEFAULT '{{}}'::jsonb,
  embedding     vector({dim}),
  ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  helpful       INTEGER NOT NULL DEFAULT 0,
  unhelpful     INTEGER NOT NULL DEFAULT 0,
  UNIQUE (doc_id, chunk_index)
);

-- Vector search. HNSW over cosine distance, which is what `<=>` uses below.
CREATE INDEX IF NOT EXISTS {table}_embedding_idx
  ON {table} USING hnsw (embedding vector_cosine_ops);

-- Lexical search over the same rows. Platform questions are full of exact
-- identifiers that an embedding blurs; this is the half that finds them.
CREATE INDEX IF NOT EXISTS {table}_fts_idx
  ON {table} USING gin (to_tsvector('english', chunk));

CREATE INDEX IF NOT EXISTS {table}_origin_idx ON {table} (origin);
CREATE INDEX IF NOT EXISTS {table}_kind_idx   ON {table} (kind);
CREATE INDEX IF NOT EXISTS {table}_doc_idx    ON {table} (doc_id);

-- Added after the table shipped, so existing installs gain it on the next
-- start-up rather than needing a migration step.
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS embed_model TEXT NOT NULL DEFAULT 'unknown';
"""

#: What is already stored for an origin, so ingestion can tell what changed.
FINGERPRINT_SQL = (
    "SELECT doc_id, chunk_index, content_hash, embed_model FROM {table} WHERE origin = %s"
)

UPSERT_SQL = """
INSERT INTO {table}
  (doc_id, chunk_index, source, kind, origin, chunk, content_hash, embed_model,
   metadata, embedding)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::vector)
ON CONFLICT (doc_id, chunk_index) DO UPDATE SET
  source = EXCLUDED.source,
  kind = EXCLUDED.kind,
  origin = EXCLUDED.origin,
  chunk = EXCLUDED.chunk,
  content_hash = EXCLUDED.content_hash,
  embed_model = EXCLUDED.embed_model,
  metadata = EXCLUDED.metadata,
  embedding = EXCLUDED.embedding,
  updated_at = now()
"""

#: Refresh the citation without paying for a new embedding, for a chunk whose
#: content hash says the text has not changed.
TOUCH_SQL = "UPDATE {table} SET updated_at = now() WHERE doc_id = %s AND chunk_index = %s"

DELETE_DOCS_SQL = "DELETE FROM {table} WHERE origin = %s AND doc_id = ANY(%s)"
DELETE_TAIL_SQL = "DELETE FROM {table} WHERE doc_id = %s AND chunk_index >= %s"

VECTOR_SEARCH_SQL = """
SELECT id, doc_id, source, kind, origin, chunk, metadata, helpful, unhelpful,
       (embedding <=> %s::vector) AS distance
FROM {table}
WHERE embedding IS NOT NULL {kind_filter}
ORDER BY distance ASC
LIMIT %s
"""

LEXICAL_SEARCH_SQL = """
SELECT id, doc_id, source, kind, origin, chunk, metadata, helpful, unhelpful,
       ts_rank_cd(to_tsvector('english', chunk), plainto_tsquery('english', %s)) AS rank
FROM {table}
WHERE to_tsvector('english', chunk) @@ plainto_tsquery('english', %s) {kind_filter}
ORDER BY rank DESC
LIMIT %s
"""

FEEDBACK_SQL = """
UPDATE {table}
SET helpful = helpful + %s, unhelpful = unhelpful + %s
WHERE id = ANY(%s)
"""

MODELS_SQL = "SELECT embed_model, count(*) FROM {table} GROUP BY embed_model ORDER BY 2 DESC"

STATS_SQL = """
SELECT origin, kind, count(*) AS chunks, count(DISTINCT doc_id) AS documents,
       max(updated_at) AS freshest, sum(helpful) AS helpful, sum(unhelpful) AS unhelpful
FROM {table}
GROUP BY origin, kind
ORDER BY origin, kind
"""

#: Reciprocal-rank-fusion constant. 60 is the value from the original RRF paper
#: and is not sensitive: it decides how steeply rank 1 outweighs rank 10.
RRF_K = 60


@dataclass(slots=True)
class IngestReport:
    origin: str
    documents: int = 0
    chunks_written: int = 0
    chunks_unchanged: int = 0
    chunks_deleted: int = 0
    embeddings_called: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "documents": self.documents,
            "chunksWritten": self.chunks_written,
            "chunksUnchanged": self.chunks_unchanged,
            "chunksDeleted": self.chunks_deleted,
            "embeddingsCalled": self.embeddings_called,
            **({"error": self.error} if self.error else {}),
        }


@dataclass(slots=True)
class Hit:
    chunk_id: int
    doc_id: str
    source: str
    kind: str
    origin: str
    text: str
    score: float
    retrieval: str
    metadata: dict[str, Any]

    def as_grounding(self) -> str:
        return f"### {self.source} ({self.kind}, {self.retrieval})\n\n{self.text}"


def embedder_name(embedder: Any) -> str:
    """A stable identity for the model that produced a vector.

    Used to detect that the index now holds two incompatible embedding spaces,
    which is otherwise undetectable from the outside.
    """
    if embedder is None:
        return "none"
    model = getattr(embedder, "model", "") or ""
    name = getattr(embedder, "name", type(embedder).__name__)
    return f"{name}:{model}" if model else str(name)


def vector_literal(values: list[float]) -> str:
    """pgvector's text input form, so no psycopg adapter is required."""
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


class KnowledgeStore:
    """pgvector-backed storage and retrieval. Every method degrades, never raises."""

    def __init__(self, dsn: str, table: str = "kb_chunk", dim: int = EMBEDDING_DIM) -> None:
        self.dsn = dsn
        if not table.replace("_", "").isalnum():
            raise ValueError(f"invalid knowledge table name {table!r}")
        self.table = table
        self.dim = dim
        self.ready = False
        self.status = "disabled (no database)" if not dsn else "pending"

    # ------------------------------------------------------------ plumbing --

    async def _connect(self) -> Any:
        import psycopg

        return await psycopg.AsyncConnection.connect(self.dsn)

    async def prepare(self) -> bool:
        """Create the schema. Returns whether the store is usable."""
        if not self.dsn:
            return False
        try:
            async with await self._connect() as conn, conn.cursor() as cur:
                try:
                    await cur.execute(EXTENSION_SQL)
                    await conn.commit()
                except Exception as exc:  # noqa: BLE001
                    # Almost always "permission denied to create extension", and
                    # almost always harmless: CNPG already created it at initdb.
                    # If it genuinely is absent the next statement fails loudly.
                    await conn.rollback()
                    log.debug("could not create the vector extension (%s); assuming it exists", exc)
                await cur.execute(SCHEMA_SQL.format(table=self.table, dim=self.dim))
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            self.ready = False
            self.status = f"unavailable: {type(exc).__name__}: {exc}"
            log.warning("knowledge store unavailable: %s", exc)
            return False
        self.ready = True
        self.status = f"ready ({self.table})"
        return True

    # ------------------------------------------------------------- ingest --

    async def ingest(
        self,
        origin: str,
        documents: list[Document],
        embedder: Any,
        prune: bool = True,
    ) -> IngestReport:
        """Upsert an origin's documents, embedding only what actually changed."""
        report = IngestReport(origin=origin, documents=len(documents))
        if not self.ready:
            report.error = "knowledge store is not available"
            return report

        chunks: list[Chunk] = []
        for doc in documents:
            chunks.extend(doc.chunks())

        try:
            async with await self._connect() as conn, conn.cursor() as cur:
                await cur.execute(FINGERPRINT_SQL.format(table=self.table), (origin,))
                known = {(r[0], r[1]): (r[2], r[3]) for r in await cur.fetchall()}

                # A chunk is stale if its TEXT changed OR if it was embedded by a
                # different model. The second half matters more than it looks:
                # `load_embeddings` falls back from the gateway to a local model
                # to nothing, so a cluster that loses its key silently starts
                # writing vectors from a different space into the same table. The
                # result is not an error, it is quietly wrong retrieval.
                model = embedder_name(embedder)
                fresh = [
                    c
                    for c in chunks
                    if known.get((c.doc_id, c.chunk_index)) != (c.content_hash, model)
                ]
                report.chunks_unchanged = len(chunks) - len(fresh)

                # One embedding call for everything that changed, rather than one
                # per chunk: providers charge and rate-limit per request.
                vectors: list[list[float]] = []
                if fresh:
                    vectors = await embedder.embed([c.text for c in fresh])
                    report.embeddings_called = 1
                if len(vectors) != len(fresh):
                    report.error = (
                        f"embedder returned {len(vectors)} vectors for {len(fresh)} chunks"
                    )
                    return report

                for chunk, vector in zip(fresh, vectors, strict=True):
                    await cur.execute(
                        UPSERT_SQL.format(table=self.table),
                        (
                            chunk.doc_id,
                            chunk.chunk_index,
                            chunk.source,
                            chunk.kind,
                            chunk.origin or origin,
                            chunk.text,
                            chunk.content_hash,
                            model,
                            json.dumps(chunk.metadata),
                            vector_literal(vector),
                        ),
                    )
                report.chunks_written = len(fresh)

                for chunk in chunks:
                    if known.get((chunk.doc_id, chunk.chunk_index)) == (
                        chunk.content_hash,
                        model,
                    ):
                        await cur.execute(
                            TOUCH_SQL.format(table=self.table),
                            (chunk.doc_id, chunk.chunk_index),
                        )

                # A document that shrank leaves orphan chunks past its new end.
                counts: dict[str, int] = {}
                for chunk in chunks:
                    counts[chunk.doc_id] = max(counts.get(chunk.doc_id, 0), chunk.chunk_index + 1)
                for doc_id, length in counts.items():
                    await cur.execute(DELETE_TAIL_SQL.format(table=self.table), (doc_id, length))

                if prune:
                    seen = {c.doc_id for c in chunks}
                    stale = sorted({d for (d, _) in known} - seen)
                    if stale:
                        await cur.execute(
                            DELETE_DOCS_SQL.format(table=self.table), (origin, stale)
                        )
                        report.chunks_deleted = cur.rowcount or 0

                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            report.error = f"{type(exc).__name__}: {exc}"
            log.warning("ingest of origin %s failed: %s", origin, exc)
        return report

    # ------------------------------------------------------------- search --

    async def search(
        self,
        query: str,
        embedder: Any = None,
        k: int = 5,
        kinds: tuple[str, ...] = (),
    ) -> list[Hit]:
        """Hybrid vector + lexical retrieval, fused by reciprocal rank."""
        if not self.ready or not query.strip():
            return []

        vector_rows: list[tuple] = []
        lexical_rows: list[tuple] = []
        kind_filter = " AND kind = ANY(%s)" if kinds else ""

        try:
            async with await self._connect() as conn, conn.cursor() as cur:
                if embedder is not None:
                    try:
                        vectors = await embedder.embed([query])
                    except Exception as exc:  # noqa: BLE001
                        log.info("embedding unavailable (%s); lexical only", exc)
                        vectors = []
                    if vectors:
                        params: list[Any] = [vector_literal(vectors[0])]
                        if kinds:
                            params.append(list(kinds))
                        params.append(k * 3)
                        await cur.execute(
                            VECTOR_SEARCH_SQL.format(table=self.table, kind_filter=kind_filter),
                            tuple(params),
                        )
                        vector_rows = await cur.fetchall()

                params = [query, query]
                if kinds:
                    params.append(list(kinds))
                params.append(k * 3)
                await cur.execute(
                    LEXICAL_SEARCH_SQL.format(table=self.table, kind_filter=kind_filter),
                    tuple(params),
                )
                lexical_rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("knowledge search failed: %s", exc)
            return []

        return self._fuse(vector_rows, lexical_rows, k)

    @staticmethod
    def _row_to_hit(row: tuple, retrieval: str, score: float) -> Hit:
        metadata = row[6]
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}
        return Hit(
            chunk_id=row[0],
            doc_id=row[1],
            source=row[2],
            kind=row[3],
            origin=row[4],
            text=row[5],
            metadata=metadata or {},
            score=score,
            retrieval=retrieval,
        )

    def _fuse(self, vector_rows: list[tuple], lexical_rows: list[tuple], k: int) -> list[Hit]:
        """Reciprocal rank fusion, then a mild trust and feedback adjustment.

        RRF combines two rankings without needing their scores to be comparable,
        which they are not: a cosine distance and a `ts_rank_cd` share no scale.
        Only the ORDER each retriever produced is used.
        """
        scores: dict[int, float] = {}
        hits: dict[int, Hit] = {}
        retrievals: dict[int, set[str]] = {}

        for rows, label in ((vector_rows, "vector"), (lexical_rows, "lexical")):
            for rank, row in enumerate(rows):
                chunk_id = row[0]
                scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank + 1)
                retrievals.setdefault(chunk_id, set()).add(label)
                hits.setdefault(chunk_id, self._row_to_hit(row, label, 0.0))

        for chunk_id, hit in hits.items():
            helpful, unhelpful = 0, 0
            for rows in (vector_rows, lexical_rows):
                for row in rows:
                    if row[0] == chunk_id:
                        helpful, unhelpful = row[7], row[8]
                        break
            # Feedback is a nudge, not a verdict: one downvote must not bury a
            # chunk that is still the only thing that answers the question.
            votes = helpful - unhelpful
            feedback = 1.0 + max(-0.3, min(0.3, votes * 0.05))
            scores[chunk_id] *= kind_weight(hit.kind) * feedback
            hit.score = scores[chunk_id]
            found = retrievals.get(chunk_id, set())
            hit.retrieval = "vector+lexical" if len(found) > 1 else next(iter(found), "vector")

        ranked = sorted(hits.values(), key=lambda h: h.score, reverse=True)
        return ranked[:k]

    # ------------------------------------------------------------ feedback --

    async def record_feedback(self, chunk_ids: list[int], helpful: bool) -> int:
        """Record that these chunks did or did not help. Returns rows updated."""
        if not self.ready or not chunk_ids:
            return 0
        try:
            async with await self._connect() as conn, conn.cursor() as cur:
                await cur.execute(
                    FEEDBACK_SQL.format(table=self.table),
                    (1 if helpful else 0, 0 if helpful else 1, list(chunk_ids)),
                )
                updated = cur.rowcount or 0
                await conn.commit()
                return updated
        except Exception as exc:  # noqa: BLE001
            log.warning("could not record knowledge feedback: %s", exc)
            return 0

    async def stats(self) -> dict[str, Any]:
        """What the knowledge base currently holds, by origin and kind."""
        if not self.ready:
            return {"status": self.status, "origins": []}
        try:
            async with await self._connect() as conn, conn.cursor() as cur:
                await cur.execute(STATS_SQL.format(table=self.table))
                rows = await cur.fetchall()
                await cur.execute(MODELS_SQL.format(table=self.table))
                models = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            return {"status": f"unavailable: {exc}", "origins": []}
        mixed = len([m for m in models if m[1] > 0]) > 1
        return {
            "status": self.status,
            "table": self.table,
            "embedModels": [{"model": m[0], "chunks": m[1]} for m in models],
            # Loud on purpose: two embedding spaces in one table is silently
            # wrong retrieval, and the next refresh repairs it by re-embedding.
            **(
                {
                    "warning": (
                        "more than one embedding model is present; vectors from "
                        "different models are not comparable, so retrieval is "
                        "degraded until the next refresh re-embeds them"
                    )
                }
                if mixed
                else {}
            ),
            "chunks": sum(r[2] for r in rows),
            "documents": sum(r[3] for r in rows),
            "origins": [
                {
                    "origin": r[0],
                    "kind": r[1],
                    "chunks": r[2],
                    "documents": r[3],
                    "freshest": r[4].isoformat() if r[4] else None,
                    "helpful": int(r[5] or 0),
                    "unhelpful": int(r[6] or 0),
                }
                for r in rows
            ],
        }
