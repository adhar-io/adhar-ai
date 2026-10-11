"""The Qdrant knowledge store, exercised against a fake transport.

No live Qdrant: httpx's MockTransport lets the REST contract itself be the
thing under test, which is what actually breaks — a wrong path, a filter shaped
the way the docs show for a different endpoint, an id that is not stable.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

from adhar_ai.rag.documents import Chunk, Document
from adhar_ai.rag.vector_store import QdrantKnowledgeStore, point_id


class FakeQdrant:
    """Enough of Qdrant to hold points and answer the calls the store makes."""

    def __init__(self, *, collection_dim: int | None = 1536) -> None:
        self.points: dict[int, dict] = {}
        self.collection_dim = collection_dim
        self.calls: list[tuple[str, str]] = []
        self.indexes: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = request.url.query.decode() if request.url.query else ""
        self.calls.append((request.method, path))
        body = json.loads(request.content) if request.content else {}

        if request.method == "GET" and path.startswith("/collections/"):
            if self.collection_dim is None:
                return httpx.Response(404, json={"status": {"error": "not found"}})
            return httpx.Response(200, json={"result": {
                "config": {"params": {"vectors": {"size": self.collection_dim}}},
                "vectors_count": len(self.points),
            }})
        if request.method == "PUT" and path.endswith("/index"):
            self.indexes.append(body.get("field_name", ""))
            return httpx.Response(200, json={"result": {}})
        if request.method == "PUT" and path == "/collections/kb":
            self.collection_dim = body["vectors"]["size"]
            return httpx.Response(200, json={"result": True})
        if request.method == "PUT" and path.startswith("/collections/kb/points"):
            for p in body["points"]:
                self.points[int(p["id"])] = {"vector": p["vector"], "payload": p["payload"]}
            return httpx.Response(200, json={"result": {"status": "completed"}})
        if path.endswith("/points/scroll"):
            doc = body["filter"]["must"][0]["match"]["value"]
            got = [
                {"id": pid, "payload": v["payload"]}
                for pid, v in self.points.items()
                if v["payload"].get("doc_id") == doc
            ]
            return httpx.Response(200, json={"result": {"points": got, "next_page_offset": None}})
        if path.endswith("/points/count"):
            flt = body.get("filter")
            if not flt:
                return httpx.Response(200, json={"result": {"count": len(self.points)}})
            origin = flt["must"][0]["match"]["value"]
            keep = set(flt.get("must_not", [{}])[0].get("match", {}).get("any", []))
            n = sum(
                1 for v in self.points.values()
                if v["payload"].get("origin") == origin and v["payload"].get("doc_id") not in keep
            )
            return httpx.Response(200, json={"result": {"count": n}})
        if path.endswith("/points/delete"):
            if "points" in body:
                for pid in body["points"]:
                    self.points.pop(int(pid), None)
            else:
                flt = body["filter"]
                origin = flt["must"][0]["match"]["value"]
                keep = set(flt["must_not"][0]["match"]["any"])
                for pid in [
                    pid
                    for pid, v in self.points.items()
                    if v["payload"].get("origin") == origin
                    and v["payload"].get("doc_id") not in keep
                ]:
                    self.points.pop(pid)
            return httpx.Response(200, json={"result": {"status": "completed"}})
        if path.endswith("/points/search"):
            kinds = None
            if "filter" in body:
                kinds = set(body["filter"]["must"][0]["match"]["any"])
            out = []
            for pid, v in self.points.items():
                if kinds is not None and v["payload"].get("kind") not in kinds:
                    continue
                out.append({"id": pid, "payload": v["payload"], "score": 0.9})
            return httpx.Response(200, json={"result": out[: body["limit"]]})
        if path.endswith("/points/payload"):
            for pid in body["points"]:
                self.points[int(pid)]["payload"].update(body["payload"])
            return httpx.Response(200, json={"result": {}})
        if path == "/collections/kb/points" and request.method == "POST":
            out = [
                {"id": pid, "payload": self.points[pid]["payload"]}
                for pid in body["ids"] if pid in self.points
            ]
            return httpx.Response(200, json={"result": out})
        return httpx.Response(404, json={"status": {"error": f"unhandled {path} {query}"}})


def store_for(fake: FakeQdrant) -> QdrantKnowledgeStore:
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return QdrantKnowledgeStore("http://qdrant:6333", collection="kb", dim=1536, client=client)


class Embedder:
    model = "text-embedding-3-small"
    name = "gateway"

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += len(texts)
        return [[0.1] * 1536 for _ in texts]


def doc(doc_id: str, kind: str = "doc") -> Document:
    return Document(doc_id=doc_id, source=doc_id, kind=kind, text="x", origin="docs", metadata={})


def chunk(i: int, text: str, doc_id: str = "docs/a.md", kind: str = "doc") -> Chunk:
    """A chunk whose `content_hash` is DERIVED from its text, as in production.

    The hash is a property of the content, so a test cannot set it directly —
    which is the point: "unchanged" must mean the text is unchanged, not that a
    caller passed the same string twice.
    """
    return Chunk(
        doc_id=doc_id, chunk_index=i, source=doc_id, kind=kind, origin="docs", text=text
    )


# A chunk's id must be the SAME ACROSS PROCESSES, or an ingest appends a second
# copy of the corpus instead of updating it — and the first run after a restart
# is when that shows up. Python's `hash()` is salted per process, so comparing
# two calls inside ONE process cannot detect it: the ids must be computed in two
# interpreters with different hash seeds.
def test_point_ids_are_identical_across_processes():
    import subprocess
    import sys

    prog = (
        "from adhar_ai.rag.vector_store import point_id;"
        "print(point_id('docs/a.md', 3))"
    )
    ids = set()
    for seed in ("0", "12345"):
        out = subprocess.run(
            [sys.executable, "-c", prog],
            capture_output=True, text=True, check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        ids.add(out.stdout.strip())
    assert len(ids) == 1, f"point_id differs between interpreters: {ids}"


def test_point_ids_are_stable_and_positive():
    a = point_id("docs/a.md", 0)
    assert a == point_id("docs/a.md", 0)
    assert a != point_id("docs/a.md", 1)
    assert a != point_id("docs/b.md", 0)
    assert 0 < a < 2**63


@pytest.mark.asyncio
async def test_prepare_creates_the_collection_and_its_indexes():
    fake = FakeQdrant(collection_dim=None)  # nothing exists yet
    store = store_for(fake)
    assert await store.prepare() is True
    assert store.ready and "kb" in store.status
    assert fake.collection_dim == 1536
    # Without these, every filtered read during ingest is a full scan.
    assert set(fake.indexes) == {"doc_id", "origin", "kind"}


# A collection of the wrong width must NOT be used and must NOT be recreated:
# recreating silently discards the corpus, and using it matches nothing, because
# cosine distance between different widths is meaningless.
@pytest.mark.asyncio
async def test_a_collection_of_the_wrong_width_is_refused_not_recreated():
    fake = FakeQdrant(collection_dim=768)
    store = store_for(fake)
    assert await store.prepare() is False
    assert store.ready is False
    assert "768" in store.status and "1536" in store.status
    assert fake.collection_dim == 768  # untouched


@pytest.mark.asyncio
async def test_ingest_writes_once_and_skips_unchanged_content():
    fake = FakeQdrant()
    store = store_for(fake)
    await store.prepare()
    emb = Embedder()

    docs = [doc("docs/a.md")]
    chunks = {"docs/a.md": [chunk(0, "hello"), chunk(1, "world")]}
    first = await store.ingest("docs", docs, chunks, embedder=emb, seen_doc_ids={"docs/a.md"})
    assert first.chunks_written == 2 and first.embeddings_called == 2
    assert len(fake.points) == 2

    # Same content, same model: nothing re-embedded. This is what makes a
    # nightly refresh of a corpus that barely moved affordable.
    second = await store.ingest("docs", docs, chunks, embedder=emb, seen_doc_ids={"docs/a.md"})
    assert second.chunks_unchanged == 2
    assert second.chunks_written == 0
    assert emb.calls == 2, "unchanged chunks were re-embedded"


@pytest.mark.asyncio
async def test_a_changed_chunk_is_re_embedded_and_replaced_in_place():
    fake = FakeQdrant()
    store = store_for(fake)
    await store.prepare()
    emb = Embedder()
    docs = [doc("docs/a.md")]
    await store.ingest("docs", docs, {"docs/a.md": [chunk(0, "old")]}, embedder=emb)
    pid = point_id("docs/a.md", 0)
    await store.ingest("docs", docs, {"docs/a.md": [chunk(0, "new")]}, embedder=emb)
    # Replaced, not duplicated — the id is derived from identity.
    assert len(fake.points) == 1
    assert fake.points[pid]["payload"]["chunk"] == "new"


# A document that got SHORTER leaves points behind, and an orphaned chunk stays
# citable forever — the agent quoting a paragraph that no longer exists.
@pytest.mark.asyncio
async def test_a_shortened_document_loses_its_tail():
    fake = FakeQdrant()
    store = store_for(fake)
    await store.prepare()
    emb = Embedder()
    docs = [doc("docs/a.md")]
    await store.ingest("docs", docs, {"docs/a.md": [chunk(0, "a"), chunk(1, "b")]}, embedder=emb)
    assert len(fake.points) == 2
    report = await store.ingest("docs", docs, {"docs/a.md": [chunk(0, "a")]}, embedder=emb)
    assert report.chunks_deleted == 1
    assert len(fake.points) == 1


# Deletions must propagate, or a decommissioned package stays retrievable and
# the agent cites something that no longer exists.
@pytest.mark.asyncio
async def test_a_document_the_origin_no_longer_has_is_forgotten():
    fake = FakeQdrant()
    store = store_for(fake)
    await store.prepare()
    emb = Embedder()
    await store.ingest(
        "docs",
        [doc("docs/a.md"), doc("docs/gone.md")],
        {"docs/a.md": [chunk(0, "a")], "docs/gone.md": [chunk(0, "g", doc_id="docs/gone.md")]},
        embedder=emb,
        seen_doc_ids={"docs/a.md", "docs/gone.md"},
    )
    assert len(fake.points) == 2
    report = await store.ingest(
        "docs", [doc("docs/a.md")], {"docs/a.md": [chunk(0, "a")]},
        embedder=emb, seen_doc_ids={"docs/a.md"},
    )
    assert report.chunks_deleted >= 1
    assert all(v["payload"]["doc_id"] == "docs/a.md" for v in fake.points.values())


# Changing the embedding model invalidates every vector: cosine distance across
# models is noise, so the corpus must be re-embedded rather than silently mixed.
@pytest.mark.asyncio
async def test_a_new_embedding_model_re_embeds_unchanged_content():
    fake = FakeQdrant()
    store = store_for(fake)
    await store.prepare()
    old = Embedder()
    await store.ingest("docs", [doc("docs/a.md")], {"docs/a.md": [chunk(0, "a")]}, embedder=old)

    class Other(Embedder):
        model = "nomic-embed-text"

    new = Other()
    report = await store.ingest(
        "docs", [doc("docs/a.md")], {"docs/a.md": [chunk(0, "a")]}, embedder=new
    )
    assert report.chunks_written == 1, "a model change must not reuse the old vectors"
    payload = fake.points[point_id("docs/a.md", 0)]["payload"]
    assert payload["embed_model"].endswith("nomic-embed-text")


@pytest.mark.asyncio
async def test_search_filters_by_kind_and_weights_the_result():
    fake = FakeQdrant()
    store = store_for(fake)
    await store.prepare()
    emb = Embedder()
    await store.ingest(
        "docs",
        [doc("docs/a.md", kind="adr"), doc("docs/b.md", kind="doc")],
        {
            "docs/a.md": [chunk(0, "alpha", kind="adr")],
            "docs/b.md": [chunk(0, "beta", doc_id="docs/b.md")],
        },
        embedder=emb,
    )
    hits = await store.search("alpha", embedder=emb, k=5)
    assert {h.kind for h in hits} == {"adr", "doc"}
    assert all(h.score > 0 and h.retrieval == "vector" for h in hits)

    only = await store.search("alpha", embedder=emb, k=5, kinds=("adr",))
    assert [h.kind for h in only] == ["adr"]


# With no embedder there is no vector retrieval; the caller falls back to its
# lexical index. It must return empty, not raise.
@pytest.mark.asyncio
async def test_search_without_an_embedder_returns_nothing():
    fake = FakeQdrant()
    store = store_for(fake)
    await store.prepare()
    assert await store.search("anything", embedder=None) == []


@pytest.mark.asyncio
async def test_feedback_is_counted_and_survives_a_reingest():
    fake = FakeQdrant()
    store = store_for(fake)
    await store.prepare()
    emb = Embedder()
    await store.ingest("docs", [doc("docs/a.md")], {"docs/a.md": [chunk(0, "a")]}, embedder=emb)
    pid = point_id("docs/a.md", 0)
    assert await store.record_feedback([pid], helpful=True) == 1
    assert fake.points[pid]["payload"]["helpful"] == 1
    # Re-ingesting CHANGED content must carry the vote across, or every edit
    # silently resets what users told us about the chunk.
    await store.ingest("docs", [doc("docs/a.md")], {"docs/a.md": [chunk(0, "a2")]}, embedder=emb)
    assert fake.points[pid]["payload"]["helpful"] == 1


# An unreachable store must disable retrieval, not raise into the request.
@pytest.mark.asyncio
async def test_an_unreachable_store_degrades():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    store = QdrantKnowledgeStore("http://qdrant:6333", collection="kb", client=client)
    assert await store.prepare() is False
    assert store.ready is False
    assert await store.search("x", embedder=Embedder()) == []
    assert await store.record_feedback([1], helpful=True) == 0
    assert (await store.stats())["origins"] == []


# No URL at all is the normal state on a platform with no Vector composed.
@pytest.mark.asyncio
async def test_with_no_url_the_store_is_simply_disabled():
    store = QdrantKnowledgeStore("")
    assert await store.prepare() is False
    assert "disabled" in store.status
    assert await store.search("x", embedder=Embedder()) == []
