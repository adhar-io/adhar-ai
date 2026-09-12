"""Adhar AI — the agentic control layer for the Adhar platform (ADR-0024).

Three roles ship from one image, selected by subcommand:

  * ``adhar_ai.mcp``      seven per-domain MCP tool servers (read tools call the
                          real platform APIs; write tools open Gitea PRs only)
  * ``adhar_ai.gateway``  a provider-agnostic, OpenAI-compatible LLM gateway
  * ``adhar_ai.runtime``  the plan-act-observe agent loop and its operators
"""

__version__ = "0.1.0"
