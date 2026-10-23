"""The platform knowledge bundle: docs and CLI reference without a Gitea mirror.

What is tested is what would make the agent wrong or the index thrash:

* the bundle is read, verified and chunked, and every document says which
  platform release it describes;
* a half-updated mount (new manifest, old part) is never indexed — the last
  good bundle is served, and with none the source fails rather than returning
  an empty origin the store would prune;
* the platform's docs and CLI never come from Gitea any more;
* a docs tree on disk still wins, and never shares the origin with the bundle.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from adhar_ai.rag import KnowledgeBase
from adhar_ai.rag.bundle import BundleInconsistent, BundleSource, PlatformBundle
from adhar_ai.rag.platform_sources import CliSource
from adhar_ai.rag.sources import DocsSource

FILES = {
    "README.md": "# Adhar\n\nAn internal developer platform.\n",
    "CHANGELOG.md": "# Changelog\n\n## v0.2.0\n\n- Knowledge bundle.\n",
    "release.md": "# Adhar release v0.2.0\n\nThis platform runs Adhar v0.2.0.\n",
    "docs/ARCHITECTURE.md": "# Architecture\n\n## Bootstrap\n\nCilium, then Argo CD, then Gitea.\n",
    "docs/adr/0024-agentic-ai-platform.md": (
        "# ADR-0024\n\n## Decision\n\nAgents behind a gateway.\n"
    ),
    "cli/adhar.md": "# `adhar` command reference\n\n## Commands\n\n- `adhar get` — Display\n",
    "cli/adhar-get.md": (
        "# `adhar get`\n\n## `adhar get`\n\nDisplay resources\n\n"
        "## `adhar get apps`\n\nList applications\n\n"
        "Flags:\n\n```\n      --all   Include disabled\n```\n"
    ),
    "examples/database.yaml": (
        "# A team database.\n# Applied with kubectl.\n"
        "apiVersion: platform.adhar.io/v1alpha1\nkind: Database\n"
    ),
}


def write_bundle(
    root: Path,
    files: dict[str, str] = FILES,
    version: str = "v0.2.0",
    part_bytes: int = 1 << 20,
) -> dict:
    """Write a bundle exactly as platform/knowledge does: tar.gz, split, manifest."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(files):
            data = files[path].encode()
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    blob = buf.getvalue()
    parts = []
    root.mkdir(parents=True, exist_ok=True)
    for i, off in enumerate(range(0, len(blob), part_bytes)):
        chunk = blob[off : off + part_bytes]
        key = f"part-{i:04d}"
        (root / key).write_bytes(chunk)
        parts.append(
            {
                "key": key,
                "configMap": f"adhar-ai-knowledge-{i}",
                "sha256": hashlib.sha256(chunk).hexdigest(),
                "size": len(chunk),
            }
        )
    manifest = {
        "schema": 1,
        "version": version,
        "commit": "abc1234",
        "buildDate": "2026-10-23",
        "digest": "sha256:" + hashlib.sha256(blob).hexdigest(),
        "size": len(blob),
        "files": sorted(files),
        "parts": parts,
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return manifest


async def test_the_bundle_becomes_docs_cli_and_examples_carrying_the_release(tmp_path: Path):
    write_bundle(tmp_path)
    bundle = PlatformBundle(tmp_path)

    docs = {d.doc_id: d for d in await BundleSource(bundle, "docs").documents()}
    assert set(docs) == {
        "docs:README.md",
        "docs:CHANGELOG.md",
        "docs:release.md",
        "docs:docs/ARCHITECTURE.md",
        "docs:docs/adr/0024-agentic-ai-platform.md",
    }
    assert docs["docs:docs/adr/0024-agentic-ai-platform.md"].kind == "adr"
    assert all(d.origin == "docs" for d in docs.values())

    cli = {d.doc_id: d for d in await BundleSource(bundle, "cli").documents()}
    # The same ids the source parser emits, so rows are updated in place.
    assert set(cli) == {"cli:adhar", "cli:adhar-get"}
    get = cli["cli:adhar-get"]
    assert get.kind == "cli" and get.source == "CLI reference: adhar get"
    assert get.metadata["commands"] == ["adhar get", "adhar get apps"]

    examples = await BundleSource(bundle, "examples").documents()
    assert [d.doc_id for d in examples] == ["examples:examples/database.yaml"]

    for doc in [*docs.values(), *cli.values(), *examples]:
        assert doc.metadata["platform_version"] == "v0.2.0"
        assert doc.metadata["platform_commit"] == "abc1234"
        assert doc.metadata["bundle_digest"].startswith("sha256:")
        for chunk in doc.chunks():
            assert chunk.metadata["platform_version"] == "v0.2.0"


async def test_a_yaml_comment_in_an_example_is_not_a_heading(tmp_path: Path):
    write_bundle(tmp_path)
    (example,) = await BundleSource(PlatformBundle(tmp_path), "examples").documents()
    chunks = example.chunks()
    # One heading — the example's own title — so one chunk holding the whole
    # file, comments included, rather than one chunk per YAML comment.
    assert len(chunks) == 1
    assert "kind: Database" in chunks[0].text and "A team database." in chunks[0].text


async def test_a_bundle_split_across_parts_reassembles(tmp_path: Path):
    big = {
        **FILES,
        "docs/BIG.md": "# Big\n\n" + "".join(f"line {i} {i * 7919}\n" for i in range(20000)),
    }
    manifest = write_bundle(tmp_path, big, part_bytes=4096)
    assert len(manifest["parts"]) > 2
    docs = await BundleSource(PlatformBundle(tmp_path), "docs").documents()
    assert any(d.doc_id == "docs:docs/BIG.md" for d in docs)


async def test_no_bundle_published_yet_is_nothing_not_an_error(tmp_path: Path):
    assert await BundleSource(PlatformBundle(tmp_path), "docs").documents() == []


async def test_a_half_updated_mount_is_never_indexed(tmp_path: Path):
    write_bundle(tmp_path, part_bytes=256)
    (tmp_path / "part-0001").write_bytes(b"stale bytes from the previous release")

    # No earlier bundle: fail the origin. An empty list would make the store
    # delete every docs row now and re-embed all of them on the next refresh.
    with pytest.raises(BundleInconsistent):
        await BundleSource(PlatformBundle(tmp_path), "docs").documents()

    # With an earlier good bundle, keep serving it through the update.
    write_bundle(tmp_path, version="v0.2.0")
    bundle = PlatformBundle(tmp_path)
    assert (await BundleSource(bundle, "docs").documents())[0].metadata[
        "platform_version"
    ] == "v0.2.0"
    write_bundle(tmp_path, {**FILES, "docs/NEW.md": "# New\n"}, version="v0.3.0", part_bytes=256)
    (tmp_path / "part-0000").write_bytes(b"not yet refreshed by the kubelet")
    served = await BundleSource(bundle, "docs").documents()
    assert {d.metadata["platform_version"] for d in served} == {"v0.2.0"}

    # Once the parts catch up, the new release is indexed.
    write_bundle(tmp_path, {**FILES, "docs/NEW.md": "# New\n"}, version="v0.3.0", part_bytes=256)
    upgraded = await BundleSource(bundle, "docs").documents()
    assert {d.metadata["platform_version"] for d in upgraded} == {"v0.3.0"}
    assert any(d.doc_id == "docs:docs/NEW.md" for d in upgraded)


async def test_an_unknown_bundle_schema_is_ignored(tmp_path: Path):
    write_bundle(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    manifest["schema"] = 2
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert await BundleSource(PlatformBundle(tmp_path), "docs").documents() == []


class RecordingGitea:
    """A Gitea that serves the GitOps repositories and remembers every repo asked."""

    def __init__(self) -> None:
        self.repos: list[str] = []

    async def get_tree(self, repo: str, ref: str = "main", *, recursive: bool = True):
        self.repos.append(repo)
        return []

    async def get_raw(self, repo: str, path: str, ref: str = "main") -> str:
        self.repos.append(repo)
        return ""


async def test_the_platform_docs_and_cli_never_come_from_gitea(tmp_path: Path):
    write_bundle(tmp_path / "bundle")
    gitea = RecordingGitea()
    kb = KnowledgeBase.build(
        dsn="",
        docs_path=str(tmp_path / "no-docs-mounted"),
        gitea=gitea,
        knowledge_path=str(tmp_path / "bundle"),
    )
    by_origin = {s.origin: s for s in kb.sources}
    assert isinstance(by_origin["docs"], BundleSource)
    assert isinstance(by_origin["cli"], BundleSource)
    assert isinstance(by_origin["examples"], BundleSource)

    reports = {r.origin: r for r in await kb.refresh()}
    assert not reports["docs"].error and reports["docs"].documents == 5
    assert reports["cli"].documents == 2
    # The GitOps repositories are still read from Gitea; the source mirror is not.
    assert "adhar" not in gitea.repos
    assert {"packages", "environments"} <= set(gitea.repos)
    # And the lexical index answers from the bundle.
    hits = kb._lexical_hits("which flag includes disabled applications", 3, ())
    assert hits and hits[0].doc_id == "cli:adhar-get"


async def test_without_a_bundle_or_checkout_there_is_no_cli_source_at_all(tmp_path: Path):
    kb = KnowledgeBase.build(dsn="", gitea=RecordingGitea())
    assert "cli" not in {s.origin for s in kb.sources}


async def test_a_docs_tree_on_disk_wins_and_never_shares_the_origin(tmp_path: Path):
    write_bundle(tmp_path / "bundle")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "local.md").write_text("# Local\n\nEdited, not yet pushed.\n")
    kb = KnowledgeBase.build(dsn="", docs_path=str(docs), knowledge_path=str(tmp_path / "bundle"))
    docs_sources = [s for s in kb.sources if s.origin == "docs"]
    assert len(docs_sources) == 1 and isinstance(docs_sources[0], DocsSource)


async def test_a_checkout_still_parses_the_cli_from_source(tmp_path: Path):
    (tmp_path / "repo" / "cmd").mkdir(parents=True)
    write_bundle(tmp_path / "bundle")
    kb = KnowledgeBase.build(
        dsn="", cli_path=str(tmp_path / "repo"), knowledge_path=str(tmp_path / "bundle")
    )
    cli = [s for s in kb.sources if s.origin == "cli"]
    assert len(cli) == 1 and isinstance(cli[0], CliSource)


async def test_a_half_updated_bundle_fails_its_origin_instead_of_emptying_it(tmp_path: Path):
    write_bundle(tmp_path, part_bytes=256)
    (tmp_path / "part-0000").write_bytes(b"mid-update")
    kb = KnowledgeBase.build(dsn="", knowledge_path=str(tmp_path))
    reports = {r.origin: r for r in await kb.refresh(only=("docs",))}
    assert reports["docs"].error and "BundleInconsistent" in reports["docs"].error


def test_the_runtime_reads_the_bundle_path_from_the_environment(monkeypatch):
    from adhar_ai.config import RuntimeEnv

    monkeypatch.setenv("ADHAR_AI_KNOWLEDGE_PATH", "/var/lib/adhar-ai/knowledge")
    assert RuntimeEnv.from_env().knowledge_path == "/var/lib/adhar-ai/knowledge"
