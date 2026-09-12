"""Tool decorators carrying Adhar's read/write tag.

The MCP spec's `ToolAnnotations` is a closed model, so the platform-specific
`adhar/access` tag rides in the tool's `_meta` (which is an open map and
survives `tools/list` to the client). `readOnlyHint` is set alongside it so
generic MCP clients — Claude Code, IDE assistants — also see the distinction.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from mcp.types import ToolAnnotations

from .audit import audited

ACCESS_META_KEY = "adhar/access"

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def access_tools(mcp: Any, domain: str) -> tuple[Callable[[F], F], Callable[[F], F]]:
    """Return ``(read, write)`` decorators for one domain's tools.

    Both wrap the function in the audit decorator, so no tool can be registered
    without emitting an audit event.
    """

    def make(access: str) -> Callable[[F], F]:
        def decorator(fn: F) -> F:
            annotated = mcp.tool(
                annotations=ToolAnnotations(
                    read_only_hint=(access == "read"),
                    destructive_hint=False,  # a write opens a PR; it destroys nothing
                    idempotent_hint=(access == "read"),
                    open_world_hint=True,
                ),
                meta={ACCESS_META_KEY: access},
            )
            return annotated(audited(access, domain)(fn))

        return decorator

    return make("read"), make("write")
