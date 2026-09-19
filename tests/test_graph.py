"""The knowledge graph: extraction, traversal and grounding.

Split in two. The pure parts — node identity, term extraction, grounding
rendering, graceful degradation — run everywhere. The traversal itself is
recursive SQL, so testing it against anything but Postgres would assert that
the code calls itself; those tests need `ADHAR_AI_TEST_DSN` and skip without it.

    docker run -d --name adhar-rag -p 15432:5432 \\
      -e POSTGRES_USER=adhar_ai -e POSTGRES_PASSWORD=adhar_ai \\
      -e POSTGRES_DB=adhar_ai_rag pgvector/pgvector:pg16

    ADHAR_AI_TEST_DSN=postgresql://adhar_ai:adhar_ai@127.0.0.1:15432/adhar_ai_rag \\
      uv run pytest tests/test_graph.py
"""

from __future__ import annotations

import os

import pytest

from adhar_ai.rag.extract import ToolGraphSource, candidate_terms
from adhar_ai.rag.graph import (
    DEPENDENCY_RELATIONS,
    Edge,
    KnowledgeGraph,
    Neighbour,
    Node,
    as_grounding,
    node_id,
)

DSN = os.environ.get("ADHAR_AI_TEST_DSN", "")
needs_db = pytest.mark.skipif(not DSN, reason="set ADHAR_AI_TEST_DSN to run graph tests")


# ------------------------------------------------------------- pure parts --


def test_node_identity_is_stable_so_re_derivation_updates_rather_than_duplicates():
    """The graph is rebuilt from live state on a schedule.

    If the id were not a pure function of what the node *is*, every refresh
    would double the graph.
    """
    assert node_id("package", "keycloak") == node_id("package", "keycloak")
    assert node_id("Service", "argocd", "adhar-system") != node_id("Service", "argocd")
    assert node_id("package", "keycloak") != node_id("Service", "keycloak")


def test_a_node_labels_itself_with_its_namespace_when_it_has_one():
    node = Node(id="x", kind="Service", name="argocd", namespace="adhar-system")
    assert "in `adhar-system`" in node.label()
    assert "in `" not in Node(id="x", kind="package", name="keycloak").label()


def test_dependency_relations_are_a_closed_set():
    """Blast radius follows dependency edges only.

    Following every relation returns the whole connected component, which for
    a platform is very nearly everything and therefore answers nothing.
    """
    assert "depends_on" in DEPENDENCY_RELATIONS
    assert "mentions" not in DEPENDENCY_RELATIONS
    assert "documented_by" not in DEPENDENCY_RELATIONS


def test_candidate_terms_pulls_the_names_a_question_is_about():
    terms = candidate_terms(
        "what breaks if the keycloak package goes down in adhar-system?"
    )
    assert "keycloak" in terms
    assert "adhar-system" in terms
    # Stopwords are not entities.
    assert "the" not in terms and "if" not in terms


def test_candidate_terms_keeps_backticked_and_hyphenated_identifiers():
    terms = candidate_terms("is `argo-cd-repo-server` the thing that is OOMKilled?")
    assert "argo-cd-repo-server" in terms


def test_candidate_terms_is_bounded():
    terms = candidate_terms(" ".join(f"service-{i}" for i in range(100)), limit=12)
    assert len(terms) <= 12


def test_grounding_groups_by_relation_rather_than_listing_edges():
    """A model reading grouped prose answers; one reading edge tuples describes
    the tuples."""
    anchor = Node(id="package:console", kind="package", name="console")
    def near(node_id: str, kind: str, name: str, relation: str, direction: str) -> Neighbour:
        return Neighbour(
            node=Node(id=node_id, kind=kind, name=name),
            depth=1,
            relation=relation,
            direction=direction,
        )

    neighbours = [
        near("package:keycloak", "package", "keycloak", "depends_on", "out"),
        near("package:cnpg", "package", "cnpg", "depends_on", "out"),
        near("app:console", "Application", "console", "owns", "in"),
    ]
    text = as_grounding(anchor, neighbours)

    assert "console" in text
    assert "keycloak" in text and "cnpg" in text
    # Both relations are represented, and the block is prose rather than tuples.
    assert "depends_on" in text.replace(" ", "_") or "depends on" in text
    assert "->" not in text and "('" not in text


