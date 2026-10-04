"""Where the platform's own files come from.

The knowledge sources that describe the platform — its manifests, its
environments, its package contracts, its documentation — all read files. Where
those files live differs by where the runtime is:

* on a workstation, a checkout of the `adhar` repository;
* in the cluster, **Gitea**, which holds the `packages` and `environments`
  repositories the platform is actually reconciled from.

The second is the one that matters. Before this, the in-cluster runtime read
documentation from an optional ConfigMap and nothing else, so it knew the
platform's rationale and none of its configuration. Reading Gitea makes the
knowledge base describe what is deployed, and keeps it current as the
repositories change — with no volume, no ConfigMap size limit, and no second
credential: the repositories are readable anonymously inside the cluster.

A `Tree` is the one interface both answer. Sources are written against it and
never know which one they have.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("adhar_ai.rag.trees")

#: Files larger than this are skipped. A manifest is a few kilobytes; a CRD
#: bundle can be several megabytes of schema that would drown the index in
#: field descriptions nobody asks about.
MAX_FILE_BYTES = 512_000

#: How many files to fetch concurrently from a remote tree. Enough to make a
#: thousand-file refresh take seconds rather than minutes; few enough that it
#: never looks like a flood to the Gitea pod.
FETCH_CONCURRENCY = 16


@dataclass(slots=True, frozen=True)
class TreeFile:
    """One file in a tree. `sha` is the content identity where the tree can
    supply one, so an unchanged file need not be read again."""

    path: str
    size: int = 0
    sha: str = ""


class Tree(Protocol):
    """A read-only view of a directory tree, local or remote."""

    #: Where this tree comes from, for logging and for the knowledge base's
    #: refresh report. `local:/path` or `gitea:packages@main`.
    label: str

    async def files(self, suffixes: tuple[str, ...] = (), prefix: str = "") -> list[TreeFile]:
        """Every file under `prefix` whose name ends with one of `suffixes`.

        Paths are relative to the tree root, `/`-separated, in sorted order so
        two refreshes of the same tree produce documents in the same order.
        """
        ...

    async def read(self, path: str) -> str:
        """The file's text. Undecodable bytes are replaced, never raised."""
        ...

    async def read_many(self, paths: list[str]) -> dict[str, str]:
        """Read several files; a path that fails is absent from the result."""
        ...


def _matches(path: str, suffixes: tuple[str, ...], prefix: str) -> bool:
    if prefix and not path.startswith(prefix.rstrip("/") + "/") and path != prefix:
        return False
    return not suffixes or path.endswith(suffixes)


class LocalTree:
    """A directory on disk — a repository checkout or a mounted ConfigMap."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.label = f"local:{self.root}"

    def exists(self) -> bool:
        return self.root.is_dir()

    async def files(self, suffixes: tuple[str, ...] = (), prefix: str = "") -> list[TreeFile]:
        if not self.root.is_dir():
            return []
        out: list[TreeFile] = []
        for file in sorted(self.root.rglob("*")):
            if not file.is_file():
                continue
            rel = file.relative_to(self.root).as_posix()
            if not _matches(rel, suffixes, prefix):
                continue
            try:
                size = file.stat().st_size
            except OSError:
                continue
            if size > MAX_FILE_BYTES:
                continue
            out.append(TreeFile(path=rel, size=size))
        return out

    async def read(self, path: str) -> str:
        return (self.root / path).read_text(encoding="utf-8", errors="replace")

    async def read_many(self, paths: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for path in paths:
            try:
                out[path] = await self.read(path)
            except OSError as exc:
                log.debug("local tree: cannot read %s: %s", path, exc)
        return out


class GiteaTree:
    """A repository in the platform's Gitea, read through its REST API.

    The tree listing is ONE paged call; the file reads are concurrent and
    bounded. A refresh of the `packages` repository is about 650 small reads,
    which at the default concurrency is a few seconds — and most of them are
    skipped by the store afterwards, because the content hash has not changed.
    """

    def __init__(self, client: Any, repo: str, ref: str = "main") -> None:
        self.client = client
        self.repo = repo
        self.ref = ref
        self.label = f"gitea:{repo}@{ref}"

    async def files(self, suffixes: tuple[str, ...] = (), prefix: str = "") -> list[TreeFile]:
        try:
            entries = await self.client.get_tree(self.repo, self.ref, recursive=True)
        except Exception as exc:  # noqa: BLE001 - a missing repo costs this tree, not the refresh
            log.warning("gitea tree %s unavailable: %s", self.label, exc)
            return []
        out: list[TreeFile] = []
        for entry in entries:
            if entry.get("type") != "blob":
                continue
            path = str(entry.get("path") or "")
            size = int(entry.get("size") or 0)
            if not _matches(path, suffixes, prefix) or size > MAX_FILE_BYTES:
                continue
            out.append(TreeFile(path=path, size=size, sha=str(entry.get("sha") or "")))
        return sorted(out, key=lambda f: f.path)

    async def read(self, path: str) -> str:
        return str(await self.client.get_raw(self.repo, path, self.ref))

    async def read_many(self, paths: list[str]) -> dict[str, str]:
        gate = asyncio.Semaphore(FETCH_CONCURRENCY)
        out: dict[str, str] = {}

        async def one(path: str) -> None:
            async with gate:
                try:
                    out[path] = await self.read(path)
                except Exception as exc:  # noqa: BLE001 - one unreadable file must not lose the rest
                    log.debug("gitea tree %s: cannot read %s: %s", self.label, path, exc)

        await asyncio.gather(*(one(p) for p in paths))
        return out


def tree_for(
    local: str | Path | None, gitea: Any | None, repo: str, ref: str = "main"
) -> Tree | None:
    """The tree a source should read, given what this runtime has.

    A local checkout wins when it exists — that is a developer's workstation,
    and the point of a checkout is to see what you have edited before it is
    pushed. Otherwise Gitea, which is the in-cluster case. Neither returns
    `None`, and the source then contributes nothing rather than failing.
    """
    if local:
        candidate = LocalTree(local)
        if candidate.exists():
            return candidate
    if gitea is not None:
        return GiteaTree(gitea, repo, ref)
    return None
