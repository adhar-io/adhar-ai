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


def _is_error(result: Any) -> bool:
    """Did the server flag this call as failed?

    Both spellings are checked on purpose. The wire field is `isError`, and the
    Python SDK models it as `is_error`; which one an object carries depends on
    whether it arrived as a parsed model or as a raw dict. Reading only the
    camelCase name — as this did — means `getattr` misses on every SDK model and
    a FAILED TOOL CALL IS HANDED TO THE MODEL AS A SUCCESSFUL TEXT RESULT, and
    is audited `decision="ok"`. That is the worst possible failure mode for an
    agent: it reasons on, and cites, an error string as if it were data.
    """
    for attribute in ("is_error", "isError"):
        value = getattr(result, attribute, None)
        if value is not None:
            return bool(value)
    if isinstance(result, dict):
        return bool(result.get("is_error") or result.get("isError"))
    return False


def _structured_of(result: Any) -> Any:
    """The structured payload, under either spelling (see `_is_error`)."""
    for attribute in ("structured_content", "structuredContent"):
        value = getattr(result, attribute, None)
        if value is not None:
            return value
    if isinstance(result, dict):
        return result.get("structured_content") or result.get("structuredContent")
    return None


def _unwrap(result: Any) -> dict[str, Any]:
    """Normalize an MCP CallToolResult into a plain dict for the model."""
    if _is_error(result):
        return {"error": _text_of(result)}
    structured = _structured_of(result)
    if isinstance(structured, dict):
        # FastMCP wraps a non-dict return value under "result". Unwrapping it can
        # yield a list, so it goes back through `_as_dict` — the declared return
        # type is a dict and `loop._invoke` does `"error" in output` on it.
        if len(structured) == 1 and "result" in structured:
            return _as_dict(structured["result"])
        return structured
    text = _text_of(result)
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"text": text}
    return _as_dict(parsed)


def _as_dict(value: Any) -> dict[str, Any]:
    """A tool result is always handed to the model as a JSON object."""
    return value if isinstance(value, dict) else {"result": value}


def _text_of(result: Any) -> str:
    parts = []
    for block in getattr(result, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts)
