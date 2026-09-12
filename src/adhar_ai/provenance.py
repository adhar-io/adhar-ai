"""Provenance markers stamped on everything Adhar AI creates.

ADR-0024 decision point 6: every AI-originated artefact must be attributable.
The platform's Kyverno `adhar-ai-guardrails` ClusterPolicy audits for the
`adhar.io/origin: adhar-ai` label, so the same marker is used for Kubernetes
labels, Gitea branch names, PR titles and PR labels.
"""

from __future__ import annotations

import re
import secrets

ORIGIN_LABEL_KEY = "adhar.io/origin"
ORIGIN_LABEL_VALUE = "adhar-ai"
ORIGIN_LABELS = {ORIGIN_LABEL_KEY: ORIGIN_LABEL_VALUE}

#: Every branch the agent pushes is namespaced so a human can spot (and bulk
#: clean) agent branches at a glance, and so Gitea branch protection can key on
#: the prefix.
BRANCH_PREFIX = "adhar-ai/"

#: Every PR the agent opens is titled with this prefix and carries this label.
PR_TITLE_PREFIX = "[adhar-ai]"
PR_LABEL = "adhar-ai"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 48) -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug[:max_len].strip("-") or "change"


def short_id() -> str:
    return secrets.token_hex(3)


def branch_name(title: str) -> str:
    """`adhar-ai/<slug>-<short id>` — unique per proposal."""
    return f"{BRANCH_PREFIX}{slugify(title)}-{short_id()}"


def pr_title(title: str) -> str:
    if title.startswith(PR_TITLE_PREFIX):
        return title
    return f"{PR_TITLE_PREFIX} {title}"


def commit_trailer(model: str | None, audit_id: str, user: str | None) -> str:
    lines = [f"Proposed-by: adhar-ai (model={model or 'unset'})", f"Audit-Id: {audit_id}"]
    if user:
        lines.append(f"Requested-by: {user}")
    lines.append(f"{ORIGIN_LABEL_KEY}: {ORIGIN_LABEL_VALUE}")
    return "\n".join(lines)


def render_pr_body(
    why: str,
    audit_id: str,
    user: str | None,
    model: str | None,
    files: list[str],
    tool: str,
) -> str:
    """PR body. Deliberately states the safety property in every PR."""
    file_list = "\n".join(f"- `{f}`" for f in files) or "- _(no files)_"
    return f"""## Why

{why}

## Files changed

{file_list}

## Provenance

| | |
|---|---|
| Origin | `{ORIGIN_LABEL_KEY}: {ORIGIN_LABEL_VALUE}` |
| Tool | `{tool}` |
| Model | `{model or "unset"}` |
| Requested by | {user or "_operator (event-driven)_"} |
| Audit id | `{audit_id}` |

---

This pull request was authored by **Adhar AI** (ADR-0024). Opening a PR is the
agent's *only* write path: it holds no cluster-mutating credential and never
calls `kubectl apply`, `argocd app sync`, or a cloud mutation API. Review and
merge as you would any human contribution — ArgoCD reconciles after the merge.
"""
