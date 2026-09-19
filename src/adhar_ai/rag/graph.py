"""The platform's own dependency structure, captured and traversable.

Vector search retrieves passages that *sound like* the question. That is the
wrong operation when the answer is a traversal, and most real platform questions
are traversals:

    "what breaks if I upgrade Cilium?"      -> returns the Cilium ADR, not the
                                               workloads that depend on it
    "who owns the service burning money?"   -> returns a cost page and an
                                               ownership page, never the join
    "which packages need Keycloak?"         -> returns pages mentioning Keycloak,
                                               not the dependency edges

The platform already emits this graph on every reconcile and throws it away.
ArgoCD Applications own resources. Crossplane composites own claims. Package
contracts declare dependencies. Ownership labels bind workloads to teams. Kyverno
policies bind to the resources they matched.

## Why Postgres and not a graph database

Because the alternative is a second datastore on the critical path of an optional
feature, and the queries this actually needs — neighbourhood, reachability,
bounded traversal — are a recursive CTE. Neo4j earns its place at depths and
volumes an IDP's own topology does not reach: a large platform is thousands of
nodes, not billions. It sits in the same database as pgvector, so retrieval can
join the two without a network hop.

## Why it is derived, never maintained

A hand-maintained graph rots faster than a document, and a confidently wrong edge
is worse than a missing one — it produces a blast-radius answer that is precise
and false. Every edge is re-derived from live state on each refresh and carries
the time it was observed, so a stale one can be recognised rather than trusted.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("adhar_ai.rag.graph")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS {nodes} (
  id          TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,
  name        TEXT NOT NULL,
  namespace   TEXT NOT NULL DEFAULT '',
  origin      TEXT NOT NULL DEFAULT '',
  attributes  JSONB NOT NULL DEFAULT '{{}}'::jsonb,
  observed_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS {nodes}_kind ON {nodes} (kind);
CREATE INDEX IF NOT EXISTS {nodes}_name ON {nodes} (lower(name));
CREATE INDEX IF NOT EXISTS {nodes}_origin ON {nodes} (origin);

CREATE TABLE IF NOT EXISTS {edges} (
  src         TEXT NOT NULL,
  dst         TEXT NOT NULL,
  relation    TEXT NOT NULL,
  origin      TEXT NOT NULL DEFAULT '',
  attributes  JSONB NOT NULL DEFAULT '{{}}'::jsonb,
  observed_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (src, dst, relation)
);
CREATE INDEX IF NOT EXISTS {edges}_src ON {edges} (src);
CREATE INDEX IF NOT EXISTS {edges}_dst ON {edges} (dst);
CREATE INDEX IF NOT EXISTS {edges}_origin ON {edges} (origin);
"""

UPSERT_NODE_SQL = """
INSERT INTO {nodes} (id, kind, name, namespace, origin, attributes, observed_at)
VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
ON CONFLICT (id) DO UPDATE SET
  kind = EXCLUDED.kind, name = EXCLUDED.name, namespace = EXCLUDED.namespace,
  origin = EXCLUDED.origin, attributes = EXCLUDED.attributes,
  observed_at = EXCLUDED.observed_at
"""

UPSERT_EDGE_SQL = """
INSERT INTO {edges} (src, dst, relation, origin, attributes, observed_at)
VALUES (%s, %s, %s, %s, %s::jsonb, %s)
ON CONFLICT (src, dst, relation) DO UPDATE SET
  origin = EXCLUDED.origin, attributes = EXCLUDED.attributes,
  observed_at = EXCLUDED.observed_at
"""

#: Anything an origin did NOT re-observe this pass is gone from the platform.
PRUNE_NODES_SQL = "DELETE FROM {nodes} WHERE origin = %s AND observed_at < %s"
PRUNE_EDGES_SQL = "DELETE FROM {edges} WHERE origin = %s AND observed_at < %s"

RESOLVE_SQL = """
SELECT id, kind, name, namespace, attributes FROM {nodes}
WHERE lower(name) = ANY(%s) OR id = ANY(%s)
LIMIT %s
"""

#: Bounded, cycle-safe traversal. `depth <= %s` is the bound; the `NOT
#: seen @> ARRAY[...]` guard is what stops a cycle — a platform graph has plenty
#: (an Application owning a resource that references the Application).
#: Bounded, cycle-safe traversal in BOTH directions.
#:
#: One recursive reference, not two: Postgres permits a recursive CTE to
#: reference itself exactly once, so the obvious shape — a UNION ALL of an
#: outbound term and an inbound term, each joining `walk` — is rejected with
#: "recursive reference to query walk must not appear within its non-recursive
#: term". The direction union goes inside a LATERAL instead, which reads the
#: same and is legal.
#:
#: `NOT path @> ARRAY[...]` is the cycle guard. A platform graph has plenty of
#: cycles (an Application owning a resource that references the Application),
#: and without it the traversal does not terminate.
NEIGHBOURHOOD_SQL = """
WITH RECURSIVE walk(id, depth, path, relation, direction) AS (
    SELECT %s::text, 0, ARRAY[%s::text], ''::text, ''::text
  UNION ALL
    SELECT step.id, w.depth + 1, w.path || step.id, step.relation, step.direction
    FROM walk w
    CROSS JOIN LATERAL (
        SELECT e.dst AS id, e.relation AS relation, 'out'::text AS direction
        FROM {edges} e WHERE e.src = w.id
      UNION ALL
        SELECT e.src AS id, e.relation AS relation, 'in'::text AS direction
        FROM {edges} e WHERE e.dst = w.id
    ) step
    WHERE w.depth < %s AND NOT w.path @> ARRAY[step.id]
)
SELECT DISTINCT ON (w.id)
       w.id, w.depth, w.relation, w.direction,
       n.kind, n.name, n.namespace, n.attributes
FROM walk w JOIN {nodes} n ON n.id = w.id
WHERE w.depth > 0
ORDER BY w.id, w.depth ASC
LIMIT %s
"""

