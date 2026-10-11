"""Qdrant-backed knowledge storage and retrieval.

Replaces pgvector (2026-10-11). The embeddings used to live in a `vector`
column on the runtime's CNPG database, which meant the platform ran a vector
index nobody tuned inside a database sized for transactional state. The
platform already offers a real vector store through the control plane, so the
embeddings moved there and Postgres kept the job it is good at: the runtime's
threads, runs and task queue.

WHAT CHANGED BESIDES THE ENGINE, AND WHY
----------------------------------------
The pgvector store did BOTH halves of hybrid retrieval itself: cosine distance
for meaning, and Postgres full-text (`ts_rank_cd`) for the exact identifier a
user typed. Qdrant cannot do the second half the same way — its text index is a
FILTER, not a scorer, so it can tell you which chunks contain a term but not
which contains it most significantly. Fusing an unranked subset by reciprocal
rank would feed RRF an arbitrary order, which is worse than not fusing at all.

So the lexical half moves to the in-process BM25 index that already exists for
exactly this (`rag/lexical.py`, built from the same chunks by
`KnowledgeBase._rebuild_lexical`). That keeps real term weighting, costs one
less network round trip per query, and leaves this module responsible for one
thing it can do well.

The alternative — Qdrant named sparse vectors with IDF — is the "proper" answer
and is deliberately not taken yet: it means encoding a sparse vector on every
ingest and every query, and a subtle mismatch between the two encoders is
invisible (retrieval simply gets quietly worse). Worth doing when there is a
way to measure it; `tests/evals/test_retrieval_gate.py` is where that would be
proven.

EVERY METHOD DEGRADES, NEVER RAISES
-----------------------------------
Same contract as the store it replaces. A knowledge base that is unavailable
must make the agent answer without grounding, not fail the request.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import httpx

from .documents import Chunk, Document, kind_weight
from .store import EMBEDDING_DIM, RRF_K, Hit, IngestReport, embedder_name

log = logging.getLogger("adhar_ai.rag")

#: Qdrant point IDs must be an unsigned integer or a UUID. The chunk id is an
#: int everywhere in this codebase (`Hit.chunk_id`, the feedback API), so the id
#: is DERIVED from the chunk's identity rather than assigned: the same
#: (doc_id, chunk_index) always maps to the same point, which is what makes
#: ingestion an idempotent upsert instead of an append.
_ID_BITS = 63


def point_id(doc_id: str, chunk_index: int) -> int:
    """A stable positive 63-bit id for a chunk.

    blake2b rather than Python's `hash()`: `hash()` is salted per process, so
    the same chunk would get a different id on every restart and every ingest
    would duplicate the corpus instead of updating it.
    """
    digest = hashlib.blake2b(f"{doc_id}\x00{chunk_index}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") >> (64 - _ID_BITS)


class QdrantKnowledgeStore:
    """Vector storage and retrieval over a Qdrant collection."""

    def __init__(
        self,
        url: str,
        collection: str = "adhar_knowledge",
        api_key: str = "",
        dim: int = EMBEDDING_DIM,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.collection = collection
        self.api_key = api_key
        self.dim = dim
        self.ready = False
        self.status = "disabled (no vector store)" if not url else "pending"
        self._client = client

    # ------------------------------------------------------------ plumbing --

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=60.0)
        return self._client

    def _headers(self) -> dict[str, str]:
        # Qdrant's own header name. Sent only when set: the platform wires the
        # key as an OPTIONAL secret ref, so an empty value is the normal state
        # while the collection is still being composed.
        return {"api-key": self.api_key} if self.api_key else {}

    async def _call(self, method: str, path: str, payload: Any = None) -> Any:
        resp = await self._http().request(
            method, f"{self.url}{path}", json=payload, headers=self._headers()
        )
        resp.raise_for_status()
        if not resp.content:
            return None
        return resp.json().get("result")

    # ------------------------------------------------------------- prepare --

    async def prepare(self) -> bool:
        """Create the collection and its payload indexes if they are missing.

        Idempotent and tolerant of a collection that already exists with the
        right shape, because the control plane's `Vector` composition may have
        created it already — and that is the normal case on this platform. A
        collection that exists with the WRONG vector width is reported rather
        than recreated: dropping it would silently discard the corpus, and
        cosine distance between widths matches nothing, so the honest outcome is
        a loud status and no retrieval.
        """
        if not self.url:
            self.status = "disabled (no vector store)"
            return False
        try:
            existing = await self._call("GET", f"/collections/{self.collection}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                self.status = f"unavailable ({exc.response.status_code})"
                log.warning("vector store unreachable: %s", exc)
                return False
            existing = None
        except Exception as exc:  # noqa: BLE001
            self.status = f"unavailable ({exc})"
            log.warning("vector store unreachable: %s", exc)
            return False

        if existing is None:
            try:
                await self._call(
                    "PUT",
                    f"/collections/{self.collection}",
                    {"vectors": {"size": self.dim, "distance": "Cosine"}},
                )
            except Exception as exc:  # noqa: BLE001
                self.status = f"could not create collection ({exc})"
                log.warning("creating collection %s: %s", self.collection, exc)
                return False
        else:
            width = (
                (existing.get("config") or {})
                .get("params", {})
                .get("vectors", {})
                .get("size")
            )
            if width and int(width) != self.dim:
                self.status = (
                    f"collection {self.collection} holds {width}-d vectors, "
                    f"this runtime embeds {self.dim}-d"
                )
                log.error("%s — retrieval disabled rather than silently wrong", self.status)
                return False

        # Payload indexes. Without them every filtered read is a full scan, and
        # ingestion filters by doc_id and origin on every document.
        for field, schema in (("doc_id", "keyword"), ("origin", "keyword"), ("kind", "keyword")):
            try:
                await self._call(
                    "PUT",
                    f"/collections/{self.collection}/index",
                    {"field_name": field, "field_schema": schema},
                )
            except Exception as exc:  # noqa: BLE001
                # An index that already exists answers 400; not fatal either way
                # — it costs scan time, not correctness.
                log.debug("payload index %s: %s", field, exc)

        self.ready = True
        self.status = f"ready ({self.collection})"
        return True

    # -------------------------------------------------------------- ingest --

    async def _points_for_doc(self, doc_id: str) -> dict[int, dict[str, Any]]:
        """Existing points of a document, by chunk index."""
        out: dict[int, dict[str, Any]] = {}
        offset = None
        while True:
            body: dict[str, Any] = {
                "filter": {"must": [{"key": "doc_id", "match": {"value": doc_id}}]},
                "limit": 256,
                "with_payload": True,
            }
            if offset is not None:
                body["offset"] = offset
            result = await self._call("POST", f"/collections/{self.collection}/points/scroll", body)
            for p in (result or {}).get("points", []):
                payload = p.get("payload") or {}
                out[int(payload.get("chunk_index", -1))] = payload
            offset = (result or {}).get("next_page_offset")
            if not offset:
                return out

    async def ingest(
        self,
        origin: str,
        documents: list[Document],
        chunks_by_doc: dict[str, list[Chunk]],
        embedder: Any = None,
        seen_doc_ids: set[str] | None = None,
    ) -> IngestReport:
        """Upsert by (doc_id, chunk_index), skipping unchanged content.

        The three properties the pgvector store was built around are kept, and
        they are what separate a knowledge base that stays true from one that
        quietly rots:

        * **Incremental.** A chunk whose content hash AND embedding model are
          unchanged is not re-embedded. That is what makes a nightly refresh of
          a corpus that barely moved affordable.
        * **Deletions propagate.** Anything this origin no longer has is
          removed, so a decommissioned package stops being citable. Without
          this the agent confidently cites something that no longer exists,
          which is worse than not knowing.
        * **The embedding model is recorded per point.** Vectors from different
          models are not comparable, and a corpus holding two of them returns
          whichever rows happen to share the query's model — no error, no empty
          result, just consistently wrong retrieval.
        """
        report = IngestReport(origin=origin)
        if not self.ready:
            report.error = self.status
            return report
        model = embedder_name(embedder)

        try:
            for doc in documents:
                chunks = chunks_by_doc.get(doc.doc_id) or []
                existing = await self._points_for_doc(doc.doc_id)
                report.documents += 1

                stale: list[Chunk] = []
                fresh_ids: list[int] = []
                for chunk in chunks:
                    prior = existing.get(chunk.chunk_index)
                    pid = point_id(doc.doc_id, chunk.chunk_index)
                    fresh_ids.append(pid)
                    if (
                        prior
                        and prior.get("content_hash") == chunk.content_hash
                        and prior.get("embed_model") == model
                    ):
                        report.chunks_unchanged += 1
                        continue
                    stale.append(chunk)

                vectors: list[list[float]] = []
                if stale and embedder is not None:
                    try:
                        vectors = await embedder.embed([c.text for c in stale])
                        report.embeddings_called += len(stale)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("embedding failed for %s: %s", doc.doc_id, exc)
                        vectors = []

                if stale and vectors:
                    points = []
                    for chunk, vector in zip(stale, vectors, strict=False):
                        prior = existing.get(chunk.chunk_index) or {}
                        points.append(
                            {
                                "id": point_id(doc.doc_id, chunk.chunk_index),
                                "vector": vector,
                                "payload": {
                                    # The CHUNK's own identity, not the
                                    # document's: a chunker may attribute a
                                    # chunk to a different source or kind than
                                    # the file it came from (a code block
                                    # inside a runbook), and the pgvector store
                                    # persisted the chunk's fields for exactly
                                    # that reason.
                                    "doc_id": chunk.doc_id,
                                    "chunk_index": chunk.chunk_index,
                                    "source": chunk.source,
                                    "kind": chunk.kind,
                                    "origin": chunk.origin or origin,
                                    "chunk": chunk.text,
                                    "content_hash": chunk.content_hash,
                                    "embed_model": model,
                                    "metadata": chunk.metadata or {},
                                    # Feedback counters live on the point so a
                                    # vote survives a re-ingest of unchanged
                                    # content.
                                    "helpful": int(prior.get("helpful", 0)),
                                    "unhelpful": int(prior.get("unhelpful", 0)),
                                },
                            }
                        )
                    await self._call(
                        "PUT",
                        f"/collections/{self.collection}/points?wait=true",
                        {"points": points},
                    )
                    report.chunks_written += len(points)

                # The tail: a document that got SHORTER leaves points behind,
                # and an orphaned chunk is citable forever.
                fresh = set(fresh_ids)
                orphans = [
                    point_id(doc.doc_id, idx)
                    for idx in existing
                    if point_id(doc.doc_id, idx) not in fresh
                ]
                if orphans:
                    await self._call(
                        "POST",
                        f"/collections/{self.collection}/points/delete?wait=true",
                        {"points": orphans},
                    )
                    report.chunks_deleted += len(orphans)

            if seen_doc_ids is not None:
                removed = await self._forget_missing(origin, seen_doc_ids)
                report.chunks_deleted += removed
        except Exception as exc:  # noqa: BLE001
            log.warning("knowledge ingest failed for %s: %s", origin, exc)
            report.error = str(exc)
        return report

    async def _forget_missing(self, origin: str, seen: set[str]) -> int:
        """Delete every point of this origin whose document is gone."""
        if not seen:
            return 0
        body = {
            "filter": {
                "must": [{"key": "origin", "match": {"value": origin}}],
                "must_not": [{"key": "doc_id", "match": {"any": sorted(seen)}}],
            }
        }
        before = await self._count(body["filter"])
        if not before:
            return 0
        await self._call("POST", f"/collections/{self.collection}/points/delete?wait=true", body)
        return before

    async def _count(self, flt: dict[str, Any] | None = None) -> int:
        body: dict[str, Any] = {"exact": True}
        if flt:
            body["filter"] = flt
        result = await self._call("POST", f"/collections/{self.collection}/points/count", body)
        return int((result or {}).get("count", 0))

    # -------------------------------------------------------------- search --

    async def search(
        self,
        query: str,
        embedder: Any = None,
        k: int = 5,
        kinds: tuple[str, ...] = (),
    ) -> list[Hit]:
        """Dense vector retrieval, ranked with the same weighting as before.

        The lexical half is the caller's in-process BM25 index now; see the
        module docstring. `kind_weight` and the feedback nudge are applied here
        so a hit's score means the same thing it did under pgvector.
        """
        if not self.ready or not query.strip() or embedder is None:
            return []
        try:
            vectors = await embedder.embed([query])
        except Exception as exc:  # noqa: BLE001
            log.info("embedding unavailable (%s); no vector retrieval", exc)
            return []
        if not vectors:
            return []

        body: dict[str, Any] = {
            "vector": vectors[0],
            # Over-fetch, because the weighting below reorders: taking exactly k
            # from the engine and then reweighting cannot promote the chunk that
            # ranked k+1 on raw distance but is the authoritative source.
            "limit": max(k * 3, k),
            "with_payload": True,
        }
        if kinds:
            body["filter"] = {"must": [{"key": "kind", "match": {"any": list(kinds)}}]}
        try:
            result = await self._call(
                "POST", f"/collections/{self.collection}/points/search", body
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("knowledge search failed: %s", exc)
            return []

        hits: list[Hit] = []
        for rank, point in enumerate(result or []):
            payload = point.get("payload") or {}
            metadata = payload.get("metadata") or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError:
                    metadata = {}
            # Reciprocal rank, so a later fusion with the lexical index combines
            # two rankings on one scale — the same reason the pgvector store
            # used RRF rather than raw cosine distance.
            score = 1.0 / (RRF_K + rank + 1)
            votes = int(payload.get("helpful", 0)) - int(payload.get("unhelpful", 0))
            # Feedback is a nudge, not a verdict: one downvote must not bury the
            # only chunk that answers the question.
            feedback = 1.0 + max(-0.3, min(0.3, votes * 0.05))
            kind = str(payload.get("kind", "doc"))
            hits.append(
                Hit(
                    chunk_id=int(point.get("id", 0)),
                    doc_id=str(payload.get("doc_id", "")),
                    source=str(payload.get("source", "")),
                    kind=kind,
                    origin=str(payload.get("origin", "unknown")),
                    text=str(payload.get("chunk", "")),
                    metadata=metadata,
                    score=score * kind_weight(kind) * feedback,
                    retrieval="vector",
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    # ------------------------------------------------------------ feedback --

    async def record_feedback(self, chunk_ids: list[int], helpful: bool) -> int:
        """Record that these chunks did or did not help.

        Read-modify-write, because Qdrant has no atomic counter. Two votes
        arriving together can lose one; the counters are a retrieval NUDGE, not
        an audit trail, so a lost vote costs a fraction of a rank position.
        """
        if not self.ready or not chunk_ids:
            return 0
        field = "helpful" if helpful else "unhelpful"
        try:
            result = await self._call(
                "POST",
                f"/collections/{self.collection}/points",
                {"ids": list(chunk_ids), "with_payload": True},
            )
            updated = 0
            for point in result or []:
                payload = point.get("payload") or {}
                await self._call(
                    "POST",
                    f"/collections/{self.collection}/points/payload?wait=true",
                    {
                        "payload": {field: int(payload.get(field, 0)) + 1},
                        "points": [point.get("id")],
                    },
                )
                updated += 1
            return updated
        except Exception as exc:  # noqa: BLE001
            log.warning("could not record knowledge feedback: %s", exc)
            return 0

    # --------------------------------------------------------------- stats --

    async def stats(self) -> dict[str, Any]:
        """What the knowledge base holds, by origin and kind."""
        if not self.ready:
            return {"status": self.status, "origins": []}
        try:
            total = await self._count()
            info = await self._call("GET", f"/collections/{self.collection}")
            return {
                "status": self.status,
                "engine": "qdrant",
                "collection": self.collection,
                "chunks": total,
                "vectors": int((info or {}).get("vectors_count") or total),
                "origins": [],
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("knowledge stats failed: %s", exc)
            return {"status": f"degraded ({exc})", "origins": []}
