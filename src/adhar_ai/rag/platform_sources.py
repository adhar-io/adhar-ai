"""Knowledge sources that read the platform's own configuration.

Three sources, all reading through a `Tree` so they work identically from a
checkout on a workstation and from Gitea inside the cluster:

* **ManifestsSource** — every Kubernetes object every package deploys, rendered
  as prose, plus one overview page per package and each package's README.
* **EnvironmentSource** — which packages are enabled in each environment.
* **CliSource** — the `adhar` CLI's command tree with its help text, parsed
  from the Cobra definitions in the Go source.

Before these existed the knowledge base held the platform's *rationale* — the
ADRs and guides — and none of its *configuration*. Asked which database
Keycloak used, it retrieved three unrelated package contracts; asked which
packages were enabled locally, a pull-request guide. The answers were in 548
manifests and five environment files nothing read.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any

from .documents import Document
from .manifests import Resource, parse_manifests, references, render, render_package_overview
from .trees import Tree

log = logging.getLogger("adhar_ai.rag.platform_sources")

MANIFEST_SUFFIXES = (".yaml", ".yml", ".yaml.tmpl")

#: Kinds whose individual documents add noise rather than knowledge. Their
#: existence still shows on the package overview; nobody asks the agent to
#: describe a RoleBinding.
SKIP_INDIVIDUAL = frozenset(
    {
        "ServiceAccount",
        "Role",
        "RoleBinding",
        "ClusterRole",
        "ClusterRoleBinding",
        "CustomResourceDefinition",
        "PodDisruptionBudget",
        "PriorityClass",
    }
)


# --------------------------------------------------------------------------- #
# Manifests
# --------------------------------------------------------------------------- #


class ManifestsSource:
    """What every package deploys, resource by resource."""

    origin = "manifests"

    def __init__(self, tree: Tree | None) -> None:
        self.tree = tree
        #: Kept after each run so the graph source can reuse the parse instead
        #: of reading 550 files a second time.
        self.resources: list[Resource] = []

    async def load(self) -> list[Resource]:
        if self.tree is None:
            return []
        files = await self.tree.files(MANIFEST_SUFFIXES)
        files = [f for f in files if "/manifests/" in f.path]
        texts = await self.tree.read_many([f.path for f in files])
        resources: list[Resource] = []
        for path in sorted(texts):
            resources.extend(parse_manifests(path, texts[path]))
        self.resources = resources
        log.info(
            "manifests: %d resource(s) from %d file(s) in %s",
            len(resources), len(files), self.tree.label,
        )
        return resources

    async def _contracts(self) -> dict[str, dict[str, Any]]:
        """Each package's `adhar-package.yaml`, for the overview page."""
        if self.tree is None:
            return {}
        import yaml

        files = await self.tree.files(("adhar-package.yaml",))
        files = [f for f in files if f.path.count("/") == 2]
        texts = await self.tree.read_many([f.path for f in files])
        out: dict[str, dict[str, Any]] = {}
        for path, text in texts.items():
            try:
                data = yaml.safe_load(text) or {}
            except yaml.YAMLError:
                continue
            if isinstance(data, dict):
                out["/".join(path.split("/")[:2])] = data
        return out

    async def documents(self) -> list[Document]:
        resources = await self.load()
        contracts = await self._contracts()
        docs: list[Document] = []
        by_package: dict[str, list[Resource]] = defaultdict(list)
        # The same kind/namespace/name is defined twice in sixteen places — a
        # cpu and a gpu variant of one Deployment, an upstream bundle that
        # repeats a ConfigMap. Two documents with one id overwrite each other
        # on EVERY refresh, which re-embeds them every time and makes the
        # incremental ingest look broken. The path and ordinal make ids unique
        # and stable across runs.
        seen: dict[str, int] = {}
        for r in resources:
            by_package[r.package].append(r)
            if r.kind in SKIP_INDIVIDUAL:
                continue
            refs = references(r)
            doc_id = f"manifest:{r.package}/{r.kind}/{r.namespace or '-'}/{r.name}"
            if doc_id in seen:
                seen[doc_id] += 1
                doc_id = f"{doc_id}@{r.path}#{seen[doc_id]}"
            else:
                seen[doc_id] = 0
            docs.append(
                Document(
                    doc_id=doc_id,
                    source=f"manifest {r.package}: {r.kind} {r.name}",
                    text=render(r, refs),
                    kind="manifest",
                    origin=self.origin,
                    metadata={
                        "package": r.package,
                        "kind": r.kind,
                        "name": r.name,
                        "namespace": r.namespace,
                        "path": r.path,
                        "hostnames": refs.hostnames,
                        "databases": refs.databases,
                    },
                )
            )
        for package, items in sorted(by_package.items()):
            docs.append(
                Document(
                    doc_id=f"manifest:{package}/overview",
                    source=f"package {package}: what it deploys",
                    text=render_package_overview(package, items, contracts.get(package)),
                    kind="manifest",
                    origin=self.origin,
                    metadata={"package": package, "resources": len(items)},
                )
            )
        docs.extend(await self._readmes())
        return docs

    async def _readmes(self) -> list[Document]:
        """Each package's README, which is how it is meant to be operated."""
        if self.tree is None:
            return []
        files = [f for f in await self.tree.files(("README.md",)) if f.path.count("/") == 2]
        texts = await self.tree.read_many([f.path for f in files])
        docs: list[Document] = []
        for path, text in sorted(texts.items()):
            package = "/".join(path.split("/")[:2])
            if not text.strip():
                continue
            docs.append(
                Document(
                    doc_id=f"manifest:{package}/README",
                    source=f"package {package} README",
                    text=text,
                    kind="doc",
                    origin=self.origin,
                    metadata={"package": package, "path": path},
                )
            )
        return docs


