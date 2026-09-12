"""MCP client: mounts the seven domain servers as one tool namespace.

Uses the official `mcp` Python SDK's streamable-HTTP client against each
server's `/mcp` endpoint — the same transport an external agent (Claude Code, an
IDE assistant) uses through the Gateway, so the runtime is not a privileged
special case.
"""

from __future__ import annotations

import json
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ..gateway.types import FunctionSpec, ToolSpec
from ..mcp.common.tools import ACCESS_META_KEY

log = logging.getLogger("adhar_ai.toolbox")

#: Tools whose MCP annotation marks them `write`. Kept as a fallback for servers
#: that predate the annotation; the annotation is authoritative when present.
KNOWN_WRITE_TOOLS = frozenset(
    {"propose_change", "propose_xr", "propose_exception", "scaffold"}
)


@dataclass(slots=True)
class RemoteTool:
    name: str
    domain: str
    description: str
    schema: dict[str, Any]
    access: str

    @property
    def is_write(self) -> bool:
        return self.access == "write"

    def to_spec(self) -> ToolSpec:
        return ToolSpec(
            function=FunctionSpec(
                name=self.name,
                description=f"[{self.domain}/{self.access}] {self.description}".strip(),
                parameters=self.schema or {"type": "object", "properties": {}},
            )
        )


def _access_of(tool: Any) -> str:
    """The `adhar/access` tag rides in the tool's `_meta`; `readOnlyHint` is the
    standard-MCP fallback, and the known-write name list is the last resort."""
    meta = getattr(tool, "meta", None) or {}
    if value := meta.get(ACCESS_META_KEY):
        return str(value)
    annotations = getattr(tool, "annotations", None)
    if annotations is not None and getattr(annotations, "read_only_hint", None) is True:
        return "read"
    return "write" if tool.name in KNOWN_WRITE_TOOLS else "read"


class MCPToolbox:
    """Opens one session per configured MCP server and keeps them for the
    lifetime of the runtime process."""

    def __init__(self, servers: dict[str, str]) -> None:
        self.servers = servers
        self._stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._tools: dict[str, RemoteTool] = {}
        self.errors: dict[str, str] = {}

    async def __aenter__(self) -> MCPToolbox:
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def connect(self) -> None:
        for domain, base_url in self.servers.items():
            url = base_url.rstrip("/")
            if not url.endswith("/mcp"):
                url = f"{url}/mcp"
            try:
                read, write = await self._stack.enter_async_context(streamable_http_client(url))
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                listing = await session.list_tools()
            except Exception as exc:
                # A single unreachable domain must not take down the runtime;
                # the rest of the toolbox stays usable and /healthz reports it.
                self.errors[domain] = f"{type(exc).__name__}: {exc}"
                log.warning("mcp server %s unreachable at %s: %s", domain, url, exc)
                continue
            self._sessions[domain] = session
            for tool in listing.tools:
                self._tools[tool.name] = RemoteTool(
                    name=tool.name,
                    domain=domain,
                    description=(tool.description or "").strip(),
                    schema=dict(getattr(tool, "input_schema", None) or {}),
                    access=_access_of(tool),
                )

    async def aclose(self) -> None:
        await self._stack.aclose()
        self._sessions.clear()
        self._tools.clear()

    # ----------------------------------------------------------------------- #

    @property
    def tools(self) -> dict[str, RemoteTool]:
        return self._tools

    def specs(
        self, allowed: tuple[str, ...] = (), include_writes: bool = True
    ) -> list[ToolSpec]:
        """Tool specs for the LLM, filtered by the operator's allow-list and by
        whether autonomy permits offering write tools at all."""
        out = []
        for tool in self._tools.values():
            if allowed and tool.name not in allowed:
                continue
            if tool.is_write and not include_writes:
                continue
            out.append(tool.to_spec())
        return out

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            return {"error": f"unknown tool {name!r}", "available": sorted(self._tools)}
        session = self._sessions[tool.domain]
        result = await session.call_tool(name, arguments)
        return _unwrap(result)


def _unwrap(result: Any) -> dict[str, Any]:
    """Normalize an MCP CallToolResult into a plain dict for the model."""
    if getattr(result, "isError", False):
        return {"error": _text_of(result)}
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        # FastMCP wraps a non-dict return value under "result".
        return structured.get("result", structured) if len(structured) == 1 else structured
    text = _text_of(result)
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"text": text}
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def _text_of(result: Any) -> str:
    parts = []
    for block in getattr(result, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts)
