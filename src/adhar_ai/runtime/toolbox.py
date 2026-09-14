"""MCP client: mounts the seven domain servers as one tool namespace.

Uses the official `mcp` Python SDK's streamable-HTTP client against each
server's `/mcp` endpoint — the same transport an external agent (Claude Code, an
IDE assistant) uses through the Gateway, so the runtime is not a privileged
special case.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
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
    """One MCP session per domain server, rebuilt when a server goes away.

    Each domain gets its OWN `AsyncExitStack`. That is not tidiness: with one
    shared stack no single session can be torn down and rebuilt, so when an MCP
    Deployment rolled — an image update, a node drain, an OOM kill — the runtime
    kept a dead session for that domain forever. Every call through it failed
    while `/healthz` still listed the domain as connected, because health was a
    snapshot taken at start-up rather than a fact about now. Nothing alerted, and
    only restarting the runtime brought the tools back.
    """

    def __init__(self, servers: dict[str, str]) -> None:
        self.servers = servers
        self._stacks: dict[str, AsyncExitStack] = {}
        self._sessions: dict[str, ClientSession] = {}
        self._tools: dict[str, RemoteTool] = {}
        self.errors: dict[str, str] = {}

    async def __aenter__(self) -> MCPToolbox:
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    @staticmethod
    def _mcp_url(base_url: str) -> str:
        url = base_url.rstrip("/")
        return url if url.endswith("/mcp") else f"{url}/mcp"

    async def connect(self, domains: Iterable[str] | None = None) -> None:
        """Open a session per domain. Pass `domains` to (re)connect only some."""
        for domain in domains if domains is not None else list(self.servers):
            base_url = self.servers.get(domain)
            if base_url is None:
                continue
            url = self._mcp_url(base_url)
            stack = AsyncExitStack()
            try:
                # NO timeout around these four lines, deliberately. They ENTER
                # long-lived contexts whose cancel scopes must outlive this
                # block; wrapping them in `fail_after` makes anyio tear the scope
                # down at the end of the `with`, which breaks every connection —
                # including the healthy ones. Bound the handshake at the
                # transport instead if it ever needs bounding.
                read, write = await stack.enter_async_context(streamable_http_client(url))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                listing = await session.list_tools()
            except BaseException as exc:  # noqa: BLE001
                # BaseException, not Exception. `streamable_http_client` runs its
                # own anyio task group, so a connection failure surfaces as a
                # BaseExceptionGroup or a scope CancelledError — neither of which
                # `except Exception` catches. Letting one escape took down the
                # whole sweep and left a half-entered stack behind.
                await self._discard(stack)
                self.errors[domain] = _describe(exc)
                self._sessions.pop(domain, None)
                log.warning("mcp server %s unreachable at %s: %s", domain, url, self.errors[domain])
                if isinstance(exc, KeyboardInterrupt | SystemExit):
                    raise
                continue

            # Replace cleanly: drop any previous session's tools first, so a
            # server that came back with a smaller tool list does not leave
            # phantom entries pointing at tools it no longer serves.
            self._forget(domain)
            self._stacks[domain] = stack
            self._sessions[domain] = session
            self.errors.pop(domain, None)
            for tool in listing.tools:
                self._tools[tool.name] = RemoteTool(
                    name=tool.name,
                    domain=domain,
                    description=(tool.description or "").strip(),
                    schema=dict(getattr(tool, "input_schema", None) or {}),
                    access=_access_of(tool),
                )

    def _forget(self, domain: str) -> None:
        """Drop a domain's session and tools, keeping its stack for the caller."""
        self._sessions.pop(domain, None)
        for name in [n for n, t in self._tools.items() if t.domain == domain]:
            del self._tools[name]

    @staticmethod
    async def _discard(stack: AsyncExitStack) -> None:
        """Close a stack, tolerating a close from a different task.

        anyio ties a cancel scope to the task that opened it, so unwinding a
        session from a request task raises rather than closing. The connection
        is then released when the process exits. That is an acceptable cost for
        a rare event (a server restart) and far better than keeping a session
        that can no longer carry a call.
        """
        try:
            await stack.aclose()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # noqa: BLE001
            # Same reason as in `connect`: unwinding another task's cancel scope
            # raises rather than closing. The socket is released at process exit,
            # which is an acceptable cost for a rare server restart.
            log.debug("could not unwind an MCP session cleanly: %s", _describe(exc))

    async def reconnect(self, domain: str) -> bool:
        """Rebuild one domain's session. Returns whether it is usable after.

        Only ever called for a domain with NO live session — one that failed to
        connect at start-up, or whose session a call has already been seen to
        fail on. It deliberately does not probe healthy sessions: a liveness
        ping issued from a different task than the one that opened the session
        tears down its cancel scope, which killed every healthy domain at once
        when it was tried.
        """
        stack = self._stacks.pop(domain, None)
        self._forget(domain)
        if stack is not None:
            await self._discard(stack)
        await self.connect([domain])
        return domain in self._sessions

    @property
    def unhealthy(self) -> list[str]:
        """Configured domains with no live session."""
        return sorted(d for d in self.servers if d not in self._sessions)

    async def aclose(self) -> None:
        for stack in list(self._stacks.values()):
            await self._discard(stack)
        self._stacks.clear()
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

    async def refresh(self) -> list[str]:
        """Re-list tools on every reachable domain. Returns domains refreshed.

        Discovery used to happen exactly once, in :meth:`connect`. The
        consequence on the platform: when an MCP Deployment rolled (a config
        change, an image bump), the runtime kept its start-up tool list and
        answered *"please enable the observability promql tool"* for a tool that
        was being served the whole time — until the runtime itself was
        restarted. Tool inventories are cheap to re-read and servers change
        underneath a long-lived runtime, so re-discovery has to be a normal
        operation, not a restart.

        Only domains WITHOUT a live session are rebuilt (see :meth:`reconnect`
        for why a healthy session must not be probed from another task); live
        ones are re-listed over their existing session.
        """
        refreshed: list[str] = []
        for domain in list(self.servers):
            session = self._sessions.get(domain)
            if session is None:
                if await self.reconnect(domain):
                    refreshed.append(domain)
                continue
            try:
                listing = await session.list_tools()
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, KeyboardInterrupt | SystemExit):
                    raise
                # The session is dead; rebuild it like a failed start-up.
                self.errors[domain] = _describe(exc)
                if await self.reconnect(domain):
                    refreshed.append(domain)
                continue
            for n in [n for n, t in self._tools.items() if t.domain == domain]:
                del self._tools[n]
            for tool in listing.tools:
                self._tools[tool.name] = RemoteTool(
                    name=tool.name,
                    domain=domain,
                    description=(tool.description or "").strip(),
                    schema=dict(getattr(tool, "input_schema", None) or {}),
                    access=_access_of(tool),
                )
            refreshed.append(domain)
        return refreshed

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            # Unknown to THIS process's inventory is not the same as unknown to
            # the platform: a server may have rolled since start-up. Refresh
            # once before declaring the tool absent.
            await self.refresh()
            tool = self._tools.get(name)
        if tool is None:
            return {"error": f"unknown tool {name!r}", "available": sorted(self._tools)}
        session = self._sessions.get(tool.domain)
        if session is None:
            return {
                "error": (
                    f"the {tool.domain} MCP server is not currently connected, so "
                    f"{name!r} cannot be called; it will be retried automatically"
                ),
                "domain": tool.domain,
            }
        try:
            result = await session.call_tool(name, arguments)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # noqa: BLE001
            # BaseException, not Exception: when the server goes away mid-call
            # the SDK's task group cancels the pending request, which surfaces
            # as `CancelledError` — a BaseException that `except Exception`
            # lets straight through, taking the whole agent run with it.
            #
            # A transport failure means the SESSION is gone, not that the tool
            # failed. Mark the domain unhealthy so `/healthz` stops claiming it
            # works and the reconnect loop rebuilds it, and tell the model
            # plainly rather than handing it an opaque error to reason around.
            reason = _describe(exc)
            self.errors[tool.domain] = reason
            self._sessions.pop(tool.domain, None)
            log.warning("mcp session for %s failed mid-call: %s", tool.domain, reason)
            return {
                "error": (
                    f"the {tool.domain} MCP server connection failed during "
                    f"{name!r} ({reason}); it will be reconnected automatically"
                ),
                "domain": tool.domain,
            }
        return _unwrap(result)


