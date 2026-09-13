"""An MCP server that restarts must not cost the runtime that domain forever.

MCP servers are Deployments. They roll on any image update, node drain or OOM
kill. Before this, the runtime opened one session per domain at start-up and
held it for the process lifetime: when a server went away, every call through
that session failed, `/healthz` still listed the domain as connected, and only
restarting the runtime brought the tools back.

These tests drive real server processes, because that is the only way to
produce the failure — an in-process fake cannot lose its transport.
"""

from __future__ import annotations

import subprocess
import time

import httpx
import pytest

from adhar_ai.runtime.toolbox import MCPToolbox, _describe

PORT = 18497
BASE = f"http://127.0.0.1:{PORT}"


def _start() -> subprocess.Popen:
    return subprocess.Popen(
        ["uv", "run", "adhar-ai", "mcp", "--domain", "provision", f"--listen=:{PORT}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_up(tries: int = 60) -> bool:
    for _ in range(tries):
        try:
            if httpx.get(f"{BASE}/healthz", timeout=1).status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


@pytest.mark.slow
async def test_a_restarted_server_is_detected_and_reconnected() -> None:
    server = _start()
    assert _wait_up(), "the MCP server under test never started"
    toolbox = MCPToolbox({"provision": BASE})
    try:
        await toolbox.connect()
        assert toolbox.unhealthy == []
        assert "error" not in await toolbox.call("list_xrs", {"kind": "CompositeCluster"})

        server.terminate()
        server.wait()
        time.sleep(1)

        # The call must RETURN an honest error, not raise. The failure arrives
        # as CancelledError from the SDK's task group, which `except Exception`
        # does not catch — it used to take the whole agent run with it.
        out = await toolbox.call("list_xrs", {"kind": "CompositeCluster"})
        assert "error" in out
        assert "provision" in out["error"]
        assert out["domain"] == "provision"
        assert toolbox.unhealthy == ["provision"], "health must stop claiming it works"

        server = _start()
        assert _wait_up(), "the MCP server under test never restarted"
        assert await toolbox.reconnect("provision") is True
        assert toolbox.unhealthy == []
        assert "error" not in await toolbox.call("list_xrs", {"kind": "CompositeCluster"})
    finally:
        await toolbox.aclose()
        server.terminate()
        server.wait()


@pytest.mark.slow
async def test_one_dead_domain_does_not_cost_the_others() -> None:
    server = _start()
    assert _wait_up()
    toolbox = MCPToolbox({"provision": BASE, "dead": "http://127.0.0.1:18496"})
    try:
        await toolbox.connect()
        assert toolbox.unhealthy == ["dead"]
        assert len(toolbox.tools) == 3, "the healthy domain keeps all of its tools"
        assert "error" not in await toolbox.call("list_xrs", {"kind": "CompositeCluster"})
        # And the reason is one a human can act on.
        assert "cancel scope" not in toolbox.errors["dead"].lower()
    finally:
        await toolbox.aclose()
        server.terminate()
        server.wait()


def test_a_connection_failure_is_described_usefully() -> None:
    """anyio buries the real cause under a CancelledError from the torn-down
    scope. Reporting the wrapper puts "Cancelled via cancel scope 0x..." in
    /healthz, which tells an operator nothing."""
    import asyncio

    buried = ExceptionGroup("tg", [ConnectionRefusedError("All connection attempts failed")])
    assert "All connection attempts failed" in _describe(buried)
    assert "connection failed" in _describe(asyncio.CancelledError())