DEPENDENTS_SQL = """
WITH RECURSIVE up(id, depth, path) AS (
    SELECT %s::text, 0, ARRAY[%s::text]
  UNION ALL
    SELECT e.src, u.depth + 1, u.path || e.src
    FROM up u JOIN {edges} e ON e.dst = u.id
    WHERE u.depth < %s AND NOT u.path @> ARRAY[e.src]
      AND e.relation = ANY(%s)
)
SELECT DISTINCT ON (u.id) u.id, u.depth, n.kind, n.name, n.namespace
FROM up u JOIN {nodes} n ON n.id = u.id
WHERE u.depth > 0
ORDER BY u.id, u.depth ASC
LIMIT %s
"""

STATS_SQL = """
SELECT origin, kind, count(*) FROM {nodes} GROUP BY origin, kind ORDER BY 3 DESC
"""
EDGE_STATS_SQL = "SELECT relation, count(*) FROM {edges} GROUP BY relation ORDER BY 2 DESC"

#: Relations that mean "the source cannot work without the destination", which
#: is what a blast-radius question is really asking about.
DEPENDENCY_RELATIONS = ("depends_on", "owns", "requires", "routes_to", "binds")


@dataclass(slots=True)
class Node:
    """One thing in the platform."""

    id: str
    kind: str
    name: str
    namespace: str = ""
    origin: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)

    def label(self) -> str:
        where = f" in `{self.namespace}`" if self.namespace else ""
        return f"{self.kind} `{self.name}`{where}"


@dataclass(slots=True)
class Edge:
    src: str
    dst: str
    relation: str
    origin: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Neighbour:
    node: Node
    depth: int
    relation: str
    direction: str


def node_id(kind: str, name: str, namespace: str = "") -> str:
    """A stable identity, so a re-derived node updates rather than duplicates."""
    return f"{kind}:{namespace}/{name}" if namespace else f"{kind}:{name}"