def _describe(exc: BaseException) -> str:
    """A one-line reason an operator can act on.

    anyio reports a failed connection as a `CancelledError` from the scope that
    was torn down, with the real cause — `ConnectError: All connection attempts
    failed` — buried in a group or in `__cause__`. Reporting the outer wrapper
    puts "Cancelled via cancel scope 0x..." in `/healthz`, which tells an
    operator nothing about why a domain is missing.
    """
    candidates = [
        c for c in _causes(exc) if type(c).__name__ not in _UNINFORMATIVE
    ]
    # Prefer something that names a network cause; anyio's internal stream
    # signals (WouldBlock, EndOfStream) are true but say nothing useful.
    for candidate in candidates:
        text = str(candidate).strip()
        if text:
            return f"{type(candidate).__name__}: {text}"
    return "connection failed (the server did not complete the MCP handshake)"


#: Exception types that are true but tell an operator nothing. anyio raises
#: these while unwinding a failed connection, and they otherwise end up in
#: `/healthz` in place of "All connection attempts failed".
_UNINFORMATIVE = frozenset(
    {"CancelledError", "WouldBlock", "EndOfStream", "ClosedResourceError",
     "BrokenResourceError", "GeneratorExit"}
)


def _causes(exc: BaseException, depth: int = 0) -> list[BaseException]:
    """The exception and everything nested inside it, most specific first."""
    if depth > 6:
        return []
    found: list[BaseException] = []
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            found.extend(_causes(sub, depth + 1))
    else:
        found.append(exc)
    for nested in (exc.__cause__, exc.__context__):
        if nested is not None and nested is not exc:
            found.extend(_causes(nested, depth + 1))
    return found


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
