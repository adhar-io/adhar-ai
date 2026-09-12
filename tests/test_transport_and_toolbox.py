"""The two defects that made the federated MCP surface unusable in a cluster.

Both were invisible to the existing suite because it never exercised the
transport or the client — tools were driven in-process and the toolbox was
faked. These tests go through the real ASGI app and the real SDK result model,
which is where both bugs lived.
"""

from __future__ import annotations

import json

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from adhar_ai.config import DOMAINS, MCPConfig
from adhar_ai.mcp.server import build_app, transport_security
from adhar_ai.runtime.toolbox import _is_error, _structured_of, _unwrap

from .conftest import server_for

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

#: The Host headers a request actually arrives with in the platform.
#: agentgateway federates these servers through a Service `backendRef`
#: (ai/agentgateway/manifests/mcp-federation.yaml), so the Host is the Service
#: DNS name or the Pod IP behind it — never localhost.
IN_CLUSTER_HOSTS = [
    "adhar-ai-mcp-cluster.adhar-system.svc.cluster.local:8080",
    "adhar-ai-mcp-cluster.adhar-system.svc:8080",
    "adhar-ai-mcp-cluster:8080",
    "10.244.1.7:8080",
    "mcp.adhar.localtest.me",
]


async def _initialize(app, host: str) -> httpx.Response:
    """POST an MCP `initialize` through the real ASGI stack with a given Host."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url=f"http://{host}"
    ) as client:
        return await client.post(
            "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Host": host}
        )


# --------------------------------------------------------------------------- #
# Host header / DNS-rebinding guard
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("host", IN_CLUSTER_HOSTS)
async def test_in_cluster_host_headers_are_accepted(host: str) -> None:
    """The regression that broke every federated tool call.

    The SDK's `streamable_http_app()` defaults to `host="127.0.0.1"`, which
    installs a DNS-rebinding guard allowing only localhost. In a Pod that
    answers `421 Invalid Host header` to agentgateway, to every external agent,
    and to the runtime's own toolbox — the entire MCP surface, with a failure
    mode that looks like a networking problem rather than a config default.
    """
    async with _lifespan(build_app(MCPConfig(domain="cluster"))) as app:
        response = await _initialize(app, host)
    assert response.status_code == 200, (
        f"Host {host!r} was rejected with {response.status_code}: {response.text[:200]}"
    )


async def test_allow_list_mode_rejects_an_unlisted_host() -> None:
    """Setting an explicit allow-list switches the guard back on."""
    cfg = MCPConfig(domain="cluster", allowed_hosts=("adhar-ai-mcp-cluster:8080",))
    assert cfg.dns_rebinding_protection is True
    async with _lifespan(build_app(cfg)) as app:
        allowed = await _initialize(app, "adhar-ai-mcp-cluster:8080")
        refused = await _initialize(app, "evil.example.com")
    assert allowed.status_code == 200
    assert refused.status_code == 421


def test_default_config_disables_the_guard_and_says_so() -> None:
    settings = transport_security(MCPConfig(domain="cluster"))
    assert settings.enable_dns_rebinding_protection is False


def test_allow_list_mirrors_hosts_into_origins() -> None:
    """A browser-based MCP client sends Origin, not just Host."""
    settings = transport_security(
        MCPConfig(domain="cluster", allowed_hosts=("mcp.adhar.localtest.me",))
    )
    assert settings.allowed_hosts == ["mcp.adhar.localtest.me"]
    assert "https://mcp.adhar.localtest.me" in settings.allowed_origins


@pytest.mark.parametrize("domain", DOMAINS)
async def test_every_domain_answers_the_health_probe(domain: str) -> None:
    """The readinessProbe in mcp-servers.yaml, against the real app."""
    async with _lifespan(build_app(MCPConfig(domain=domain))) as app:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://probe") as c:
            response = await c.get("/healthz")
    assert response.status_code == 200
    assert response.json()["domain"] == domain


# --------------------------------------------------------------------------- #
# CallToolResult unwrapping
# --------------------------------------------------------------------------- #


def test_a_failed_tool_call_is_reported_as_an_error() -> None:
    """The silent-failure bug.

    `_unwrap` read `result.isError`. The SDK model's field is `is_error`, so the
    getattr always missed and a FAILED call was handed to the model as ordinary
    text — then audited `decision="ok"`, because the audit decides by looking
    for an "error" key that this function never wrote.
    """
    result = CallToolResult(
        content=[TextContent(type="text", text="BackendNotConfigured: no ArgoCD URL")],
        is_error=True,
    )
    assert _is_error(result) is True
    assert _unwrap(result) == {"error": "BackendNotConfigured: no ArgoCD URL"}


def test_error_detection_accepts_the_wire_spelling_too() -> None:
    """A raw JSON-RPC dict uses `isError`; both spellings must work."""
    assert _is_error({"isError": True, "content": []}) is True
    assert _is_error({"is_error": True, "content": []}) is True
    assert _is_error({"content": []}) is False


def test_structured_content_is_read_from_the_sdk_model() -> None:
    result = CallToolResult(
        content=[TextContent(type="text", text="ignored")],
        structured_content={"result": {"healthy": 3}},
    )
    assert _structured_of(result) == {"result": {"healthy": 3}}
    assert _unwrap(result) == {"healthy": 3}


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            CallToolResult(
                content=[TextContent(type="text", text="x")],
                structured_content={"result": [1, 2]},
            ),
            {"result": [1, 2]},
        ),
        (
            CallToolResult(
                content=[TextContent(type="text", text="x")],
                structured_content={"a": 1, "b": 2},
            ),
            {"a": 1, "b": 2},
        ),
        (
            CallToolResult(content=[TextContent(type="text", text=json.dumps({"a": 1}))]),
            {"a": 1},
        ),
        (CallToolResult(content=[TextContent(type="text", text="plain")]), {"text": "plain"}),
    ],
)
def test_every_result_shape_unwraps_to_a_dict(result, expected) -> None:
    """`loop._invoke` does `"error" in output`, so a list would silently never
    look like an error."""
    unwrapped = _unwrap(result)
    assert isinstance(unwrapped, dict)
    assert unwrapped == expected


# --------------------------------------------------------------------------- #


class _lifespan:
    """Run an ASGI app's lifespan so the streamable-HTTP task group exists."""

    def __init__(self, app) -> None:
        self.app = app

    async def __aenter__(self):
        from contextlib import AsyncExitStack

        self._stack = AsyncExitStack()
        router = self.app.router
        self._ctx = router.lifespan_context(self.app)
        await self._stack.enter_async_context(self._ctx)
        return self.app

    async def __aexit__(self, *exc) -> None:
        await self._stack.aclose()


