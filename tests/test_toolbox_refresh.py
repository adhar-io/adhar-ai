"""Tool re-discovery: a rolled MCP server must not require a runtime restart."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from adhar_ai.runtime.toolbox import MCPToolbox


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"{name} tool"
        self.input_schema = {"type": "object"}
        self.annotations = SimpleNamespace(readOnlyHint=True)


class _Session:
    """A live MCP session whose tool list changes between calls."""

    def __init__(self, listings: list[list[str]]) -> None:
        self._listings = listings
        self.calls = 0

    async def list_tools(self):
        names = self._listings[min(self.calls, len(self._listings) - 1)]
        self.calls += 1
        return SimpleNamespace(tools=[_Tool(n) for n in names])


@pytest.mark.asyncio
async def test_refresh_relists_a_live_session_and_drops_phantoms() -> None:
    tb = MCPToolbox({"observability": "http://obs:8080"})
    session = _Session([["promql"], ["promql", "traceql"], ["traceql"]])
    tb._sessions["observability"] = session  # a live session, as after connect()
    await tb.refresh()  # start-up inventory
    assert sorted(tb.tools) == ["promql"]
    await tb.refresh()  # the server rolled and gained a tool: no restart needed
    assert sorted(tb.tools) == ["promql", "traceql"]
    await tb.refresh()  # ...and shrank: the phantom must go away, not linger
    assert sorted(tb.tools) == ["traceql"]


@pytest.mark.asyncio
async def test_unknown_tool_triggers_one_rediscovery_before_failing() -> None:
    tb = MCPToolbox({"observability": "http://obs:8080"})
    session = _Session([["promql"]])
    tb._sessions["observability"] = session
    # Nothing discovered yet (the regression: start-up happened before the
    # server was serving this tool).
    assert "promql" not in tb.tools
    result = await tb.call("promql", {"query": "up"})
    # It re-listed (calls == 1) and now knows the tool; the call itself then
    # proceeds to the session, which our stub cannot serve — but it must NOT
    # answer "unknown tool" anymore.
    assert session.calls == 1
    assert "promql" in tb.tools
    assert result.get("error") != "unknown tool 'promql'"


@pytest.mark.asyncio
async def test_truly_unknown_tool_still_reports_unknown() -> None:
    tb = MCPToolbox({})
    result = await tb.call("nope", {})
    assert result["error"] == "unknown tool 'nope'"