def test_grounding_for_an_isolated_node_says_so_rather_than_rendering_nothing():
    text = as_grounding(Node(id="package:lonely", kind="package", name="lonely"), [])
    assert "lonely" in text
    assert text.strip()


def test_an_invalid_table_prefix_is_refused_rather_than_interpolated():
    """Table names cannot be parameterised, so they are validated instead."""
    with pytest.raises(ValueError, match="prefix"):
        KnowledgeGraph(dsn="", prefix="kb; DROP TABLE users --")


async def test_a_graph_with_no_database_answers_empty_rather_than_raising():
    """Without Postgres the platform still has to answer — just without a graph."""
    graph = KnowledgeGraph(dsn="")
    assert await graph.prepare() is False
    assert graph.ready is False
    assert await graph.resolve(["keycloak"]) == []
    assert await graph.neighbourhood("package:keycloak") == []
    assert await graph.dependents("package:keycloak") == []

    stats = await graph.stats()
    assert stats["nodes"] == 0
    # A stable shape: `/knowledge` renders these keys unconditionally.
    assert stats["byKind"] == [] and stats["byRelation"] == []
    assert "no database" in stats["status"]


async def test_the_tool_source_turns_the_agents_own_tools_into_a_graph():
    """Makes "which of your tools could tell me about X" a traversal.

    Derived from the real MCP servers, so it cannot drift from what the agent
    can actually call — which a second hand-written list always does.
    """
    nodes, edges = await ToolGraphSource().extract()

    tool_names = {n.name for n in nodes if n.kind == "Tool"}
    domains = {n.name for n in nodes if n.kind == "Domain"}
    assert {"app_status", "propose_change"} <= tool_names
    assert "gitops" in domains

    # Every edge must land on a node that exists, or a traversal returns ids
    # that resolve to nothing.
    ids = {n.id for n in nodes}
    assert edges and all(e.src in ids and e.dst in ids for e in edges)

    # Write access is carried on the node, so the catalogue can say which tools
    # open a pull request without a second source of truth.
    propose = next(n for n in nodes if n.name == "propose_change")
    assert propose.attributes["access"] == "write"


# ------------------------------------------------------- against Postgres --


@pytest.fixture
async def graph():
    graph = KnowledgeGraph(dsn=DSN, prefix="kb_graph_test")
    assert await graph.prepare() is True
    yield graph
    import psycopg

    async with await psycopg.AsyncConnection.connect(DSN) as conn, conn.cursor() as cur:
        await cur.execute("DROP TABLE IF EXISTS kb_graph_test_edge, kb_graph_test_node")


def _platform() -> tuple[list[Node], list[Edge]]:
    """A small platform: console depends on keycloak, which depends on cnpg."""
    nodes = [
        Node(id=node_id("package", name), kind="package", name=name, origin="packages")
        for name in ("console", "keycloak", "cnpg", "argocd", "unrelated", "runbook")
    ]
    def edge(src: str, dst: str, relation: str) -> Edge:
        return Edge(src=src, dst=dst, relation=relation, origin="packages")

    edges = [
        edge("package:console", "package:keycloak", "depends_on"),
        edge("package:keycloak", "package:cnpg", "depends_on"),
        edge("package:argocd", "package:keycloak", "depends_on"),
        edge("package:console", "package:argocd", "mentions"),
        # `runbook` reaches cnpg ONLY through a non-dependency edge. A runbook
        # that mentions a database does not break when the database does.
        edge("package:runbook", "package:cnpg", "mentions"),
    ]
    return nodes, edges