# --------------------------------------------------------------------------- #
# Anticipated failures keep their message
# --------------------------------------------------------------------------- #


async def test_an_unconfigured_backend_tells_the_model_which_one() -> None:
    """The MCP SDK splits tool failures in two: a `ToolError`'s message is
    returned to the client, and any other exception's is withheld — the model
    sees only "Error executing tool <name>". `BackendNotConfigured` was in the
    second bucket, so the agent could not tell "Prometheus is not configured
    here" from "the tool crashed", and the system prompt's instruction to say so
    plainly had nothing to say it from.
    """
    from adhar_ai.config import TelemetryConfig

    cfg = MCPConfig(domain="observability", telemetry=TelemetryConfig())
    with pytest.raises(ToolError) as raised:
        await server_for("observability", cfg).call_tool("promql", {"query": "up"})

    message = str(raised.value)
    assert "Prometheus is not configured" in message
    assert "PROMETHEUS_URL" in message  # says which knob fixes it


async def test_a_read_only_server_says_why_it_refused_a_write() -> None:
    from adhar_ai.config import GiteaConfig

    cfg = MCPConfig(domain="gitops", gitea=GiteaConfig(write_enabled=False))
    with pytest.raises(ToolError) as raised:
        await server_for("gitops", cfg).call_tool(
            "propose_change",
            {
                "repo": "packages",
                "title": "t",
                "why": "w",
                "changes": [{"path": "packages/x.yaml", "content": "a: b"}],
            },
        )
    assert "read-only" in str(raised.value)


async def test_an_unexpected_crash_still_withholds_its_detail() -> None:
    """The masking exists for a reason: an unanticipated exception may carry a
    connection string or a token in its text. Only the anticipated list is
    re-raised as a `ToolError`."""
    from adhar_ai.mcp.common.audit import _expected

    secret = RuntimeError("psql://user:hunter2@db/adhar failed")
    assert not isinstance(_expected(secret), ToolError)
