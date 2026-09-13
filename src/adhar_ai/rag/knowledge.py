"""The knowledge base: what the agent knows, and how it keeps learning.

This is the one object the runtime talks to. It owns the sources, the pgvector
store and the lexical fallback, and it exposes three verbs:

    refresh()   re-derive knowledge from the platform
    search()    retrieve grounding for a question
    learn()     take something back in

`learn()` is the half that makes this more than an index. Three things flow back
into the store as they happen:

* **Notes** — meeting notes, troubleshooting write-ups, learnings. Someone types
  them once and every later question can find them.
* **Findings** — what the operators concluded, including the pull request that
  fixed it, so the next similar alert retrieves the last resolution.
* **Feedback** — which retrieved chunks actually helped. Chunks that repeatedly
  help rank slightly higher; ones that repeatedly mislead rank slightly lower.

The degradation ladder is deliberate and has no cliff:

    pgvector + embeddings   hybrid vector and lexical retrieval, the full thing
    pgvector, no key        lexical retrieval over the same indexed corpus
    no database             in-process BM25 over the docs tree
    no docs either          honest: "no grounding is available"

An unkeyed platform is therefore still grounded, which is the difference between
an assistant that knows Adhar and one that knows the internet.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from .documents import Chunk, Document
from .lexical import LexicalIndex
from .sources import (
    ClusterSource,
    DocsSource,
    FindingsSource,
    NotesSource,
    PackagesSource,
    Source,
    ToolsSource,
)
from .store import Hit, IngestReport, KnowledgeStore

log = logging.getLogger("adhar_ai.rag")

#: How often the background refresh re-derives knowledge. Live sources (cluster
#: inventory) move fastest and set the floor; docs and packages are cheap to
#: re-check because unchanged content is never re-embedded.
DEFAULT_REFRESH_SECONDS = 1800


@dataclass
class KnowledgeBase:
    store: KnowledgeStore
    embedder: Any = None
    lexical: LexicalIndex | None = None
    notes: NotesSource | None = None
    sources: list[Source] = field(default_factory=list)
    #: Last refresh per origin, for `/knowledge/stats`.
    last_refresh: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ------------------------------------------------------------ assembly --

    @classmethod
    def build(
        cls,
        dsn: str,
        docs_path: str = "",
        packages_path: str = "",
        table: str = "kb_chunk",
        notes_table: str = "kb_note",
        toolbox: Any = None,
        findings: Any = None,
        embedder: Any = None,
    ) -> KnowledgeBase:
        store = KnowledgeStore(dsn, table=table)
        notes = NotesSource(dsn, table=notes_table) if dsn else None
        sources: list[Source] = [ToolsSource()]
        if docs_path:
            sources.append(DocsSource(docs_path))
        if packages_path:
            sources.append(PackagesSource(packages_path))
        if toolbox is not None:
            sources.append(ClusterSource(toolbox))
        if findings is not None:
            sources.append(FindingsSource(findings))
        if notes is not None:
            sources.append(notes)
        return cls(store=store, embedder=embedder, notes=notes, sources=sources)

    async def prepare(self) -> bool:
        ready = await self.store.prepare()
        if self.notes is not None:
            await self.notes.prepare()
        return ready

    @property
    def mode(self) -> str:
        """What `/healthz` reports about how grounding is retrieved."""
        size = self.lexical.size if self.lexical else 0
        if self.store.ready and self.embedder is not None:
            return f"hybrid (pgvector + lexical), fallback over {size} local chunks"
        if self.store.ready:
            return "lexical over pgvector (no embeddings configured)"
        if size:
            return f"in-process lexical only ({size} chunks) — {self.store.status}"
        return f"unavailable — {self.store.status}"

    # ------------------------------------------------------------- refresh --

    async def refresh(self, only: tuple[str, ...] = ()) -> list[IngestReport]:
        """Re-derive knowledge from every source. One failure costs one origin."""
        reports: list[IngestReport] = []
        for source in self.sources:
            if only and source.origin not in only:
                continue
            report = IngestReport(origin=source.origin)
            started = time.monotonic()
            try:
                documents = await source.documents()
            except Exception as exc:  # noqa: BLE001
                report.error = f"{type(exc).__name__}: {exc}"
                log.warning("knowledge source %s failed: %s", source.origin, exc)
                reports.append(report)
                continue

            if self.store.ready and self.embedder is not None:
                report = await self.store.ingest(source.origin, documents, self.embedder)
            else:
                # No store or no embedder: the documents still feed the
                # in-process lexical index, so a keyless platform is grounded.
                report.documents = len(documents)
                report.chunks_written = sum(len(d.chunks()) for d in documents)

            reports.append(report)
            self.last_refresh[source.origin] = {
                **report.as_dict(),
                "seconds": round(time.monotonic() - started, 2),
                "at": time.time(),
            }

        await self._rebuild_lexical(only)
        return reports

    async def _rebuild_lexical(self, only: tuple[str, ...] = ()) -> None:
        """Keep the in-process BM25 index in step with the sources.

        It is built even when pgvector is healthy: it is what answers while a
        first ingest is still running, and what keeps answering if the database
        starts failing mid-life.

        A SCOPED refresh replaces only that origin's chunks and keeps the rest.
        Rebuilding from just the refreshed sources — which is what this did —
        silently threw away everything else: refreshing the `cluster` origin
        alone shrank the index from a thousand chunks to three, and every
        subsequent lexical answer lost the documentation.
        """
        kept: list[Chunk] = []
        if only and self.lexical is not None:
            kept = [c for c in self.lexical.chunks() if c.origin not in only]

        documents: list[Document] = []
        for source in self.sources:
            if only and source.origin not in only:
                continue
            try:
                documents.extend(await source.documents())
            except Exception:  # noqa: BLE001 - already reported by refresh()
                continue
        chunks = kept + [c for doc in documents for c in doc.chunks()]
        if chunks:
            self.lexical = LexicalIndex.from_chunks(chunks)

    # -------------------------------------------------------------- search --

    async def search(self, query: str, k: int = 5, kinds: tuple[str, ...] = ()) -> list[Hit]:
        if self.store.ready:
            hits = await self.store.search(query, self.embedder, k=k, kinds=kinds)
            if hits:
                return hits
        return self._lexical_hits(query, k)

    def _lexical_hits(self, query: str, k: int) -> list[Hit]:
        if self.lexical is None:
            return []
        return [
            Hit(
                chunk_id=-1,
                doc_id=chunk.doc_id,
                source=chunk.source,
                kind=chunk.kind,
                origin=chunk.origin,
                text=chunk.text,
                score=score,
                retrieval="lexical (in-process)",
                metadata=dict(chunk.metadata),
            )
            for chunk, score in self.lexical.search(query, k)
        ]

    async def grounding(self, query: str, k: int = 5) -> list[str]:
        return [hit.as_grounding() for hit in await self.search(query, k)]

    async def grounding_with_ids(self, query: str, k: int = 5) -> tuple[list[str], list[int]]:
        """Grounding blocks plus the chunk ids behind them.

        The ids are what makes feedback possible: an answer can be reported
        unhelpful and the store knows exactly which retrieved chunks led to it.
        """
        hits = await self.search(query, k)
        return (
            [hit.as_grounding() for hit in hits],
            [hit.chunk_id for hit in hits if hit.chunk_id >= 0],
        )

    # --------------------------------------------------------------- learn --

    async def add_note(
        self,
        title: str,
        body: str,
        kind: str = "note",
        author: str = "unknown",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Take in a human-written note and make it retrievable immediately.

        Indexed on the spot rather than at the next refresh: someone writing up
        an incident at 02:00 should be able to ask about it at 02:01.
        """
        note_id = secrets.token_hex(8)
        stored = False
        if self.notes is not None:
            stored = await self.notes.add(note_id, title, body, kind, author, tags)

        document = Document(
            doc_id=f"note:{note_id}",
            source=f"{kind}: {title}",
            text=f"# {title}\n\n- Recorded by: {author}\n\n{body}",
            kind=kind if kind in ("note", "incident", "runbook") else "note",
            origin="notes",
            metadata={"author": author, "tags": tags or []},
        )
        indexed = False
        if self.store.ready and self.embedder is not None:
            # prune=False: this is one document, not the whole origin. Pruning
            # here would delete every other note.
            report = await self.store.ingest("notes", [document], self.embedder, prune=False)
            indexed = report.chunks_written > 0 and not report.error

        self._add_lexical(document)
        return {
            "id": note_id,
            "durable": stored,
            "indexed": indexed,
            "retrievable": True,
            "kind": document.kind,
        }

    def _add_lexical(self, document: Document) -> None:
        chunks = list(self.lexical.chunks()) if self.lexical else []
        chunks.extend(document.chunks())
        self.lexical = LexicalIndex.from_chunks(chunks)

    async def learn_from_finding(self, finding: Any) -> bool:
        """Index one finding as soon as it is produced."""
        if not (self.store.ready and self.embedder is not None):
            return False
        try:
            source = FindingsSource(_OneFinding(finding))
            documents = await source.documents()
        except Exception as exc:  # noqa: BLE001
            log.debug("could not turn a finding into knowledge: %s", exc)
            return False
        if not documents:
            return False
        report = await self.store.ingest("findings", documents, self.embedder, prune=False)
        return report.chunks_written > 0 and not report.error

    async def record_feedback(self, chunk_ids: list[int], helpful: bool) -> int:
        return await self.store.record_feedback(chunk_ids, helpful)

    async def stats(self) -> dict[str, Any]:
        stats = await self.store.stats()
        stats["mode"] = self.mode
        stats["sources"] = [s.origin for s in self.sources]
        stats["lastRefresh"] = self.last_refresh
        stats["lexicalChunks"] = self.lexical.size if self.lexical else 0
        return stats


@dataclass(slots=True)
class _OneFinding:
    """Adapts a single finding to the interface `FindingsSource` expects."""

    finding: Any

    async def recent(self, limit: int = 1) -> list[Any]:
        return [self.finding]
