"""Per-tool scope gate for the write path.

Defence in depth with the platform's Kyverno `adhar-ai-guardrails` ClusterPolicy
and Gitea branch protection: this process refuses out-of-policy writes before a
request is ever made.
"""

from __future__ import annotations

import posixpath

from ...clients.errors import WriteNotPermitted
from ...config import GiteaConfig

#: Mirrors `writePolicy.allowedPathPrefixes` in the `adhar-ai-config` ConfigMap.
#: Entries are REPO-QUALIFIED: a file `security/kyverno-policies/x.yaml` in the
#: `packages` repo is checked as `packages/security/kyverno-policies/x.yaml`.
#: (Inside the repo itself the category directories sit at the root — the
#: ApplicationSet's manifestPath values are `security/vault/manifests` etc.)
DEFAULT_PATH_PREFIXES: tuple[str, ...] = ("packages/", "environments/")

#: Repos the bot may open a PR against, when GITEA_WRITE_REPOS is unset.
ALLOWED_REPOS: tuple[str, ...] = ("packages", "environments")


def normalize_path(path: str) -> str:
    """Reject traversal and absolute paths before they reach the Gitea API."""
    cleaned = posixpath.normpath(path.strip().lstrip("/"))
    if cleaned in {".", ""} or cleaned.startswith("../") or cleaned == "..":
        raise WriteNotPermitted(f"illegal file path {path!r}")
    return cleaned


def guard_write(
    cfg: GiteaConfig,
    repo: str,
    paths: list[str],
    prefixes: tuple[str, ...] = DEFAULT_PATH_PREFIXES,
) -> list[str]:
    """Validate a proposed change set. Returns the normalized paths."""
    if not cfg.write_enabled:
        raise WriteNotPermitted(
            "this MCP server is read-only (GITEA_WRITE_ENABLED=false); "
            "no write tool is available here"
        )
    if not cfg.bot_token:
        raise WriteNotPermitted(
            "no Gitea bot token configured (the adhar-ai-bot secret is unset), "
            "so no pull request can be opened"
        )
    allowed = cfg.write_repos or ALLOWED_REPOS
    if repo not in allowed:
        raise WriteNotPermitted(f"repo {repo!r} is not in the allowed set {list(allowed)}")
    if not paths:
        raise WriteNotPermitted("a proposal must change at least one file")
    normalized = [normalize_path(p) for p in paths]
    for path in normalized:
        qualified = f"{repo}/{path}"
        if not any(qualified.startswith(prefix) for prefix in prefixes):
            raise WriteNotPermitted(
                f"path {path!r} in repo {repo!r} is outside the allowed prefixes "
                f"{list(prefixes)}"
            )
    return normalized
