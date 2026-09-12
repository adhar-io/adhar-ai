"""The one and only write path (ADR-0024 §3).

`open_pr` creates a branch, commits file changes, and opens a Gitea pull
request. It never applies anything to a cluster. After a human merges, ArgoCD
reconciles — identical to a human contribution.
"""

from __future__ import annotations

from typing import Any

from ...clients.gitea import GiteaClient, PRRef
from ...config import GiteaConfig
from ...provenance import (
    PR_LABEL,
    branch_name,
    commit_trailer,
    pr_title,
    render_pr_body,
)
from .audit import emit, new_audit_id
from .policy import guard_write


async def open_pr(
    cfg: GiteaConfig,
    repo: str,
    changes: list[dict[str, str]],
    title: str,
    why: str,
    tool: str,
    model: str | None = None,
    user: str | None = None,
    client: GiteaClient | None = None,
) -> PRRef:
    """Open a PR carrying ``changes`` (a list of ``{path, content}``).

    Raises `WriteNotPermitted` before touching the network if policy forbids
    the repo, a path, or writes altogether on this server.
    """
    raw_paths = [str(c.get("path", "")) for c in changes]
    paths = guard_write(cfg, repo, raw_paths)

    audit_id = new_audit_id()
    branch = branch_name(title)
    full_title = pr_title(title)
    trailer = commit_trailer(model, audit_id, user)

    gitea = client or GiteaClient(cfg)
    owns = client is None
    try:
        await gitea.create_branch(repo, new_branch=branch, base="main")
        for path, change in zip(paths, changes, strict=True):
            await gitea.put_file(
                repo,
                path=path,
                content=str(change.get("content", "")),
                branch=branch,
                message=f"{full_title}\n\n{why}\n\n{trailer}",
            )
        pr = await gitea.create_pull_request(
            repo,
            head=branch,
            base="main",
            title=full_title,
            body=render_pr_body(why, audit_id, user, model, paths, tool),
            labels=[PR_LABEL],
        )
    finally:
        if owns:
            await gitea.aclose()

    ref = PRRef(
        repo=repo,
        number=int(pr.get("number", 0)),
        url=str(pr.get("html_url") or pr.get("url") or ""),
        branch=branch,
        files=paths,
    )
    emit(
        audit_id=audit_id,
        tool=tool,
        access="write",
        action="open_pr",
        repo=repo,
        pr=ref.number,
        url=ref.url,
        files=paths,
        user=user,
        model=model,
        decision="proposed",
    )
    return ref


def render_yaml_document(obj: dict[str, Any]) -> str:
    """Render a Kubernetes/Crossplane object as the platform writes them:
    2-space indent, keys in declaration order, provenance label already set."""
    import yaml

    return yaml.safe_dump(obj, sort_keys=False, default_flow_style=False, width=100)
