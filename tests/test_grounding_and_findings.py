"""Grounding without a key, and findings that survive a restart.

Two things the documentation described and the code did not do: the README
promised lexical degradation when no provider key is configured, and findings
were an in-process deque that a rollout erased.
"""

from __future__ import annotations

import pytest

from adhar_ai.rag import Document, KnowledgeBase, KnowledgeStore, LexicalIndex, tokenize
from adhar_ai.runtime.findings import Citation, Finding
from adhar_ai.runtime.store import FindingStore

DOCS = [
    Document(
        doc_id="adr:0024",
        source="adr/0024-agentic-ai-platform.md",
        kind="adr",
        origin="docs",
        text=(
            "## Decision\n\n"
            "Every mutation is a Git change against Gitea. There is no tool that calls "
            "kubectl apply, argocd app set, or a cloud API directly with mutating scope."
        ),
    ),
    Document(
        doc_id="adr:0025",
        source="adr/0025-ai-gateway-agentgateway.md",
        kind="adr",
        origin="docs",
        text=(
            "## Decision\n\n"
            "A PreRouting policy lifts the model out of the request body into a header "
            "and ordinary HTTPRoute matches fan out to Anthropic, OpenAI or the "
            "in-cluster vLLM backend."
        ),
    ),
    Document(
        doc_id="runbook:degraded",
        source="runbooks/degraded-app.md",
        kind="runbook",
        origin="docs",
        text=(
            "## Triage\n\n"
            "When an ArgoCD Application reports Degraded, check the pod events first, "
            "then the container logs, then whether the last sync succeeded."
        ),
    ),
]
CHUNKS = [c for d in DOCS for c in d.chunks()]


@pytest.fixture
def lexical() -> LexicalIndex:
    return LexicalIndex.from_chunks(CHUNKS)


def knowledge_base(lexical: LexicalIndex | None = None) -> KnowledgeBase:
    """A knowledge base with no database: the unkeyed, no-CNPG posture."""
    return KnowledgeBase(store=KnowledgeStore(dsn=""), embedder=None, lexical=lexical)


# --------------------------------------------------------------------------- #
# Lexical retrieval
# --------------------------------------------------------------------------- #


def test_the_tokenizer_keeps_identifiers_whole() -> None:
    """Technical prose is mostly identifiers; splitting `kube-system` into two
    stopword-adjacent fragments is what makes naive tokenizers useless here."""
    tokens = tokenize("Check the kube-system namespace and app.kubernetes.io/name label")
    assert "kube-system" in tokens
    assert "app.kubernetes.io/name" in tokens
    assert "the" not in tokens  # stopword
    assert "a" not in tokens  # single character


def test_lexical_search_finds_the_relevant_document(lexical: LexicalIndex) -> None:
    hits = lexical.search("why is my argocd application degraded", k=1)
    assert hits
    assert hits[0][0].source.startswith("runbooks/degraded-app.md")


def test_lexical_search_discriminates_between_two_adrs(lexical: LexicalIndex) -> None:
    routing = lexical.search("which backend does a model name route to", k=1)
    writes = lexical.search("can the agent run kubectl apply", k=1)
    assert "0025" in routing[0][0].source
    assert "0024" in writes[0][0].source


def test_a_query_matching_nothing_returns_nothing(lexical: LexicalIndex) -> None:
    """Zero-scoring chunks are dropped rather than padded to k, so the model is
    not handed irrelevant text presented as grounding."""
    assert lexical.search("quarterly marketing spend in EMEA", k=5) == []


def test_an_empty_index_is_harmless() -> None:
    empty = LexicalIndex.from_chunks([])
    assert empty.size == 0
    assert empty.search("anything", k=5) == []


def test_a_missing_docs_path_yields_an_empty_index() -> None:
    assert LexicalIndex.from_path("/nonexistent/docs").size == 0


