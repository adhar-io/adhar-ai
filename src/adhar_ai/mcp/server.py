"""Builds the MCP app for one domain.

One image, seven Deployments: the domain comes from `--domain`, or the
`MCP_DOMAIN` / `ADHAR_AI_MCP_DOMAIN` env the platform manifests set.

Transport is streamable HTTP at `/mcp`, with `/healthz` on the same port for
the readiness probe the manifests declare (`httpGet /healthz` on port 8080).
"""

from __future__ import annotations

import importlib
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from ..config import DOMAINS, WRITE_DOMAINS, MCPConfig

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

    @mcp.custom_route("/readyz", methods=["GET"])
    async def readyz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "domain": cfg.domain})

    return mcp


def build_app(cfg: MCPConfig) -> Any:
    """Starlette ASGI app: `/mcp` (streamable HTTP) + `/healthz` + `/readyz`."""
    return build_server(cfg).streamable_http_app()


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