# --------------------------------------------------------------------------- #
# Environments
# --------------------------------------------------------------------------- #


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "yes", "1", "on"}


class EnvironmentSource:
    """Which packages are on in each environment, and where they deploy."""

    origin = "environments"

    def __init__(self, tree: Tree | None) -> None:
        self.tree = tree
        self.environments: dict[str, dict[str, Any]] = {}

    async def load(self) -> dict[str, dict[str, Any]]:
        if self.tree is None:
            return {}
        import yaml

        files = [f for f in await self.tree.files(("config.yaml",)) if f.path.count("/") == 1]
        texts = await self.tree.read_many([f.path for f in files])
        out: dict[str, dict[str, Any]] = {}
        for path, text in sorted(texts.items()):
            try:
                data = yaml.safe_load(text) or {}
            except yaml.YAMLError as exc:
                log.debug("environment %s did not parse: %s", path, exc)
                continue
            if not isinstance(data, dict):
                continue
            # The directory is canonical: `production/config.yaml` declares
            # `environment: prod`, and people say both.
            name = path.split("/")[0]
            out[name] = data
        self.environments = out
        return out

    async def documents(self) -> list[Document]:
        envs = await self.load()
        docs: list[Document] = []
        for name, data in sorted(envs.items()):
            rows = [p for p in (data.get("packages") or []) if isinstance(p, dict)]
            enabled = [p for p in rows if _truthy(p.get("enabled"))]
            disabled = [p for p in rows if not _truthy(p.get("enabled"))]
            by_category: dict[str, list[str]] = defaultdict(list)
            for p in enabled:
                by_category[str(p.get("category") or "unknown")].append(str(p.get("name")))
            namespaces = sorted({str(p.get("namespace")) for p in enabled if p.get("namespace")})

            alias = str(data.get("environment") or name)
            lines = [
                f"# Environment `{name}`" + (f" (`{alias}`)" if alias != name else ""),
                "",
                f"Type `{data.get('type', 'unknown')}`. {len(enabled)} of {len(rows)} packages "
                f"enabled, deploying into "
                f"{', '.join(f'`{n}`' for n in namespaces) or 'no namespace'}.",
                "",
                "## Enabled packages",
                "",
            ]
            for category in sorted(by_category):
                names = ", ".join(f"`{n}`" for n in sorted(by_category[category]))
                lines.append(f"- **{category}**: {names}")
            lines += ["", "## Disabled packages", ""]
            disabled.sort(key=lambda p: str(p.get("name")))
            lines.append(", ".join(f"`{p.get('name')}`" for p in disabled) or "none")
            lines += [
                "",
                "## Where each enabled package comes from",
                "",
            ]
            for p in sorted(enabled, key=lambda p: str(p.get("name"))):
                lines.append(
                    f"- `{p.get('name')}` ({p.get('category')}) in `{p.get('namespace')}` "
                    f"from `{p.get('manifestPath')}`"
                )
            docs.append(
                Document(
                    doc_id=f"environment:{name}",
                    source=f"environment {name}",
                    text="\n".join(lines) + "\n",
                    kind="environment",
                    origin=self.origin,
                    metadata={
                        "environment": name,
                        "alias": alias,
                        "type": data.get("type"),
                        "enabled": sorted(str(p.get("name")) for p in enabled),
                    },
                )
            )
        return docs


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

#: A Cobra command literal. `Use` is required; the rest are optional and may
#: be raw (backtick) or interpreted (double-quoted) strings.
#: Both `var x = &cobra.Command{` and `x := &cobra.Command{`; the second is
#: how a command built inside a constructor function is written.
COMMAND_RE = re.compile(
    r"(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s*:?=\s*&cobra\.Command\{(?P<body>.*?)\n\s*\}", re.S
)
FIELD_RE = re.compile(
    r"^\s*(?P<key>Use|Short|Long|Example)\s*:\s*(?:`(?P<raw>[^`]*)`|\"(?P<str>(?:[^\"\\\\]|\\\\.)*)\")\s*,",
    re.M | re.S,
)
ALIASES_RE = re.compile(r"^\s*Aliases\s*:\s*\[\]string\{(?P<items>[^}]*)\}", re.M)
ADD_RE = re.compile(r"(?P<parent>[A-Za-z_][A-Za-z0-9_.]*)\.AddCommand\((?P<children>[^)]*)\)", re.S)
ROOT_ADD_RE = re.compile(r"(?<![.\w])AddCommand\((?P<children>[^)]*)\)", re.S)


