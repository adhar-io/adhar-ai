"""Reranking and query rewriting, and the one property each must keep.

Both are optimisations over retrieval that already works, so the invariant that
matters is **fail-open**: a model that answers with prose, an empty list, a
list of nonsense, or an exception must leave retrieval exactly as it was. A
rerank that could lose a hit, or a rewrite that could replace the question,
would turn a quality feature into an availability risk.
"""

from __future__ import annotations

import json

import pytest

from adhar_ai.rag import KnowledgeBase
from adhar_ai.rag.documents import Chunk
from adhar_ai.rag.lexical import LexicalIndex
from adhar_ai.rag.retrieval import GatewayQueryRewriter, GatewayReranker, fuse
from adhar_ai.rag.store import Hit, KnowledgeStore


def _hit(i: int, text: str = "", kind: str = "doc") -> Hit:
    return Hit(
        chunk_id=i,
        doc_id=f"d{i}",
        source=f"source {i}",
        kind=kind,
        origin="test",
        text=text or f"text {i}",
        score=1.0 / (i + 1),
        retrieval="vector",
        metadata={},
    )


class ScriptedGateway:
    """Answers each `chat` with the next scripted completion."""

    api_base = "http://gateway"

    def __init__(self, replies: list[str | Exception]) -> None:
        self.replies = list(replies)
        self.requests: list[dict] = []

    async def chat(self, messages, tools, tenant, model=None, max_tokens=4096, bearer=""):
        self.requests.append(
            {
                "prompt": messages[-1].content,
                "tenant": tenant,
                "model": model,
                "max_tokens": max_tokens,
                "bearer": bearer,
            }
        )
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"choices": [{"message": {"role": "assistant", "content": reply}}]}


# ----------------------------------------------------------------- rerank ---


async def test_rerank_applies_the_models_order_and_marks_the_hits():
    gateway = ScriptedGateway(["[2, 0]"])
    hits = [_hit(0), _hit(1), _hit(2)]
    out = await GatewayReranker(gateway, model="m").rerank("q", hits, k=3)

    assert [h.chunk_id for h in out] == [2, 0, 1], "ranked first, then the omitted one in order"
    assert out[0].retrieval == "vector+rerank" and out[1].retrieval == "vector+rerank"
    assert out[2].retrieval == "vector", "an omitted candidate is not claimed as reranked"
    assert gateway.requests[0]["model"] == "m"
    assert gateway.requests[0]["max_tokens"] <= 512, "a rank is a few tokens, not an essay"


async def test_rerank_narrows_to_k():
    gateway = ScriptedGateway(["[3, 1, 2, 0]"])
    hits = [_hit(i) for i in range(4)]
    out = await GatewayReranker(gateway).rerank("q", hits, k=2)
    assert [h.chunk_id for h in out] == [3, 1]


async def test_rerank_tolerates_a_fenced_or_chatty_reply():
    gateway = ScriptedGateway(["Sure! Here you go:\n```json\n[1, 0]\n```"])
    out = await GatewayReranker(gateway).rerank("q", [_hit(0), _hit(1)], k=2)
    assert [h.chunk_id for h in out] == [1, 0]


@pytest.mark.parametrize(
    "reply",
    ["I think the second one is best.", "[]", "[99, -1, 'x']", '{"not": "a list"}', ""],
)
async def test_rerank_falls_back_to_retrieval_order_on_a_useless_reply(reply):
    """Fail open. The hits the retriever found are never lost to a bad rank."""
    gateway = ScriptedGateway([reply])
    hits = [_hit(0), _hit(1), _hit(2)]
    reranker = GatewayReranker(gateway)
    out = await reranker.rerank("q", hits, k=3)
    assert [h.chunk_id for h in out] == [0, 1, 2]
    assert reranker.failures == 1


async def test_rerank_falls_back_when_the_gateway_raises():
    reranker = GatewayReranker(ScriptedGateway([RuntimeError("gateway down")]))
    out = await reranker.rerank("q", [_hit(0), _hit(1)], k=2)
    assert [h.chunk_id for h in out] == [0, 1]
    assert reranker.failures == 1


async def test_rerank_does_not_spend_a_completion_on_one_candidate():
    gateway = ScriptedGateway([])
    out = await GatewayReranker(gateway).rerank("q", [_hit(0)], k=3)
    assert len(out) == 1 and gateway.requests == []


