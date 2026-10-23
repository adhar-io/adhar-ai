"""The platform knowledge bundle: the docs of the release the cluster runs.

The platform controller publishes a bundle of this release's documentation —
`docs/` (guides, ADRs, designs), `examples/`, `README.md`, `CHANGELOG.md`, a
`release.md` naming the version, and the `adhar` CLI reference rendered from
the binary's own Cobra tree — as the `adhar-ai-knowledge-<n>` ConfigMaps, which
the runtime and the reindex CronJob mount through one projected volume at
`ADHAR_AI_KNOWLEDGE_PATH`:

    manifest.json   schema, version, commit, digest, files, parts (part 0 only)
    part-0000       the bundle (a tar.gz), split because a ConfigMap is ≤1 MiB
    part-0001 ...

This replaced a Gitea pull mirror of github.com/adhar-io/adhar (2026-10-23).
The mirror needed egress from the cluster, tracked upstream `main` rather than
the release actually deployed, and only fed the CLI parser — the docs ConfigMap
the indexer also looked for was never created, so in the cluster the agent had
no documentation at all. The bundle needs no network and no credential, and an
upgraded controller re-publishes it, so the knowledge moves with the platform.

**Consistency.** The ConfigMaps are written one at a time and the kubelet
refreshes the mount on its own schedule, so a refresh can see a new manifest
beside an old part. Every part is checked against the manifest; a set that does
not verify is never indexed. The last good bundle is served instead, and with
none the source RAISES rather than returning nothing — an empty result would
make the store prune every document of the origin, then re-embed all of them a
minute later.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import tarfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .documents import Document
from .sources import classify_doc

log = logging.getLogger("adhar_ai.rag.bundle")

#: The bundle format this reader understands (platform/knowledge.Schema).
SCHEMA = 1
MANIFEST = "manifest.json"
#: Files larger than this are left out of the index; see trees.MAX_FILE_BYTES.
MAX_FILE_BYTES = 512_000


class BundleInconsistent(RuntimeError):
    """The mounted parts do not match the mounted manifest (mid-update)."""


@dataclass(slots=True)
class BundleContents:
    """A verified bundle: its manifest and its files as text."""

    manifest: dict[str, Any]
    files: dict[str, str] = field(default_factory=dict)

    @property
    def version(self) -> str:
        return str(self.manifest.get("version") or "unknown")

    @property
    def commit(self) -> str:
        return str(self.manifest.get("commit") or "")

    @property
    def digest(self) -> str:
        return str(self.manifest.get("digest") or "")


class PlatformBundle:
    """Reads, verifies and caches the bundle mounted at `path`."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.label = f"bundle:{self.path}"
        self._good: BundleContents | None = None

    def exists(self) -> bool:
        return (self.path / MANIFEST).is_file()

    def load(self) -> BundleContents | None:
        """The current bundle; `None` when none is published.

        Raises `BundleInconsistent` only when the mount is mid-update AND no
        earlier bundle verified — otherwise the last good one is returned.
        """
        manifest_file = self.path / MANIFEST
        if not manifest_file.is_file():
            if self._good is None:
                log.info("no platform knowledge bundle at %s", self.path)
            return self._good
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return self._keep(f"manifest unreadable: {exc}")
        if not isinstance(manifest, dict):
            return self._keep("manifest is not an object")
        if manifest.get("schema") != SCHEMA:
            # A newer controller with a format this runtime cannot read: say so
            # and index nothing from it rather than misread it.
            log.warning(
                "knowledge bundle schema %r is not %d; upgrade adhar-ai",
                manifest.get("schema"),
                SCHEMA,
            )
            return self._good
        digest = str(manifest.get("digest") or "")
        if self._good is not None and self._good.digest == digest:
            return self._good

        data = bytearray()
        for part in manifest.get("parts") or []:
            key = str(part.get("key") or "")
            try:
                chunk = (self.path / key).read_bytes()
            except OSError as exc:
                return self._keep(f"part {key} unreadable: {exc}")
            if hashlib.sha256(chunk).hexdigest() != part.get("sha256"):
                return self._keep(f"part {key} does not match the manifest")
            data += chunk
        if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
            return self._keep("bundle digest does not match the manifest")

        files: dict[str, str] = {}
        with tarfile.open(fileobj=io.BytesIO(bytes(data)), mode="r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile() or member.size > MAX_FILE_BYTES:
                    continue
                name = PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts:
                    continue
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                files[str(name)] = handle.read().decode("utf-8", errors="replace")
        self._good = BundleContents(manifest=manifest, files=files)
        log.info(
            "platform knowledge bundle %s (%s): %d file(s)",
            self._good.version,
            digest[:19],
            len(files),
        )
        return self._good

    def _keep(self, reason: str) -> BundleContents | None:
        if self._good is not None:
            log.info("knowledge bundle mid-update (%s); serving %s", reason, self._good.version)
            return self._good
        raise BundleInconsistent(reason)


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip().strip("`")
    return ""


class BundleSource:
    """One origin's documents out of the bundle.

    Three of them are built over one `PlatformBundle`: `docs` (docs/, README,
    CHANGELOG, release.md), `cli` (the command reference) and `examples`. They
    keep the origins and the `cli:` document ids the Gitea-derived sources
    used, so an upgraded runtime updates those rows in place instead of leaving
    the old ones beside the new.
    """

    def __init__(self, bundle: PlatformBundle, origin: str) -> None:
        if origin not in ("docs", "cli", "examples"):
            raise ValueError(f"no bundle origin {origin!r}")
        self.bundle = bundle
        self.origin = origin

    def _wanted(self, path: str) -> bool:
        if self.origin == "cli":
            return path.startswith("cli/") and path.endswith(".md")
        if self.origin == "examples":
            return path.startswith("examples/") and path.endswith((".md", ".yaml", ".yml"))
        return (path.startswith("docs/") and path.endswith(".md")) or path in (
            "README.md",
            "CHANGELOG.md",
            "release.md",
        )

    async def documents(self) -> list[Document]:
        contents = self.bundle.load()
        if contents is None:
            return []
        meta = {
            "platform_version": contents.version,
            "platform_commit": contents.commit,
            "bundle_digest": contents.digest,
        }
        docs: list[Document] = []
        for path in sorted(contents.files):
            text = contents.files[path]
            if not self._wanted(path) or not text.strip():
                continue
            docs.append(self._document(path, text, meta))
        return docs

    def _document(self, path: str, text: str, meta: dict[str, str]) -> Document:
        if self.origin == "cli":
            name = PurePosixPath(path).stem
            title = _first_heading(text) or name.replace("-", " ")
            commands = [
                line[3:].strip().strip("`") for line in text.splitlines() if line.startswith("## `")
            ]
            return Document(
                doc_id=f"cli:{name}",
                source=f"CLI reference: {title}",
                text=text,
                kind="cli",
                origin=self.origin,
                metadata={**meta, "path": path, "commands": commands},
            )
        if self.origin == "examples" and not path.endswith(".md"):
            # Indented, not fenced: the chunker splits on lines starting with
            # `#`, and every YAML comment would otherwise become a heading.
            body = "\n".join("    " + line for line in text.splitlines())
            text = f"# Example `{path}`\n\nThe example configuration `{path}`.\n\n{body}\n"
        return Document(
            doc_id=f"{self.origin}:{path}",
            source=path,
            text=text,
            kind="doc" if self.origin == "examples" else classify_doc(Path(path)),
            origin=self.origin,
            metadata={**meta, "path": path},
        )
