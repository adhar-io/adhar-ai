"""Where the platform's knowledge comes from.

Each source turns one part of the platform into `Document`s. They are separate
because they change at completely different rates and fail independently: the
docs tree changes on a commit, the live cluster inventory changes by the minute,
and a meeting note arrives when someone writes one. A refresh runs them all and
takes whatever succeeds — a source whose backend is down costs its own knowledge
and nothing else.

The set is chosen so that an agent can answer the questions people actually ask
an internal developer platform:

| Source | Answers |
|---|---|
| `docs` | "why is it built this way", "how do I do X" |
| `tools` | "what can you actually do for me" |
| `packages` | "what is installed, and how is it configured" |
| `cluster` | "what is running right now, and is it healthy" |
| `findings` | "has the platform noticed this before" |
| `notes` | "what did we decide, and what did we learn" |

`cluster` is the one that makes the knowledge base *dynamic* rather than a
snapshot of a docs folder: it is re-derived from live state on every refresh, so
the agent's background knowledge moves with the platform.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Protocol

from .documents import Document

log = logging.getLogger("adhar_ai.rag")


class Source(Protocol):
    """Produces the documents for one origin."""

    origin: str

    async def documents(self) -> list[Document]: ...


def classify_doc(path: Path) -> str:
    parts = {p.lower() for p in path.parts}
    name = path.name.lower()
    if "adr" in parts:
        return "adr"
    if "runbook" in parts or "runbooks" in parts or "runbook" in name:
        return "runbook"
    if {"incident", "incidents", "postmortem", "postmortems"} & parts:
        return "incident"
    if "incident" in name or "postmortem" in name:
        return "incident"
    if "design" in parts:
        return "adr"
    return "doc"


# --------------------------------------------------------------------------- #
# Documentation
# --------------------------------------------------------------------------- #


class DocsSource:
    """The platform's own Markdown: docs, ADRs, designs, runbooks, postmortems."""

    origin = "docs"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    async def documents(self) -> list[Document]:
        root = self.path
        if not root.exists():
            log.info("docs path %s does not exist; skipping", root)
            return []
        files = sorted(root.rglob("*.md")) if root.is_dir() else [root]
        docs: list[Document] = []
        for file in files:
            try:
                text = file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if not text.strip():
                continue
            rel = str(file.relative_to(root)) if root.is_dir() else file.name
            docs.append(
                Document(
                    doc_id=f"docs:{rel}",
                    source=rel,
                    text=text,
                    kind=classify_doc(file),
                    origin=self.origin,
                    metadata={"path": rel},
                )
            )
        return docs


# --------------------------------------------------------------------------- #
# The agent's own capabilities
# --------------------------------------------------------------------------- #


class ToolsSource:
    """The MCP tool inventory, as prose the agent can retrieve.

    "What can you do?" is among the most common questions put to a platform
    assistant, and the tool schemas already in the model's context answer it
    only for the tools offered in THAT request — a read-only session cannot
    describe the write tools it was not given. Indexing the inventory means the
    agent can explain its own capabilities and their limits accurately whatever
    stage it is running at.
    """

    origin = "tools"

    async def documents(self) -> list[Document]:
        from ..config import DOMAINS, WRITE_DOMAINS, MCPConfig
        from ..mcp.server import build_server, tool_access

        docs: list[Document] = []
        for domain in DOMAINS:
            try:
                server = build_server(MCPConfig(domain=domain))
                listing = await server.list_tools()
            except Exception as exc:  # noqa: BLE001
                log.warning("could not list tools for domain %s: %s", domain, exc)
                continue
            lines = [
                f"# Adhar AI `{domain}` tools",
                "",
                f"The `{domain}` MCP server exposes {len(listing)} tools. "
                + (
                    "It carries a write tool, which opens a Gitea pull request and "
                    "nothing else."
                    if domain in WRITE_DOMAINS
                    else "It is read-only: every tool here only queries state."
                ),
                "",
            ]
            for tool in listing:
                access = tool_access(tool)
                description = (getattr(tool, "description", "") or "").strip()
                lines.append(f"## `{tool.name}` ({access})")
                lines.append("")
                lines.append(description or "(no description)")
                schema = getattr(tool, "input_schema", None) or {}
                props = (schema or {}).get("properties") or {}
                if props:
                    lines.append("")
                    lines.append("Arguments: " + ", ".join(f"`{p}`" for p in props))
                lines.append("")
            docs.append(
                Document(
                    doc_id=f"tools:{domain}",
                    source=f"adhar-ai tools: {domain}",
                    text="\n".join(lines),
                    kind="tool",
                    origin=self.origin,
                    metadata={"domain": domain, "write": domain in WRITE_DOMAINS},
                )
            )
        return docs


# --------------------------------------------------------------------------- #
# The package catalogue
# --------------------------------------------------------------------------- #


