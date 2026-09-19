"""Deriving the graph from what the platform already knows about itself.

Each extractor turns one source of truth into nodes and edges. They are separate
because they fail independently and refresh at different rates, and because each
one's failure should cost only its own slice of the graph.

Everything here is **derived, never authored**. Nothing writes a relationship a
human typed; every edge comes from a package contract, a Kubernetes object or an
ArgoCD Application, and carries the moment it was observed. That is what keeps a
blast-radius answer trustworthy: the graph cannot claim a dependency the platform
does not currently have.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .graph import Edge, Node, node_id

log = logging.getLogger("adhar_ai.rag.graph")


class PackageGraphSource:
    """Dependencies declared in every `adhar-package.yaml`.

    The most reliable edges in the platform: a package contract is
    machine-readable, reviewed, and states exactly what the package needs. This
    is what answers "which packages need Keycloak" without guessing.
    """

    origin = "packages"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    async def extract(self) -> tuple[list[Node], list[Edge]]:
        root = self.path
        if not root.exists():
            return [], []
        import yaml

        nodes: dict[str, Node] = {}
        edges: list[Edge] = []
        for file in sorted(root.rglob("adhar-package.yaml")):
            try:
                data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
            except Exception:  # noqa: BLE001 - one bad contract must not lose the rest
                continue
            name = str(data.get("name") or file.parent.name)
            category = str(data.get("category") or "unknown")
            pid = node_id("Package", name)
            nodes[pid] = Node(
                id=pid,
                kind="Package",
                name=name,
                origin=self.origin,
                attributes={
                    "category": category,
                    "stability": data.get("stability"),
                    "plane": data.get("planeAffinity"),
                    "localSafe": (data.get("resources") or {}).get("localSafe"),
                },
            )
            for dep in data.get("dependencies") or []:
                if not isinstance(dep, dict) or not dep.get("name"):
                    continue
                target = node_id("Package", str(dep["name"]))
                nodes.setdefault(
                    target,
                    Node(id=target, kind="Package", name=str(dep["name"]), origin=self.origin),
                )
                edges.append(
                    Edge(
                        src=pid,
                        dst=target,
                        relation="depends_on",
                        origin=self.origin,
                        # Optional dependencies are real edges but weaker ones:
                        # a blast-radius answer should be able to say "degrades"
                        # rather than "breaks".
                        attributes={"optional": bool(dep.get("optional"))},
                    )
                )
        return list(nodes.values()), edges


class ArgoGraphSource:
    """Applications, and the resources they own.

    `Application owns Resource` is the single most useful relationship in the
    platform: it is how a workload is traced back to the package that deployed
    it, and it is already in the data the gitops tools read.
    """

    origin = "argocd"

    def __init__(self, toolbox: Any) -> None:
        self.toolbox = toolbox

    async def _call(self, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
        try:
            out = await self.toolbox.call(tool, args)
        except Exception as exc:  # noqa: BLE001
            log.debug("graph extraction: %s failed: %s", tool, exc)
            return None
        return None if not isinstance(out, dict) or "error" in out else out

    async def extract(self) -> tuple[list[Node], list[Edge]]:
        listing = await self._call("sync_status", {"only_unhealthy": False})
        if not listing:
            return [], []

        nodes: dict[str, Node] = {}
        edges: list[Edge] = []
        for app in (listing.get("applications") or [])[:400]:
            name = str(app.get("name") or "")
            if not name:
                continue
            namespace = str(app.get("namespace") or "")
            aid = node_id("Application", name, namespace)
            nodes[aid] = Node(
                id=aid,
                kind="Application",
                name=name,
                namespace=namespace,
                origin=self.origin,
                attributes={
                    "sync": app.get("sync_status"),
                    "health": app.get("health_status"),
                },
            )
            # An Application is named after its package by platform convention,
            # so this edge joins live state to the catalogue. Asserted only when
            # a package of that name exists, which the graph resolves at query
            # time rather than here.
            pid = node_id("Package", name)
            edges.append(Edge(src=aid, dst=pid, relation="deploys", origin=self.origin))

        return list(nodes.values()), edges


class WorkloadGraphSource:
    """Workloads, their namespaces, and the teams that own them.

    Ownership comes from `app.kubernetes.io/part-of` and `adhar.io/owner`, which
    is how the platform already labels things. Without this edge, "who owns the
    service burning the most money" needs a human to join two answers.
    """

    origin = "workloads"

    def __init__(self, toolbox: Any, namespace: str = "adhar-system") -> None:
        self.toolbox = toolbox
        self.namespace = namespace

    async def extract(self) -> tuple[list[Node], list[Edge]]:
        try:
            out = await self.toolbox.call("list_pods", {"namespace": self.namespace})
        except Exception as exc:  # noqa: BLE001
            log.debug("workload graph extraction failed: %s", exc)
            return [], []
        if not isinstance(out, dict) or "error" in out:
            return [], []

        nodes: dict[str, Node] = {}
        edges: list[Edge] = []
        nsid = node_id("Namespace", self.namespace)
        nodes[nsid] = Node(
            id=nsid, kind="Namespace", name=self.namespace, origin=self.origin
        )

        for pod in (out.get("pods") or out.get("items") or [])[:500]:
            if not isinstance(pod, dict):
                continue
            name = str(pod.get("name") or "")
            if not name:
                continue
            namespace = str(pod.get("namespace") or self.namespace)
            labels = pod.get("labels") or {}
            pid = node_id("Pod", name, namespace)
            nodes[pid] = Node(
                id=pid,
                kind="Pod",
                name=name,
                namespace=namespace,
                origin=self.origin,
                attributes={"phase": pod.get("phase"), "ready": pod.get("ready")},
            )
            edges.append(Edge(src=pid, dst=nsid, relation="runs_in", origin=self.origin))

            part_of = labels.get("app.kubernetes.io/part-of")
            if part_of:
                cid = node_id("Component", str(part_of))
                nodes.setdefault(
                    cid, Node(id=cid, kind="Component", name=str(part_of), origin=self.origin)
                )
                edges.append(
                    Edge(src=cid, dst=pid, relation="owns", origin=self.origin)
                )

            owner = labels.get("adhar.io/owner") or labels.get("adhar.io/team")
            if owner:
                tid = node_id("Team", str(owner))
                nodes.setdefault(
                    tid, Node(id=tid, kind="Team", name=str(owner), origin=self.origin)
                )
                edges.append(Edge(src=tid, dst=pid, relation="owns", origin=self.origin))

        return list(nodes.values()), edges


class ToolGraphSource:
    """The agent's own tools, and the domains that serve them.

    Small, and it answers a question people genuinely ask: "which of your tools
    could tell me about X". It also makes the capability catalogue a traversal
    rather than a second hand-written list.
    """

    origin = "tools"

    async def extract(self) -> tuple[list[Node], list[Edge]]:
        from ..config import DOMAINS, WRITE_DOMAINS, MCPConfig
        from ..mcp.server import build_server, tool_access

        nodes: list[Node] = []
        edges: list[Edge] = []
        for domain in DOMAINS:
            try:
                server = build_server(MCPConfig(domain=domain))
                listing = await server.list_tools()
            except Exception as exc:  # noqa: BLE001
                log.debug("tool graph extraction failed for %s: %s", domain, exc)
                continue
            did = node_id("Domain", domain)
            nodes.append(
                Node(
                    id=did,
                    kind="Domain",
                    name=domain,
                    origin=self.origin,
                    attributes={"writes": domain in WRITE_DOMAINS},
                )
            )
            for tool in listing:
                tid = node_id("Tool", tool.name)
                nodes.append(
                    Node(
                        id=tid,
                        kind="Tool",
                        name=tool.name,
                        origin=self.origin,
                        attributes={
                            "access": tool_access(tool),
                            "description": (getattr(tool, "description", "") or "")[:200],
                        },
                    )
                )
                edges.append(Edge(src=did, dst=tid, relation="serves", origin=self.origin))
        return nodes, edges


#: Terms never worth resolving against the graph. Without this, a question
#: containing "the cluster" resolves to whatever node happens to be called
#: "cluster" and anchors the whole answer on it.
STOP_TERMS = frozenset(
    """the a an is are was were do does did can could should would will my our your
    what which who how why when where cluster platform service app application pod
    namespace team package tool agent this that these those and or but for with""".split()
)


def candidate_terms(prompt: str, limit: int = 12) -> list[str]:
    """Words from a question that might name a platform entity.

    Biased toward identifiers: quoted strings, backticked names, hyphenated
    words and dotted paths. A platform question that is *about* something almost
    always spells that thing exactly, because the asker copied it from a console.
    """
    import re

    terms: list[str] = []
    # Quoted and backticked spans first — an explicit naming.
    for match in re.findall(r"[`\"']([A-Za-z0-9][\w.\-/]{1,60})[`\"']", prompt):
        terms.append(match)
    # Then identifier-shaped bare words.
    for match in re.findall(r"\b([a-z0-9]+(?:[-.][a-z0-9]+)+)\b", prompt.lower()):
        terms.append(match)
    # Then ordinary words, which resolve only on an exact node-name match.
    for match in re.findall(r"\b([a-z][a-z0-9]{2,})\b", prompt.lower()):
        if match not in STOP_TERMS:
            terms.append(match)

    seen: list[str] = []
    for term in terms:
        if term not in seen:
            seen.append(term)
    return seen[:limit]