@pytest.mark.slow
@needs_db
async def test_a_node_resolves_by_exact_name(graph):
    nodes, edges = _platform()
    await graph.replace_origin("packages", nodes, edges)

    resolved = await graph.resolve(["keycloak"])
    assert [n.id for n in resolved] == ["package:keycloak"]
    # Fuzzy resolution is deliberately absent: a blast radius computed from the
    # wrong node is confidently wrong.
    assert await graph.resolve(["keycloack"]) == []


@pytest.mark.slow
@needs_db
async def test_traversal_reaches_both_directions_and_respects_depth(graph):
    nodes, edges = _platform()
    await graph.replace_origin("packages", nodes, edges)

    one_hop = {n.node.id for n in await graph.neighbourhood("package:keycloak", depth=1)}
    assert one_hop == {"package:console", "package:cnpg", "package:argocd"}
    # Traversal follows every relation, unlike blast radius: "what is near
    # keycloak" and "what breaks without keycloak" are different questions.
    assert "package:runbook" in {
        n.node.id for n in await graph.neighbourhood("package:cnpg", depth=1)
    }

    two_hops = {n.node.id for n in await graph.neighbourhood("package:keycloak", depth=2)}
    assert "package:unrelated" not in two_hops


@pytest.mark.slow
@needs_db
async def test_blast_radius_walks_backwards_along_dependency_edges_only(graph):
    """"What breaks if keycloak goes?" is an upstream question."""
    nodes, edges = _platform()
    await graph.replace_origin("packages", nodes, edges)

    dependents = {n.node.id for n in await graph.dependents("package:cnpg", depth=3)}
    # console -> keycloak -> cnpg, so both are downstream of a cnpg outage.
    assert {"package:keycloak", "package:console", "package:argocd"} <= dependents
    # `mentions` is not a dependency, so nothing arrives through it alone —
    # `runbook` is attached to cnpg by exactly one `mentions` edge.
    assert "package:runbook" not in dependents
    assert "package:unrelated" not in dependents


@pytest.mark.slow
@needs_db
async def test_a_cycle_terminates_rather_than_recursing_forever(graph):
    """Platform graphs have cycles. A traversal that does not expect one hangs."""
    nodes = [
        Node(id=node_id("package", n), kind="package", name=n, origin="packages")
        for n in ("a", "b", "c")
    ]
    edges = [
        Edge(src="package:a", dst="package:b", relation="depends_on", origin="packages"),
        Edge(src="package:b", dst="package:c", relation="depends_on", origin="packages"),
        Edge(src="package:c", dst="package:a", relation="depends_on", origin="packages"),
    ]
    await graph.replace_origin("packages", nodes, edges)

    reached = {n.node.id for n in await graph.neighbourhood("package:a", depth=5)}
    assert reached == {"package:b", "package:c"}


@pytest.mark.slow
@needs_db
async def test_refreshing_one_origin_leaves_the_others_alone(graph):
    """The graph is rebuilt per source on its own schedule.

    A refresh of the package source that wiped the cluster source would empty
    the graph between scrapes, which is the bug this test exists to catch.
    """
    nodes, edges = _platform()
    await graph.replace_origin("packages", nodes, edges)
    await graph.replace_origin(
        "cluster",
        [
            Node(
                id="Service:adhar-system/argocd",
                kind="Service",
                name="argocd",
                namespace="adhar-system",
                origin="cluster",
            )
        ],
        [],
    )

    await graph.replace_origin("packages", nodes[:2], edges[:1])
    stats = await graph.stats()
    kinds = {row["kind"] for row in stats["byKind"]}
    assert "Service" in kinds, "the cluster origin was wiped by a package refresh"
    assert await graph.resolve(["argocd"]), "the surviving node no longer resolves"


@pytest.mark.slow
@needs_db
async def test_re_ingesting_the_same_nodes_does_not_duplicate_them(graph):
    nodes, edges = _platform()
    await graph.replace_origin("packages", nodes, edges)
    first = await graph.stats()
    await graph.replace_origin("packages", nodes, edges)
    assert (await graph.stats())["nodes"] == first["nodes"]