class CliSource:
    """The `adhar` CLI, from its Cobra definitions.

    Parsed from source rather than from `adhar --help` because the in-cluster
    runtime has the repository and not the binary. The help text is the same
    text; what is lost is anything computed at runtime, which for a help string
    is nothing.
    """

    origin = "cli"

    def __init__(self, tree: Tree | None, prefix: str = "cmd", binary: str = "adhar") -> None:
        self.tree = tree
        self.prefix = prefix
        self.binary = binary

    async def commands(self) -> dict[str, dict[str, Any]]:
        """Every command literal, keyed by `<package dir>:<variable>`.

        Go variable names are per package, and the platform has a `healthCmd`
        in both `cmd/health` and `cmd/db`. Keyed by variable alone the second
        overwrote the first's parent and `adhar db health` rendered as
        `adhar health` — a wrong command in a reference the agent quotes.
        """
        if self.tree is None:
            return {}
        files = await self.tree.files((".go",), self.prefix)
        files = [f for f in files if not f.path.endswith("_test.go")]
        texts = await self.tree.read_many([f.path for f in files])

        commands: dict[str, dict[str, Any]] = {}
        parents: dict[str, str] = {}
        roots: set[str] = set()
        for path, text in sorted(texts.items()):
            package = path.rsplit("/", 1)[0]
            for m in COMMAND_RE.finditer(text):
                body = m.group("body")
                fields: dict[str, str] = {}
                for f in FIELD_RE.finditer(body):
                    raw, quoted = f.group("raw"), f.group("str")
                    fields[f.group("key")] = (raw if raw is not None else quoted or "").strip()
                if "Use" not in fields:
                    continue
                aliases = ALIASES_RE.search(body)
                key = f"{package}:{m.group('var')}"
                commands[key] = {
                    "var": m.group("var"),
                    "use": fields["Use"],
                    "short": fields.get("Short", ""),
                    "long": fields.get("Long", ""),
                    "example": fields.get("Example", ""),
                    "aliases": re.findall(r'"([^"]+)"', aliases.group("items")) if aliases else [],
                    "file": path,
                    "package": package,
                }
            for m in ADD_RE.finditer(text):
                parent = m.group("parent").split(".")[-1]
                for child in re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", m.group("children")):
                    # Same package unless qualified (`get.GetCmd`), in which case
                    # the qualifier names the package directory under cmd/.
                    if "." in child:
                        qualifier, var = child.rsplit(".", 1)
                        child_key = f"{self.prefix}/{qualifier.split('.')[-1]}:{var}"
                    else:
                        child_key = f"{package}:{child}"
                    parents[child_key] = f"{package}:{parent}"
            if path.endswith("main.go"):
                qualified = r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*Cmd)"
                for m in ROOT_ADD_RE.finditer(text):
                    for qualifier, var in re.findall(qualified, m.group("children")):
                        roots.add(f"{self.prefix}/{qualifier}:{var}")

        for key, cmd in commands.items():
            cmd["parent"] = "" if key in roots else parents.get(key, "")
        return commands

    def _path(
        self, var: str, commands: dict[str, dict[str, Any]], seen: set[str] | None = None
    ) -> str:
        seen = seen or set()
        cmd = commands.get(var)
        if cmd is None or var in seen:
            return self.binary
        seen.add(var)
        own = cmd["use"].split()[0]
        parent = cmd.get("parent") or ""
        if not parent or parent not in commands:
            # The root command's own `Use` IS the binary name.
            return self.binary if own == self.binary else f"{self.binary} {own}"
        return f"{self._path(parent, commands, seen)} {own}"

    async def documents(self) -> list[Document]:
        commands = await self.commands()
        if not commands:
            return []
        # One document per top-level command group: `adhar get` with all of
        # its subcommands on one page, chunked by heading so a subcommand and
        # its help are one chunk.
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for var, cmd in commands.items():
            full = self._path(var, commands)
            cmd["full"] = full
            top = " ".join(full.split()[:2])
            groups[top].append(cmd)

        docs: list[Document] = []
        for top, cmds in sorted(groups.items()):
            cmds.sort(key=lambda c: c["full"])
            head = next((c for c in cmds if c["full"] == top), None)
            lines = [f"# `{top}`", ""]
            if head and head["short"]:
                lines += [head["short"], ""]
            for cmd in cmds:
                lines += [f"## `{cmd['full']}`", ""]
                if cmd["short"]:
                    lines.append(cmd["short"])
                if cmd["aliases"]:
                    lines.append("Aliases: " + ", ".join(f"`{a}`" for a in cmd["aliases"]) + ".")
                if cmd["long"]:
                    lines += ["", cmd["long"].strip()]
                if cmd["example"]:
                    lines += ["", "```", cmd["example"].strip(), "```"]
                lines.append("")
            docs.append(
                Document(
                    doc_id=f"cli:{top.replace(' ', '-')}",
                    source=f"CLI reference: {top}",
                    text="\n".join(lines),
                    kind="cli",
                    origin=self.origin,
                    metadata={"commands": [c["full"] for c in cmds]},
                )
            )
        return docs