class PackagesSource:
    """Every `adhar-package.yaml` contract: what is installable and what it needs.

    This is the platform's own machine-readable description of itself, so it
    answers "is X available", "what does it depend on", "is it safe to run
    locally" and "who maintains it" without the agent guessing from a docs page
    that may predate the package.
    """

    origin = "packages"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    async def documents(self) -> list[Document]:
        root = self.path
        if not root.exists():
            return []
        import yaml

        docs: list[Document] = []
        for file in sorted(root.rglob("adhar-package.yaml")):
            try:
                data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
            except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
                log.debug("skipping unreadable package contract %s: %s", file, exc)
                continue
            name = str(data.get("name") or file.parent.name)
            category = str(data.get("category") or "unknown")
            deps = data.get("dependencies") or []
            resources = data.get("resources") or {}
            lines = [
                f"# Package `{category}/{name}`",
                "",
                str(data.get("description") or "").strip(),
                "",
                "## Contract",
                "",
                f"- Version: {data.get('version')} (app {data.get('appVersion')})",
                f"- Stability: {data.get('stability')}",
                f"- Plane: {data.get('planeAffinity')}",
                f"- Licence: {data.get('license')}",
                f"- Safe to run locally: {resources.get('localSafe')}",
                f"- Footprint: {resources.get('footprint')} "
                f"(cpu {resources.get('cpu')}, memory {resources.get('memory')})",
            ]
            if deps:
                lines += ["", "## Dependencies", ""]
                for dep in deps:
                    optional = " (optional)" if dep.get("optional") else ""
                    lines.append(f"- `{dep.get('category')}/{dep.get('name')}`{optional}")
            if notes := (resources.get("notes") or "").strip():
                lines += ["", "## Notes", "", notes]
            docs.append(
                Document(
                    doc_id=f"package:{category}/{name}",
                    source=f"package {category}/{name}",
                    text="\n".join(lines),
                    kind="package",
                    origin=self.origin,
                    metadata={"name": name, "category": category},
                )
            )
        return docs


# --------------------------------------------------------------------------- #
# Live platform state
# --------------------------------------------------------------------------- #