class KnowledgeGraph:
    """Entity and relationship storage over the same database as pgvector."""

    def __init__(self, dsn: str, prefix: str = "kb_graph") -> None:
        self.dsn = dsn
        if not prefix.replace("_", "").isalnum():
            raise ValueError(f"invalid graph table prefix {prefix!r}")
        self.nodes_table = f"{prefix}_node"
        self.edges_table = f"{prefix}_edge"
        self.ready = False
        self.status = "disabled (no database)" if not dsn else "pending"

    def _sql(self, template: str) -> str:
        return template.format(nodes=self.nodes_table, edges=self.edges_table)

    async def _connect(self) -> Any:
        import psycopg

        return await psycopg.AsyncConnection.connect(self.dsn)

    async def prepare(self) -> bool:
        if not self.dsn:
            return False
        try:
            async with await self._connect() as conn, conn.cursor() as cur:
                await cur.execute(self._sql(SCHEMA_SQL))
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            self.ready = False
            self.status = f"unavailable: {type(exc).__name__}: {exc}"
            log.warning("knowledge graph unavailable: %s", exc)
            return False
        self.ready = True
        self.status = f"ready ({self.nodes_table})"
        return True

    # ------------------------------------------------------------- ingest --

    async def replace_origin(
        self, origin: str, nodes: list[Node], edges: list[Edge]
    ) -> dict[str, Any]:
        """Write one origin's view of the graph, and drop what it no longer sees.

        Scoped per origin so the ArgoCD derivation cannot delete the package
        catalogue's edges, and stamped with one timestamp for the whole pass so
        the prune is exact rather than racing its own writes.
        """
        if not self.ready:
            return {"nodes": 0, "edges": 0, "pruned": 0}
        observed = time.time()
        try:
            async with await self._connect() as conn, conn.cursor() as cur:
                for node in nodes:
                    await cur.execute(
                        self._sql(UPSERT_NODE_SQL),
                        (
                            node.id, node.kind, node.name, node.namespace,
                            origin, json.dumps(node.attributes), observed,
                        ),
                    )
                for edge in edges:
                    await cur.execute(
                        self._sql(UPSERT_EDGE_SQL),
                        (
                            edge.src, edge.dst, edge.relation,
                            origin, json.dumps(edge.attributes), observed,
                        ),
                    )
                await cur.execute(self._sql(PRUNE_EDGES_SQL), (origin, observed))
                pruned = cur.rowcount or 0
                await cur.execute(self._sql(PRUNE_NODES_SQL), (origin, observed))
                pruned += cur.rowcount or 0
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("graph write for origin %s failed: %s", origin, exc)
            return {"nodes": 0, "edges": 0, "pruned": 0, "error": str(exc)}
        return {"nodes": len(nodes), "edges": len(edges), "pruned": pruned}

    # ------------------------------------------------------------ queries --

    async def resolve(self, terms: list[str], limit: int = 8) -> list[Node]:
        """Find the entities a question is about.

        Exact name or id only. Fuzzy resolution sounds helpful and is not: a
        blast-radius answer computed from the wrong node is confidently wrong,
        which is the one failure mode this whole module exists to avoid.
        """
        if not self.ready or not terms:
            return []
        lowered = [t.lower() for t in terms]
        try:
            async with await self._connect() as conn, conn.cursor() as cur:
                await cur.execute(self._sql(RESOLVE_SQL), (lowered, terms, limit))
                rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("graph resolve failed: %s", exc)
            return []
        return [
            Node(id=r[0], kind=r[1], name=r[2], namespace=r[3], attributes=_json(r[4]))
            for r in rows
        ]

    async def neighbourhood(
        self, node: str, depth: int = 2, limit: int = 60
    ) -> list[Neighbour]:
        """Everything within `depth` hops, in either direction."""
        if not self.ready:
            return []
        try:
            async with await self._connect() as conn, conn.cursor() as cur:
                await cur.execute(
                    self._sql(NEIGHBOURHOOD_SQL), (node, node, depth, limit)
                )
                rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("graph traversal failed: %s", exc)
            return []
        return [
            Neighbour(
                node=Node(id=r[0], kind=r[4], name=r[5], namespace=r[6], attributes=_json(r[7])),
                depth=r[1],
                relation=r[2],
                direction=r[3],
            )
            for r in rows
        ]

    async def dependents(
        self, node: str, depth: int = 3, limit: int = 100
    ) -> list[Neighbour]:
        """What would be affected if this broke — the blast radius.

        Walks edges BACKWARDS along dependency relations only. Following every
        relation would return the whole connected component, which for a
        platform is very nearly everything and therefore useless.
        """
        if not self.ready:
            return []
        try:
            async with await self._connect() as conn, conn.cursor() as cur:
                await cur.execute(
                    self._sql(DEPENDENTS_SQL),
                    (node, node, depth, list(DEPENDENCY_RELATIONS), limit),
                )
                rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("dependents query failed: %s", exc)
            return []
        return [
            Neighbour(
                node=Node(id=r[0], kind=r[2], name=r[3], namespace=r[4]),
                depth=r[1],
                relation="depends on",
                direction="in",
            )
            for r in rows
        ]

    async def stats(self) -> dict[str, Any]:
        empty: dict[str, Any] = {
            "status": self.status, "nodes": 0, "edges": 0,
            "byKind": [], "byRelation": [],
        }
        if not self.ready:
            return empty
        try:
            async with await self._connect() as conn, conn.cursor() as cur:
                await cur.execute(self._sql(STATS_SQL))
                node_rows = await cur.fetchall()
                await cur.execute(self._sql(EDGE_STATS_SQL))
                edge_rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            return {**empty, "status": f"unavailable: {exc}"}
        return {
            "status": self.status,
            "nodes": sum(r[2] for r in node_rows),
            "edges": sum(r[1] for r in edge_rows),
            "byKind": [{"origin": r[0], "kind": r[1], "count": r[2]} for r in node_rows],
            "byRelation": [{"relation": r[0], "count": r[1]} for r in edge_rows],
        }


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value) if value else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def as_grounding(anchor: Node, neighbours: list[Neighbour]) -> str:
    """Render a subgraph as a grounding block the model can read.

    Prose rather than adjacency lists, and grouped by relation: a model reading
    "`console` depends on: Service keycloak, Secret argocd-redis" answers the
    question, while one reading a list of edge tuples describes the list.
    """
    if not neighbours:
        return f"### graph: {anchor.label()} (no recorded relationships)\n\nnothing connected"

    out: dict[str, list[str]] = {}
    for neighbour in neighbours:
        arrow = "depends on" if neighbour.direction == "out" else "is depended on by"
        key = f"{arrow} ({neighbour.relation})" if neighbour.relation else arrow
        out.setdefault(key, []).append(neighbour.node.label())

    lines = [f"### graph: {anchor.label()} (live platform topology)", ""]
    for relation, items in sorted(out.items()):
        shown = items[:15]
        more = f" …and {len(items) - 15} more" if len(items) > 15 else ""
        lines.append(f"- **{relation}**: {', '.join(shown)}{more}")
    return "\n".join(lines)
