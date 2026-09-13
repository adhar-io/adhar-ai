"""The pgvector knowledge store, against a real Postgres.

These need a database, because the properties that matter are properties of the
SQL: the upsert key, the model-aware staleness check, the hybrid fusion and the
delete propagation. A fake would assert that the code calls itself.

    docker run -d --name adhar-rag -p 15432:5432 \\
      -e POSTGRES_USER=adhar_ai -e POSTGRES_PASSWORD=adhar_ai \\
      -e POSTGRES_DB=adhar_ai_rag pgvector/pgvector:pg16

    ADHAR_AI_TEST_DSN=postgresql://adhar_ai:adhar_ai@127.0.0.1:15432/adhar_ai_rag \\
      uv run pytest tests/test_knowledge_store.py

Without `ADHAR_AI_TEST_DSN` they skip.
"""

from __future__ import annotations

import hashlib
import os

import pytest

from adhar_ai.rag import Document, KnowledgeStore
from adhar_ai.rag.store import embedder_name

DSN = os.environ.get("ADHAR_AI_TEST_DSN", "")
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not DSN, reason="set ADHAR_AI_TEST_DSN to run knowledge-store tests"),
]


class Embedder:
    """Deterministic, content-bearing vectors. Identical text embeds identically,
    which is what the staleness check relies on."""

    def __init__(self, name: str = "test", model: str = "v1") -> None:
        self.name = name
        self.model = model
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        out = []
        for text in texts:
            digest = hashlib.sha256(f"{self.model}:{text}".encode()).digest()
            vector = [((digest[i % len(digest)] / 255.0) - 0.5) for i in range(1536)]
            norm = sum(v * v for v in vector) ** 0.5 or 1.0
            out.append([v / norm for v in vector])
        return out


def doc(doc_id: str, text: str, kind: str = "doc", origin: str = "test") -> Document:
    return Document(doc_id=doc_id, source=doc_id, text=text, kind=kind, origin=origin)


@pytest.fixture
async def store():
    table = f"kb_test_{os.getpid()}"
    store = KnowledgeStore(DSN, table=table)
    assert await store.prepare()
    yield store
    import psycopg

    async with await psycopg.AsyncConnection.connect(DSN) as conn, conn.cursor() as cur:
        await cur.execute(f"DROP TABLE IF EXISTS {table}")
        await conn.commit()


# --------------------------------------------------------------------------- #
# Incremental ingestion
# --------------------------------------------------------------------------- #


async def test_a_second_ingest_of_unchanged_content_embeds_nothing(store) -> None:
    """What makes a nightly refresh affordable. Re-embedding an unchanged corpus
    would put keeping the index current and keeping the bill down in conflict."""
    embedder = Embedder()
    docs = [doc(f"d{i}", f"## Section\n\nbody number {i}") for i in range(5)]

    first = await store.ingest("test", docs, embedder)
    assert first.chunks_written == 5
    assert first.embeddings_called == 1

    embedder.calls = 0
    second = await store.ingest("test", docs, embedder)
    assert second.chunks_written == 0
    assert second.chunks_unchanged == 5
    assert embedder.calls == 0, "unchanged content must never be re-embedded"


async def test_only_the_changed_document_is_re_embedded(store) -> None:
    embedder = Embedder()
    docs = [doc(f"d{i}", f"## Section\n\nbody {i}") for i in range(4)]
    await store.ingest("test", docs, embedder)

    docs[2] = doc("d2", "## Section\n\nbody 2, rewritten")
    report = await store.ingest("test", docs, embedder)
    assert report.chunks_written == 1
    assert report.chunks_unchanged == 3


async def test_a_document_removed_from_its_origin_is_deleted(store) -> None:
    """Without this a decommissioned package stays retrievable forever and the
    agent confidently cites something that no longer exists."""
    embedder = Embedder()
    await store.ingest(
        "test",
        [doc("keep", "## A\n\nkeep me"), doc("drop", "## B\n\ndrop me")],
        embedder,
    )

    report = await store.ingest("test", [doc("keep", "## A\n\nkeep me")], embedder)
    assert report.chunks_deleted == 1
    hits = await store.search("drop me", embedder, k=5)
    assert not any(h.doc_id == "drop" for h in hits)


async def test_pruning_is_scoped_to_one_origin(store) -> None:
    """Adding a single note must not delete every other note, and refreshing the
    docs must not delete the findings."""
    embedder = Embedder()
    await store.ingest("docs", [doc("a", "## A\n\nfrom docs", origin="docs")], embedder)
    await store.ingest("notes", [doc("n", "## N\n\nfrom notes", origin="notes")], embedder)

    await store.ingest("docs", [doc("a", "## A\n\nfrom docs", origin="docs")], embedder)
    stats = await store.stats()
    origins = {o["origin"] for o in stats["origins"]}
    assert origins == {"docs", "notes"}


async def test_a_shrinking_document_drops_its_orphan_chunks(store) -> None:
    embedder = Embedder()
    long = doc("d", "## A\n\nfirst\n\n## B\n\nsecond\n\n## C\n\nthird")
    await store.ingest("test", [long], embedder)
    stats = await store.stats()
    assert stats["chunks"] == 3

    await store.ingest("test", [doc("d", "## A\n\nfirst")], embedder)
    stats = await store.stats()
    assert stats["chunks"] == 1, "chunks past the document's new end must be removed"