class ClusterSource:
    """Live inventory, re-derived on every refresh.

    This is what makes the knowledge base current rather than a snapshot. It
    summarises what is deployed and how it is doing, so a question like "is
    Keycloak installed" is answered from the cluster rather than from a document
    that describes the intended catalogue.

    Deliberately a SUMMARY, not a dump: object specs are large, change
    constantly, and would flood retrieval with noise. The detail lives behind
    the read tools, which fetch it fresh when a question actually needs it.
    """

    origin = "cluster"

    def __init__(self, toolbox: Any, namespace: str = "adhar-system") -> None:
        self.toolbox = toolbox
        self.namespace = namespace

    async def _call(self, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
        try:
            out = await self.toolbox.call(tool, args)
        except Exception as exc:  # noqa: BLE001
            log.debug("cluster source: %s failed: %s", tool, exc)
            return None
        return None if not isinstance(out, dict) or "error" in out else out

    async def documents(self) -> list[Document]:
        docs: list[Document] = []

        if apps := await self._call("sync_status", {"only_unhealthy": False}):
            rows = apps.get("applications") or []
            lines = [
                "# ArgoCD application inventory (live)",
                "",
                f"{apps.get('count', len(rows))} applications, "
                f"{apps.get('out_of_sync', 0)} OutOfSync, "
                f"{apps.get('degraded', 0)} degraded.",
                "",
            ]
            for app in rows[:200]:
                lines.append(
                    f"- `{app.get('name')}` in `{app.get('namespace')}`: "
                    f"sync={app.get('sync_status')} health={app.get('health_status')}"
                )
            docs.append(
                Document(
                    doc_id="cluster:argocd-applications",
                    source="live: ArgoCD applications",
                    text="\n".join(lines),
                    kind="resource",
                    origin=self.origin,
                    metadata={"count": apps.get("count")},
                )
            )

        if health := await self._call("resource_health", {"namespace": self.namespace}):
            degraded = health.get("workloads_degraded") or []
            lines = [
                f"# Workload health in `{self.namespace}` (live)",
                "",
                f"{health.get('workloads_total', 0)} workloads, {len(degraded)} degraded.",
                "",
            ]
            for workload in degraded[:100]:
                lines.append(
                    f"- `{workload.get('name')}`: "
                    f"{workload.get('replicas_ready', 0)}"
                    f"/{workload.get('replicas_desired', 0)} ready"
                )
            docs.append(
                Document(
                    doc_id=f"cluster:health:{self.namespace}",
                    source=f"live: workload health in {self.namespace}",
                    text="\n".join(lines),
                    kind="resource",
                    origin=self.origin,
                    metadata={"namespace": self.namespace},
                )
            )

        if packages := await self._call("search_packages", {}):
            rows = packages.get("packages") or packages.get("results") or []
            if rows:
                lines = ["# Installed package catalogue (live)", ""]
                for pkg in rows[:300]:
                    if isinstance(pkg, dict):
                        lines.append(
                            f"- `{pkg.get('name')}` ({pkg.get('category', '?')}): "
                            f"{pkg.get('status', pkg.get('health', 'unknown'))}"
                        )
                docs.append(
                    Document(
                        doc_id="cluster:package-catalogue",
                        source="live: package catalogue",
                        text="\n".join(lines),
                        kind="package",
                        origin=self.origin,
                    )
                )
        return docs


# --------------------------------------------------------------------------- #
# What the platform has learned about itself
# --------------------------------------------------------------------------- #


class FindingsSource:
    """Operator findings: what the platform noticed, and what it proposed.

    A finding is the platform's own observation about itself, so feeding it back
    is the tightest learning loop available — the next investigation of a similar
    alert retrieves what the last one concluded, including the pull request that
    fixed it.
    """

    origin = "findings"

    def __init__(self, findings: Any) -> None:
        self._findings = findings

    async def documents(self) -> list[Document]:
        try:
            rows = await self._findings.recent(500) if hasattr(self._findings, "recent") else []
        except Exception as exc:  # noqa: BLE001
            log.debug("findings source unavailable: %s", exc)
            return []
        docs = []
        for finding in rows:
            subject = json.dumps(finding.subject) if finding.subject else "{}"
            pr = finding.pull_request or {}
            lines = [
                f"# Finding: {finding.title}",
                "",
                f"- Operator: `{finding.operator}`",
                f"- Severity: {finding.severity}",
                f"- Autonomy at the time: {finding.autonomy}",
                f"- Subject: `{subject}`",
            ]
            if pr.get("url"):
                lines.append(f"- Proposed fix: {pr.get('url')}")
            lines += ["", "## What it found", "", finding.summary or "(no summary)"]
            if finding.recommendation and finding.recommendation != finding.summary:
                lines += ["", "## Recommendation", "", finding.recommendation]
            docs.append(
                Document(
                    doc_id=f"finding:{finding.id}",
                    source=f"finding {finding.id} ({finding.operator})",
                    text="\n".join(lines),
                    kind="finding",
                    origin=self.origin,
                    metadata={
                        "operator": finding.operator,
                        "severity": finding.severity,
                        "pull_request": pr.get("url", ""),
                    },
                )
            )
        return docs


class NotesSource:
    """Human-contributed knowledge: meeting notes, troubleshooting, learnings.

    Held in its own table rather than derived from anything, because it is the
    one kind of platform knowledge with no other home. It is also the kind most
    likely to be the ONLY record of why something was done — so it is kept, with
    its author and date, and never pruned by a refresh of some other origin.
    """

    origin = "notes"

    CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS {table} (
      id         TEXT PRIMARY KEY,
      title      TEXT NOT NULL,
      body       TEXT NOT NULL,
      kind       TEXT NOT NULL DEFAULT 'note',
      author     TEXT NOT NULL DEFAULT 'unknown',
      tags       TEXT[] NOT NULL DEFAULT '{{}}',
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """
    INSERT_SQL = """
    INSERT INTO {table} (id, title, body, kind, author, tags)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (id) DO UPDATE SET
      title = EXCLUDED.title, body = EXCLUDED.body,
      kind = EXCLUDED.kind, tags = EXCLUDED.tags
    """
    SELECT_SQL = (
        "SELECT id, title, body, kind, author, tags, created_at FROM {table} "
        "ORDER BY created_at DESC LIMIT %s"
    )

    def __init__(self, dsn: str, table: str = "kb_note") -> None:
        self.dsn = dsn
        if not table.replace("_", "").isalnum():
            raise ValueError(f"invalid notes table name {table!r}")
        self.table = table

    async def _connect(self) -> Any:
        import psycopg

        return await psycopg.AsyncConnection.connect(self.dsn)

    async def prepare(self) -> bool:
        if not self.dsn:
            return False
        try:
            async with await self._connect() as conn, conn.cursor() as cur:
                await cur.execute(self.CREATE_SQL.format(table=self.table))
                await conn.commit()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("notes table unavailable: %s", exc)
            return False

    async def add(
        self,
        note_id: str,
        title: str,
        body: str,
        kind: str = "note",
        author: str = "unknown",
        tags: list[str] | None = None,
    ) -> bool:
        if not self.dsn:
            return False
        try:
            async with await self._connect() as conn, conn.cursor() as cur:
                await cur.execute(
                    self.INSERT_SQL.format(table=self.table),
                    (note_id, title, body, kind, author, tags or []),
                )
                await conn.commit()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("could not store note %s: %s", note_id, exc)
            return False

    async def documents(self) -> list[Document]:
        if not self.dsn:
            return []
        try:
            async with await self._connect() as conn, conn.cursor() as cur:
                await cur.execute(self.SELECT_SQL.format(table=self.table), (1000,))
                rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            log.debug("notes source unavailable: %s", exc)
            return []
        docs = []
        for note_id, title, body, kind, author, tags, created in rows:
            header = [
                f"# {title}",
                "",
                f"- Recorded by: {author}",
                f"- When: {created.isoformat() if created else 'unknown'}",
            ]
            if tags:
                header.append(f"- Tags: {', '.join(tags)}")
            header += ["", body]
            docs.append(
                Document(
                    doc_id=f"note:{note_id}",
                    source=f"{kind}: {title}",
                    text="\n".join(header),
                    kind=kind if kind in ("note", "incident", "runbook") else "note",
                    origin=self.origin,
                    metadata={"author": author, "tags": list(tags or [])},
                )
            )
        return docs
