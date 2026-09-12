"""The safety contract: what tools exist, and what they are allowed to do.

This is the test the design doc (§9, §12) singles out — if it ever fails, the
agent has gained a write path that does not go through a pull request.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from adhar_ai.config import DOMAINS, WRITE_DOMAINS, MCPConfig
from adhar_ai.mcp import catalog, gitops, provision, security
from adhar_ai.mcp.common import pr as pr_module
from adhar_ai.mcp.server import build_app, build_server, tool_access

EXPECTED = {
    "cluster": {"list_pods", "describe", "get_events", "logs", "resource_health"},
    "gitops": {"app_status", "app_diff", "sync_status", "propose_change"},
    "provision": {"list_xrs", "xr_status", "propose_xr"},
    "observability": {"promql", "logql", "traceql", "slo_burn", "correlate"},
    "security": {"findings", "policy_explain", "posture", "propose_exception"},
    "cost": {"cost_by", "budget_status", "showback"},
    "catalog": {"search_packages", "template_params", "scaffold"},
}

EXPECTED_WRITES = {"propose_change", "propose_xr", "propose_exception", "scaffold"}

#: Tool names that must never exist. The agent's safety rests on their absence.
FORBIDDEN = {
    "kubectl_apply",
    "apply",
    "argo_sync",
    "app_sync",
    "sync",
    "helm_install",
    "helm_upgrade",
    "delete",
    "delete_resource",
    "patch",
    "scale",
    "rollback",
    "exec",
    "create_cluster",
    "terminate_instance",
}


def _tools(domain: str, mcp_cfg: MCPConfig):
    cfg = MCPConfig(
        domain=domain, gitea=mcp_cfg.gitea, argocd=mcp_cfg.argocd, telemetry=mcp_cfg.telemetry
    )
    return asyncio.run(build_server(cfg).list_tools())


@pytest.mark.parametrize("domain", DOMAINS)
def test_domain_exposes_exactly_the_designed_tools(domain, mcp_cfg):
    names = {t.name for t in _tools(domain, mcp_cfg)}
    assert names == EXPECTED[domain]


@pytest.mark.parametrize("domain", DOMAINS)
def test_no_mutating_tool_exists_anywhere(domain, mcp_cfg):
    names = {t.name for t in _tools(domain, mcp_cfg)}
    assert not names & FORBIDDEN


@pytest.mark.parametrize("domain", DOMAINS)
def test_access_tags_match_the_manifests(domain, mcp_cfg):
    tools = _tools(domain, mcp_cfg)
    writes = {t.name for t in tools if tool_access(t) == "write"}
    assert writes == (EXPECTED[domain] & EXPECTED_WRITES)
    # GITEA_WRITE_ENABLED in mcp-servers.yaml must agree with reality.
    assert bool(writes) is (domain in WRITE_DOMAINS)


@pytest.mark.parametrize("domain", DOMAINS)
def test_every_tool_is_documented(domain, mcp_cfg):
    for tool in _tools(domain, mcp_cfg):
        assert (tool.description or "").strip(), f"{tool.name} has no description"


def test_every_write_tool_routes_through_open_pr():
    """Source-level assertion: each write tool's body calls `open_pr` and
    nothing else that could mutate a cluster."""
    write_fns = {
        "propose_change": gitops,
        "propose_xr": provision,
        "propose_exception": security,
        "scaffold": catalog,
    }
    for name, module in write_fns.items():
        source = inspect.getsource(module.register)
        # Locate the tool body within register()'s source.
        start = source.index(f"async def {name}(")
        end = source.find("\n    @", start)
        body = source[start : end if end != -1 else len(source)]
        assert "open_pr(" in body, f"{name} does not call open_pr"
        for forbidden in ("apply", "kubectl", "create_namespaced", "patch_", "delete_"):
            assert forbidden not in body, f"{name} contains a mutating call: {forbidden}"


def test_open_pr_only_touches_gitea():
    """The single write path must not import a Kubernetes or cloud client."""
    source = inspect.getsource(pr_module)
    for forbidden in ("kubernetes", "boto3", "azure", "google.cloud", "kubectl"):
        assert forbidden not in source


# --------------------------------------------- the federated transport contract --
#
# ai/agentgateway federates the seven servers with
#   static: {backendRef: adhar-ai-mcp-<domain>, port: 8080, path: /mcp,
#            protocol: StreamableHTTP}
# (ADR-0025 item 5). If the app ever stopped mounting MCP at `/mcp`, every tool
# in the platform would vanish from the federated endpoint with no local failure
# to show for it — so the path is pinned here.


@pytest.mark.parametrize("domain", DOMAINS)
def test_every_domain_serves_streamable_http_at_slash_mcp(domain):
    app = build_app(MCPConfig(domain=domain))
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/mcp" in paths
    # The readiness probe in mcp-servers.yaml hits /healthz on the SAME port.
    assert {"/healthz", "/readyz"} <= paths
