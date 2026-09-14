"""Builds the MCP app for one domain.

One image, seven Deployments: the domain comes from `--domain`, or the
`MCP_DOMAIN` / `ADHAR_AI_MCP_DOMAIN` env the platform manifests set.

Transport is streamable HTTP at `/mcp`, with `/healthz` on the same port for
the readiness probe the manifests declare (`httpGet /healthz` on port 8080).
"""

from __future__ import annotations

import importlib
from typing import Any

from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..config import DOMAINS, WRITE_DOMAINS, MCPConfig
from ..observability import METRICS_CONTENT_TYPE, metrics

try:  # mcp SDK >= 2.0 renamed FastMCP to MCPServer; the decorator API is the same.
    from mcp.server.mcpserver import MCPServer as FastMCP
except ModuleNotFoundError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import FastMCP  # type: ignore[attr-defined,no-redef]

SERVER_NAMES = {domain: f"adhar-{domain}" for domain in DOMAINS}


def build_server(cfg: MCPConfig) -> Any:
    """Create the MCPServer (FastMCP) for ``cfg.domain`` with its tools mounted."""
    mcp = FastMCP(
        name=SERVER_NAMES[cfg.domain],
        instructions=(
            f"Adhar AI {cfg.domain} tools for the Adhar internal developer platform.\n"
            "Read tools query live platform state and are RBAC-scoped. Write tools open a "
            "Gitea pull request and NOTHING else — there is no apply, sync, or cloud-mutation "
            "tool here, by design (ADR-0024). If a backend is unconfigured the tool says so; "
            "never present an unavailable backend's data as if it were retrieved."
        ),
    )

    module = importlib.import_module(f".{cfg.domain}", package=__package__)
    module.register(mcp, cfg)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "domain": cfg.domain,
                "server": SERVER_NAMES[cfg.domain],
                "write_enabled": cfg.gitea.write_enabled,
                "write_path": "gitea-pull-request-only",
            }
        )

    @mcp.custom_route("/metrics", methods=["GET"])
    async def prometheus_metrics(_request: Request) -> Response:
        """Each domain server exports its own tool-call counters, so a slow or
        failing backend is attributable to the domain that fronts it rather
        than averaged away across all seven."""
        return Response(
            content=metrics.render_metrics(), media_type=METRICS_CONTENT_TYPE
        )

    @mcp.custom_route("/readyz", methods=["GET"])
    async def readyz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "domain": cfg.domain})

    return mcp


def transport_security(cfg: MCPConfig) -> TransportSecuritySettings:
    """DNS-rebinding settings for the streamable-HTTP transport.

    The SDK's default allow-list is localhost-only. In a Pod that rejects every
    real caller — agentgateway reaches these servers through a Service
    `backendRef`, so the Host header is the Service DNS name or a Pod IP — and
    the whole federated tool surface answers `421 Invalid Host header`. See
    `MCPConfig.allowed_hosts` for why `*` (guard off) is the right default here
    and what the actual boundary is.
    """
    if not cfg.dns_rebinding_protection:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    hosts = list(cfg.allowed_hosts)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        # Origin is a browser-only header; mirroring the host list keeps a
        # browser-based MCP client working against the same allow-list.
        allowed_origins=[f"http://{h}" for h in hosts] + [f"https://{h}" for h in hosts],
    )


def build_app(cfg: MCPConfig) -> Any:
    """Starlette ASGI app: `/mcp` (streamable HTTP) + `/healthz` + `/readyz`."""
    return build_server(cfg).streamable_http_app(transport_security=transport_security(cfg))


def expected_write_enabled(domain: str) -> bool:
    """What the platform manifests set GITEA_WRITE_ENABLED to for a domain."""
    return domain in WRITE_DOMAINS


def tool_access(tool: Any) -> str:
    """Read the `adhar/access` tag off a listed tool (meta, then hint)."""
    meta = getattr(tool, "meta", None) or {}
    if value := meta.get("adhar/access"):
        return str(value)
    annotations = getattr(tool, "annotations", None)
    if annotations is not None and getattr(annotations, "read_only_hint", None) is True:
        return "read"
    return "write"