async def test_rerank_shows_the_model_a_bounded_opening_of_each_candidate():
    gateway = ScriptedGateway(["[0]"])
    long = _hit(0, text="word " * 2000)
    await GatewayReranker(gateway).rerank("q", [long, _hit(1)], k=2)
    prompt = gateway.requests[0]["prompt"]
    assert len(prompt) < 2500, "a rerank prompt must stay small or it costs more than it saves"
    assert "source 0" in prompt and "source 1" in prompt


# ---------------------------------------------------------------- rewrite ---


async def test_rewrite_returns_platform_vocabulary_plus_terms():
    gateway = ScriptedGateway(
        [json.dumps({"query": "checkout Application OutOfSync Degraded", "terms": ["repo-server"]})]
    )
    out = await GatewayQueryRewriter(gateway).rewrite("why is checkout down?")
    assert out == "checkout Application OutOfSync Degraded repo-server"


async def test_rewrite_is_cached_by_question():
    """The same question twice costs one completion, not two."""
    gateway = ScriptedGateway([json.dumps({"query": "x y z", "terms": []})])
    rewriter = GatewayQueryRewriter(gateway)
    first = await rewriter.rewrite("Why is checkout down?")
    # The same question, differently spaced and cased.
    second = await rewriter.rewrite("why is  checkout down?  ")
    assert first == second == "x y z"
    assert rewriter.calls == 1


async def test_a_rewrite_identical_to_the_question_is_not_a_rewrite():
    gateway = ScriptedGateway([json.dumps({"query": "list the enabled packages", "terms": []})])
    out = await GatewayQueryRewriter(gateway).rewrite("list the enabled packages")
    assert out is None, "a second retrieval of the same query buys nothing"


@pytest.mark.parametrize("reply", ["not json at all", "[1, 2]", "", '{"query": ""}'])
async def test_rewrite_falls_back_to_none_on_a_useless_reply(reply):
    rewriter = GatewayQueryRewriter(ScriptedGateway([reply]))
    assert await rewriter.rewrite("q") is None


async def test_rewrite_falls_back_when_the_gateway_raises():
    rewriter = GatewayQueryRewriter(ScriptedGateway([RuntimeError("down")]))
    assert await rewriter.rewrite("q") is None
    assert rewriter.failures == 1


# ------------------------------------------------------------------- fuse ---


def test_fuse_ranks_a_hit_both_queries_found_above_one_either_found():
    shared, only_a, only_b = _hit(1), _hit(2), _hit(3)
    out = fuse([only_a, shared], [shared, only_b], k=3)
    assert out[0].chunk_id == 1, "found by both"
    assert {h.chunk_id for h in out} == {1, 2, 3}


def test_fuse_cannot_bury_the_originals_first_hit_below_the_rewrites_strays():
    """The rewrite may add or promote; it may not demote what the question
    itself ranked first below things only the rewrite found."""
    original = [_hit(1), _hit(2)]
    rewrite = [_hit(7), _hit(8), _hit(9)]
    out = fuse(original, rewrite, k=5)
    # Rank 0 of each list ties; the original's is listed first and wins the tie.
    assert out[0].chunk_id in (1, 7)
    assert out.index(next(h for h in out if h.chunk_id == 1)) <= 1


# ------------------------------------------------- knowledge base, end to end


def _lexical_kb(chunks: list[Chunk]) -> KnowledgeBase:
    kb = KnowledgeBase(store=KnowledgeStore("", table="kb_chunk"), embedder=None, sources=[])
    kb.lexical = LexicalIndex.from_chunks(chunks)
    return kb


CHUNKS = [
    Chunk("doc:argo", 0, "ARCHITECTURE.md#ArgoCD", "doc", "docs", "ArgoCD sync and health notes"),
    Chunk("man:checkout", 0, "manifest checkout: Deployment", "manifest", "manifests",
          "checkout Deployment OutOfSync Degraded repo-server replicas"),
    Chunk("doc:cost", 0, "COST.md#Budgets", "doc", "docs", "OpenCost budgets and showback"),
]


async def test_search_without_a_gateway_is_exactly_the_old_search():
    kb = _lexical_kb(CHUNKS)
    hits = await kb.search("ArgoCD sync", k=2)
    assert hits and hits[0].doc_id == "doc:argo"
    assert all("rerank" not in h.retrieval for h in hits)