async def test_the_knowledge_base_grounds_with_no_key_and_no_database(
    lexical: LexicalIndex,
) -> None:
    """The README's promise, now true: no key and no database still grounds."""
    kb = knowledge_base(lexical)
    hits = await kb.search("argocd application degraded", k=2)
    assert hits
    assert all("lexical" in h.retrieval for h in hits)
    assert "in-process lexical only" in kb.mode


async def test_grounding_blocks_carry_their_source_and_path(
    lexical: LexicalIndex,
) -> None:
    blocks = await knowledge_base(lexical).grounding("kubectl apply", k=1)
    assert blocks
    assert "0024-agentic-ai-platform.md" in blocks[0]
    assert "lexical" in blocks[0]


async def test_grounding_ids_are_empty_without_a_store(lexical: LexicalIndex) -> None:
    """Feedback needs real row ids. An in-process hit has none, and says so by
    returning nothing rather than a fake id that /feedback would silently drop."""
    blocks, ids = await knowledge_base(lexical).grounding_with_ids("kubectl apply", k=2)
    assert blocks
    assert ids == []


async def test_with_nothing_configured_the_mode_says_so() -> None:
    kb = knowledge_base(None)
    assert await kb.search("anything", k=3) == []
    assert "unavailable" in kb.mode


async def test_a_note_is_retrievable_immediately(lexical: LexicalIndex) -> None:
    """Someone writing up an outage at 02:00 must be able to ask about it at
    02:01, not after the next scheduled refresh."""
    kb = knowledge_base(lexical)
    result = await kb.add_note(
        title="Gitea token rotation broke the agent's write path",
        body="The adhar-ai-bot token expired; PRs failed with 401 until it was reissued.",
        kind="incident",
        author="ops",
    )
    assert result["retrievable"] is True
    assert result["durable"] is False  # no database in this posture
    hits = await kb.search("adhar-ai-bot token expired", k=3)
    assert any("token rotation" in h.source.lower() for h in hits)


# --------------------------------------------------------------------------- #
# Finding persistence
# --------------------------------------------------------------------------- #


def finding(identifier: str = "alert-triage-abc") -> Finding:
    return Finding(
        id=identifier,
        operator="alert-triage",
        title="checkout pods crash-looping",
        severity="critical",
        summary="Image pull failure on the new tag.",
        citations=[Citation(source="logs", kind="tool")],
    )


async def test_with_no_database_the_store_is_a_no_op() -> None:
    """`docker compose` and a bare `adhar-ai runtime` have no DSN. Persistence
    is an upgrade when the database is there, never a dependency."""
    store = FindingStore(dsn="")
    assert store.enabled is False
    assert "disabled" in store.status
    await store.prepare()
    await store.save(finding())  # must not raise
    assert await store.recent() == []


async def test_an_unreachable_database_degrades_to_memory_and_says_why() -> None:
    store = FindingStore(dsn="postgresql://nobody@127.0.0.1:1/none")
    await store.prepare()
    assert store.enabled is False
    assert "unavailable" in store.status
    await store.save(finding())
    assert await store.recent() == []


def test_the_table_name_must_be_an_identifier() -> None:
    """The name is interpolated into DDL. It comes from a ConfigMap rather than
    a user, but "not user input today" is not a reason to leave it uncheckable."""
    with pytest.raises(ValueError):
        FindingStore(dsn="postgresql://x/y", table="finding; DROP TABLE kb_chunk")
    assert FindingStore(dsn="postgresql://x/y", table="finding_v2").table == "finding_v2"


async def test_a_save_failure_never_breaks_the_run_that_produced_it() -> None:
    """The finding is already in the deque and the HTTP response; the durable
    copy is the only thing a database outage may cost."""
    store = FindingStore(dsn="postgresql://nobody@127.0.0.1:1/none")
    store.enabled = True  # pretend prepare() succeeded, then the DB went away
    await store.save(finding())  # must not raise
