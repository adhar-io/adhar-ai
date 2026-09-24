"""Adhar AI — the agentic control layer for the Adhar platform (ADR-0024).

Three roles ship from one image, selected by subcommand:

  * ``adhar_ai.mcp``      seven per-domain MCP tool servers (read tools call the
                          real platform APIs; write tools open Gitea PRs only)
  * ``adhar_ai.gateway``  a provider-agnostic, OpenAI-compatible LLM gateway
  * ``adhar_ai.runtime``  the plan-act-observe agent loop and its operators
"""

__version__ = "0.2.0"


def build_info() -> dict[str, str]:
    """Which build this is, for `/healthz`.

    Every image the platform runs is `:latest` with `imagePullPolicy: Always`,
    which is the right default for an Adhar-owned component: a push to main is
    meant to reach the cluster. The cost is that a pod is otherwise anonymous —
    you cannot tell a rollout that picked up the new build from one that did
    not. CI bakes the commit in at build time and this reports it.

    `unknown` is honest rather than alarming: a local `docker build` or a
    `uv run` has no revision to report, and inventing one would be worse.
    """
    import os

    return {
        "version": os.environ.get("ADHAR_AI_BUILD_VERSION") or __version__,
        "revision": os.environ.get("ADHAR_AI_REVISION") or "unknown",
    }