async def test_a_rewrite_finds_what_the_question_alone_does_not():
    """The point of the feature, end to end over the lexical index."""
    kb = _lexical_kb(CHUNKS)
    # The literal question shares no token with the manifest chunk...
    question = "why is the shop broken"
    assert not any(h.doc_id == "man:checkout" for h in await kb.search(question, k=3))
    # ...but its rewrite into platform vocabulary does.
    kb.rewriter = GatewayQueryRewriter(
        ScriptedGateway(
            [json.dumps({"query": "checkout OutOfSync Degraded", "terms": ["repo-server"]})]
        )
    )
    hits = await kb.search(question, k=3)
    assert any(h.doc_id == "man:checkout" for h in hits)


async def test_a_rerank_reorders_the_candidates_the_search_found():
    kb = _lexical_kb(CHUNKS)
    kb.candidates = 10
    before = await kb.search("ArgoCD OpenCost", k=5)
    assert len(before) == 2, "both the argo and cost chunks are candidates"
    # The model prefers whichever retrieval ranked SECOND.
    kb.reranker = GatewayReranker(ScriptedGateway(["[1]"]))
    hits = await kb.search("ArgoCD OpenCost", k=1)
    assert len(hits) == 1
    assert hits[0].doc_id == before[1].doc_id
    assert hits[0].retrieval.endswith("+rerank")


def test_fuse_collapses_the_same_chunk_seen_by_two_retrievers():
    """A pgvector hit carries its row id; the in-process BM25 hit of the same
    chunk carries -1. They are one chunk, and must take one slot."""
    stored = _hit(1, text="keycloak binds the db secret")
    stored.doc_id, stored.source = "doc:x", "manifest x"
    lexical = Hit(
        chunk_id=-1,
        doc_id="doc:x",
        source="manifest x",
        kind="manifest",
        origin="m",
        text="keycloak binds the db secret",
        score=3.0,
        retrieval="lexical (in-process)",
        metadata={},
    )
    out = fuse([stored], [lexical], k=5)
    assert len(out) == 1
    credited = out[0].retrieval
    assert "lexical" in credited and "vector" in credited, "both voters are credited"


def test_the_json_extractor_takes_the_earliest_value_not_the_first_array():
    """`{"query": …, "terms": […]}` used to come back as the terms array."""
    from adhar_ai.rag.retrieval import _first_json

    payload = _first_json('Sure: {"query": "a b", "terms": ["x", "y"]} done')
    assert payload == {"query": "a b", "terms": ["x", "y"]}
    assert _first_json("ranking: [2, 0, 1]") == [2, 0, 1]


# --------------------------------------- the run's token travels with retrieval


async def test_rerank_and_rewrite_carry_the_runs_bearer():
    """Both are completions through the same Strict-JWT gateway as the agent
    loop. Sent without the token they are 401s — invisible here because the
    fallback hides them, and fatal elsewhere because each one counted against
    the model's shared circuit breaker (AWS cluster, 2026-10-09)."""
    gateway = ScriptedGateway(["[1, 0]", '{"query": "argocd sync", "terms": ["degraded"]}'])
    await GatewayReranker(gateway).rerank("q", [_hit(0), _hit(1)], 1, bearer="run-token")
    await GatewayQueryRewriter(gateway).rewrite("why is the shop broken", bearer="run-token")
    assert [r["bearer"] for r in gateway.requests] == ["run-token", "run-token"]


async def test_search_threads_the_bearer_into_both_helpers():
    gateway = ScriptedGateway(['{"query": "checkout Deployment OutOfSync", "terms": []}', "[1]"])
    kb = _lexical_kb(CHUNKS)
    kb.rewriter = GatewayQueryRewriter(gateway)
    kb.reranker = GatewayReranker(gateway)
    hits = await kb.search("ArgoCD sync", k=1, bearer="run-token")
    assert hits, "the fused candidates were lost"
    assert len(gateway.requests) == 2, "both the rewrite and the rerank should have run"
    assert all(r["bearer"] == "run-token" for r in gateway.requests)


async def test_grounding_passes_the_bearer_to_search(monkeypatch):
    seen: list[str] = []

    async def fake_search(self, query, k=5, kinds=(), *, bearer=""):
        seen.append(bearer)
        return []

    async def no_graph(self, query):
        return []

    monkeypatch.setattr(KnowledgeBase, "search", fake_search)
    monkeypatch.setattr(KnowledgeBase, "graph_context", no_graph)
    kb = _lexical_kb(CHUNKS)
    await kb.grounding_with_ids("q", bearer="run-token")
    await kb.grounding("q", bearer="run-token")
    assert seen == ["run-token", "run-token"]
