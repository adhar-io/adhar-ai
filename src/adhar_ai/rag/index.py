"""Chunk and ingest the platform's documentation into pgvector.

Walks the docs tree (`docs/`, `docs/adr/`, `docs/design/`, `PRODUCTION.md`,
runbooks) that `ADHAR_AI_DOCS_PATH` points at, chunks on Markdown headings, and
upserts into the `kb_chunk` table the platform's CNPG bootstrap SQL creates.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .embeddings import EmbeddingBackend

log = logging.getLogger("adhar_ai.rag")

MAX_CHARS = 2000
HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$", re.MULTILINE)

CREATE_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS {table} (
  id        BIGSERIAL PRIMARY KEY,
  source    TEXT,
  kind      TEXT,
  chunk     TEXT,
  embedding vector({dim})
);
CREATE INDEX IF NOT EXISTS {table}_embedding_idx
  ON {table} USING hnsw (embedding vector_cosine_ops);
"""


@dataclass(slots=True)
class Chunk:
    source: str
    kind: str
    text: str


def classify(path: Path) -> str:
    parts = {p.lower() for p in path.parts}
    name = path.name.lower()
    if "adr" in parts:
        return "adr"
    if "runbook" in parts or "runbooks" in parts or "runbook" in name:
        return "runbook"
    if "incident" in parts or "incident" in name:
        return "incident"
    return "doc"


def chunk_markdown(text: str, source: str, kind: str = "doc") -> list[Chunk]:
    """Split on headings, then hard-wrap oversized sections.

    Each chunk keeps its heading path in the `source` so a citation points at a
    section (`docs/adr/0024-….md#Decision`), not just a file.
    """
    matches = list(HEADING_RE.finditer(text))
    sections: list[tuple[str, str]] = []
    if not matches:
        sections.append((source, text))
    else:
        if matches[0].start() > 0:
            preamble = text[: matches[0].start()].strip()
            if preamble:
                sections.append((source, preamble))
        for i, match in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            heading = match.group(2).strip()
            body = text[match.start() : end].strip()
            if body:
                sections.append((f"{source}#{heading}", body))

    chunks: list[Chunk] = []
    for label, body in sections:
        if len(body) <= MAX_CHARS:
            chunks.append(Chunk(label, kind, body))
            continue
        paragraphs = body.split("\n\n")
        buf = ""
        for para in paragraphs:
            if len(buf) + len(para) + 2 > MAX_CHARS and buf:
                chunks.append(Chunk(label, kind, buf.strip()))
                buf = ""
            buf = f"{buf}\n\n{para}" if buf else para
        if buf.strip():
            chunks.append(Chunk(label, kind, buf.strip()))
    return [c for c in chunks if c.text.strip()]


def collect(docs_path: str | Path) -> list[Chunk]:
    """Walk a docs tree and produce chunks. Returns [] if the path is absent —
    an un-mounted docs volume disables RAG, it does not crash the runtime."""
    root = Path(docs_path)
    if not root.exists():
        log.info("docs path %s does not exist; RAG ingestion skipped", root)
        return []
    files = sorted(root.rglob("*.md")) if root.is_dir() else [root]
    chunks: list[Chunk] = []
    for file in files:
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = str(file.relative_to(root)) if root.is_dir() else file.name
        chunks.extend(chunk_markdown(text, rel, classify(file)))
    return chunks


async def ingest_path(
    dsn: str,
    docs_path: str | Path,
    embeddings: EmbeddingBackend,
    table: str = "kb_chunk",
    batch_size: int = 32,
) -> int:
    """Re-index the docs tree. Returns the number of chunks written.

    Idempotent: the table is truncated first, so a re-run replaces the index
    rather than accumulating duplicates (this is what the nightly reindex
    CronOperation calls).
    """
    chunks = collect(docs_path)
    if not chunks:
        return 0
    try:
        import psycopg
    except ModuleNotFoundError as exc:  # pragma: no cover - optional extra
        raise RuntimeError(
            "RAG needs the optional extra: `uv sync --extra rag`"
        ) from exc

    written = 0
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(CREATE_SQL.format(table=table, dim=embeddings.dim))
            await cur.execute(f"TRUNCATE {table}")  # noqa: S608 - table name is config, not input
            for i in range(0, len(chunks), batch_size):
                batch = chunks[i : i + batch_size]
                vectors = await embeddings.embed([c.text for c in batch])
                await cur.executemany(
                    f"INSERT INTO {table} (source, kind, chunk, embedding) "  # noqa: S608
                    "VALUES (%s, %s, %s, %s)",
                    [
                        (c.source, c.kind, c.text, _vector_literal(v))
                        for c, v in zip(batch, vectors, strict=True)
                    ],
                )
                written += len(batch)
        await conn.commit()
    log.info("indexed %d chunks from %s", written, docs_path)
    return written


def _vector_literal(vector: list[float]) -> str:
    """pgvector accepts a `[1,2,3]` text literal — avoids needing the pgvector
    psycopg adapter to be registered."""
    return "[" + ",".join(f"{v:.6f}" for v in vector) + "]"