# --------------------------------------------------------------------------- #
# The embedding model is part of a row's identity
# --------------------------------------------------------------------------- #


async def test_changing_the_embedding_model_re_embeds_everything(store) -> None:
    """The bug this prevents is invisible from the outside.

    `load_embeddings` falls back gateway -> local -> none, so a cluster that
    loses its key starts writing vectors from a DIFFERENT space into the same
    table. Cosine distance between two models' vectors is noise, so retrieval
    silently returns whichever rows happen to share the query's model and
    ignores the rest — no error, no empty result, just consistently wrong
    answers.
    """
    first = Embedder(name="gateway", model="v1")
    docs = [doc(f"d{i}", f"## S\n\nbody {i}") for i in range(3)]
    await store.ingest("test", docs, first)

    swapped = Embedder(name="local", model="v2")
    report = await store.ingest("test", docs, swapped)
    assert report.chunks_written == 3, "a different model makes every row stale"
    assert report.chunks_unchanged == 0

    stats = await store.stats()
    models = {m["model"] for m in stats["embedModels"]}
    assert models == {embedder_name(swapped)}
    assert "warning" not in stats


async def test_a_mixed_index_is_reported_loudly(store) -> None:
    embedder_a = Embedder(name="gateway", model="v1")
    embedder_b = Embedder(name="local", model="v2")
    await store.ingest("a", [doc("a", "## A\n\nalpha", origin="a")], embedder_a)
    await store.ingest("b", [doc("b", "## B\n\nbeta", origin="b")], embedder_b)

    stats = await store.stats()
    assert len(stats["embedModels"]) == 2
    assert "warning" in stats
    assert "not comparable" in stats["warning"]


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #


async def test_lexical_retrieval_finds_an_exact_identifier(store) -> None:
    """Platform questions are full of exact identifiers that an embedding blurs.
    This is the half of the hybrid that finds them."""
    embedder = Embedder()
    await store.ingest(
        "test",
        [
            doc("x", "## Symptoms\n\nThe pod reports CreateContainerConfigError on start."),
            doc("y", "## Costs\n\nQuarterly spend by namespace, broken down."),
        ],
        embedder,
    )
    hits = await store.search("CreateContainerConfigError", embedder, k=3)
    assert hits
    assert hits[0].doc_id == "x"


async def test_retrieval_works_with_no_embedder_at_all(store) -> None:
    """An unkeyed install keeps full-text retrieval over the whole indexed
    corpus, which is far more than the in-process index covers."""
    embedder = Embedder()
    await store.ingest(
        "test",
        [doc("x", "## A\n\nKyverno policy exceptions are proposed as PRs")],
        embedder,
    )

    hits = await store.search("Kyverno policy exceptions", embedder=None, k=3)
    assert hits
    assert all(h.retrieval == "lexical" for h in hits)


async def test_a_kind_filter_narrows_retrieval(store) -> None:
    embedder = Embedder()
    await store.ingest(
        "test",
        [
            doc("r", "## Triage\n\nRestart the gateway and check the logs", kind="runbook"),
            doc("d", "## Notes\n\nRestart the gateway and check the logs", kind="doc"),
        ],
        embedder,
    )
    hits = await store.search("restart the gateway", embedder, k=5, kinds=("runbook",))
    assert hits
    assert {h.kind for h in hits} == {"runbook"}


# --------------------------------------------------------------------------- #
# Feedback
# --------------------------------------------------------------------------- #


async def test_feedback_is_recorded_and_raises_a_chunk_score(store) -> None:
    embedder = Embedder()
    await store.ingest(
        "test",
        [
            doc("a", "## A\n\nthe gitea bot token expired"),
            doc("b", "## B\n\nthe gitea bot token expired"),
        ],
        embedder,
    )
    before = await store.search("gitea bot token expired", embedder, k=2)
    assert len(before) == 2
    target = before[-1]

    assert await store.record_feedback([target.chunk_id], helpful=True) == 1
    after = await store.search("gitea bot token expired", embedder, k=2)
    raised = next(h for h in after if h.chunk_id == target.chunk_id)
    assert raised.score > target.score


async def test_feedback_cannot_bury_the_only_answer(store) -> None:
    """Bounded on purpose: one downvote must not remove the only document that
    answers a question."""
    embedder = Embedder()
    await store.ingest("test", [doc("a", "## A\n\nunique subject matter")], embedder)
    chunk_id = (await store.search("unique subject matter", embedder, k=1))[0].chunk_id

    for _ in range(20):
        await store.record_feedback([chunk_id], helpful=False)
    hits = await store.search("unique subject matter", embedder, k=1)
    assert hits and hits[0].chunk_id == chunk_id
    assert hits[0].score > 0


async def test_feedback_on_an_unknown_chunk_is_harmless(store) -> None:
    assert await store.record_feedback([999_999], helpful=True) == 0
    assert await store.record_feedback([], helpful=True) == 0
