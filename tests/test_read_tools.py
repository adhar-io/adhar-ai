"""Read tools against mocked backends — asserting they call the REAL APIs."""

from __future__ import annotations

import httpx
import pytest
import respx

from adhar_ai.clients.errors import BackendNotConfigured
from adhar_ai.config import MCPConfig, TelemetryConfig

from .conftest import ARGOCD_URL, GITEA_URL, LOKI_URL, OPENCOST_URL, PROM_URL, call, server_for

pytestmark = pytest.mark.usefixtures("fake_kube")


# ------------------------------------------------------------------ cluster --


async def test_list_pods_and_filtering(mcp_cfg, fake_kube):
    server = server_for("cluster", mcp_cfg)
    everything = await call(server, "list_pods", {})
    assert everything["count"] == 2

    scoped = await call(server, "list_pods", {"namespace": "demo"})
    assert [p["name"] for p in scoped["pods"]] == ["broken-xyz"]

    selected = await call(
        server, "list_pods", {"label_selector": "app.kubernetes.io/name=adhar-ai-runtime"}
    )
    assert selected["count"] == 1


async def test_logs_and_events_come_from_the_kube_api(mcp_cfg):
    server = server_for("cluster", mcp_cfg)
    logs = await call(server, "logs", {"namespace": "demo", "name": "broken-xyz"})
    assert logs["lines"][-1] == "panic: boom"

    events = await call(server, "get_events", {"namespace": "demo"})
    assert events["events"][0]["reason"] == "BackOff"


async def test_resource_health_reports_degraded_workloads(mcp_cfg):
    server = server_for("cluster", mcp_cfg)
    health = await call(server, "resource_health", {})
    assert health["healthy"] is False
    assert health["workloads_degraded"][0]["name"] == "broken"
    assert any(p["name"] == "broken-xyz" for p in health["pods_unhealthy"])


# ------------------------------------------------------------------- gitops --

APP = {
    "metadata": {"name": "vault", "namespace": "adhar-system"},
    "spec": {
        "project": "default",
        "source": {"repoURL": "http://gitea/adhar/packages", "path": "security/vault/manifests"},
        "destination": {"namespace": "adhar-system"},
    },
    "status": {
        "sync": {"status": "OutOfSync", "revision": "abc123"},
        "health": {"status": "Degraded", "message": "pod pending"},
    },
}


@respx.mock
async def test_app_status_uses_the_argocd_rest_api(mcp_cfg):
    respx.post(f"{ARGOCD_URL}/api/v1/session").mock(
        return_value=httpx.Response(200, json={"token": "jwt-token"})
    )
    route = respx.get(f"{ARGOCD_URL}/api/v1/applications/vault").mock(
        return_value=httpx.Response(200, json=APP)
    )
    out = await call(server_for("gitops", mcp_cfg), "app_status", {"app": "vault"})

    assert out["sync_status"] == "OutOfSync"
    assert out["health_status"] == "Degraded"
    assert out["path"] == "security/vault/manifests"
    assert route.calls[0].request.headers["authorization"] == "Bearer jwt-token"


@respx.mock
async def test_sync_status_filters_unhealthy(mcp_cfg):
    respx.post(f"{ARGOCD_URL}/api/v1/session").mock(
        return_value=httpx.Response(200, json={"token": "t"})
    )
    healthy = {
        "metadata": {"name": "gitea"},
        "spec": {"source": {}},
        "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}},
    }
    respx.get(f"{ARGOCD_URL}/api/v1/applications").mock(
        return_value=httpx.Response(200, json={"items": [APP, healthy]})
    )
    out = await call(server_for("gitops", mcp_cfg), "sync_status", {"only_unhealthy": True})
    assert out["count"] == 1
    assert out["out_of_sync"] == 1
    assert out["applications"][0]["name"] == "vault"


async def test_gitops_falls_back_to_the_kubernetes_api_when_argocd_is_unkeyed(mcp_cfg, fake_kube):
    """Read tools must still work before the ArgoCD credential is wired."""
    fake_kube.custom["applications"] = [APP]
    cfg = MCPConfig(domain="gitops", gitea=mcp_cfg.gitea)  # no ArgoCD config
    from adhar_ai.mcp.server import build_server

    out = await call(build_server(cfg), "app_status", {"app": "vault"})
    assert out["sync_status"] == "OutOfSync"


async def test_app_diff_says_so_when_argocd_is_unavailable(mcp_cfg):
    cfg = MCPConfig(domain="gitops", gitea=mcp_cfg.gitea)
    from adhar_ai.mcp.server import build_server

    out = await call(build_server(cfg), "app_diff", {"app": "vault"})
    assert out["diff_available"] is False
    assert "ARGOCD_URL" in out["reason"]


# ------------------------------------------------------------ observability --


@respx.mock
async def test_promql_instant_and_range(mcp_cfg):
    payload = {
        "status": "success",
        "data": {"result": [{"metric": {"pod": "p"}, "value": [1, "3"]}]},
    }
    instant = respx.get(f"{PROM_URL}/api/v1/query").mock(
        return_value=httpx.Response(200, json=payload)
    )
    ranged = respx.get(f"{PROM_URL}/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=payload)
    )
    server = server_for("observability", mcp_cfg)

    out = await call(server, "promql", {"query": "up"})
    assert out["series"][0]["metric"]["pod"] == "p"
    assert instant.calls[0].request.url.params["query"] == "up"

    await call(server, "promql", {"query": "up", "range_minutes": 30})
    assert ranged.called
    assert ranged.calls[0].request.url.params["step"] == "60s"


@respx.mock
async def test_logql_hits_loki_query_range(mcp_cfg):
    route = respx.get(f"{LOKI_URL}/loki/api/v1/query_range").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "result": [{"stream": {"namespace": "demo"}, "values": [["1", "oops"]]}]
                }
            },
        )
    )
    out = await call(
        server_for("observability", mcp_cfg), "logql", {"query": '{namespace="demo"}'}
    )
    assert out["stream_count"] == 1
    assert out["streams"][0]["entries"][0][1] == "oops"
    assert route.calls[0].request.url.params["direction"] == "backward"


@respx.mock
async def test_slo_burn_computes_burn_rate_per_window(mcp_cfg):
    respx.get(f"{PROM_URL}/api/v1/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"result": [{"metric": {}, "value": [1, "0.98"]}]},
            },
        )
    )
    out = await call(
        server_for("observability", mcp_cfg),
        "slo_burn",
        {"slo_metric": "ratio[WINDOW]", "objective": 0.99, "windows": "5m,1h"},
    )
    assert out["error_budget"] == pytest.approx(0.01)
    assert [w["window"] for w in out["windows"]] == ["5m", "1h"]
    # 2% bad against a 1% budget => burning at 2x.
    assert out["windows"][0]["burn_rate"] == pytest.approx(2.0)
    assert out["windows"][0]["query"] == "ratio[5m]"


@respx.mock
async def test_correlate_reports_unavailable_backends_honestly(mcp_cfg):
    """An unconfigured backend must surface as 'unavailable', never as data."""
    respx.get(f"{PROM_URL}/api/v1/query").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": {"result": []}})
    )
    respx.get(f"{PROM_URL}/api/v1/alerts").mock(
        return_value=httpx.Response(200, json={"data": {"alerts": []}})
    )
    cfg = MCPConfig(
        domain="observability",
        telemetry=TelemetryConfig(prometheus_url=PROM_URL),  # Loki deliberately unset
    )
    from adhar_ai.mcp.server import build_server

    out = await call(build_server(cfg), "correlate", {"namespace": "demo", "workload": "broken"})
    assert "unavailable" in out["logs"]
    assert "Loki is not configured" in out["logs"]["unavailable"]


async def test_unconfigured_backend_raises_rather_than_inventing(mcp_cfg):
    from adhar_ai.clients.telemetry import TelemetryClient

    client = TelemetryClient(TelemetryConfig())
    with pytest.raises(BackendNotConfigured, match="Prometheus"):
        await client.promql("up")


# ----------------------------------------------------------------- security --

REPORT = {
    "metadata": {"name": "cpol-require-labels", "namespace": "demo"},
    "results": [
        {
            "policy": "require-labels",
            "rule": "check-team",
            "result": "fail",
            "severity": "medium",
            "message": "label missing",
            "resources": [{"kind": "Deployment", "name": "broken"}],
        },
        {"policy": "require-labels", "rule": "check-team", "result": "pass", "resources": [{}]},
    ],
}


async def test_findings_reads_policyreports(mcp_cfg, fake_kube):
    fake_kube.custom["policyreports"] = [REPORT]
    out = await call(server_for("security", mcp_cfg), "findings", {"namespace": "demo"})
    assert out["count"] == 1
    assert out["by_policy"] == {"require-labels": 1}
    assert out["findings"][0]["resource"] == "Deployment/broken"


async def test_posture_aggregates_by_result(mcp_cfg, fake_kube):
    fake_kube.custom["policyreports"] = [REPORT]
    fake_kube.custom["clusterpolicyreports"] = []
    out = await call(server_for("security", mcp_cfg), "posture", {})
    assert out["by_result"] == {"fail": 1, "pass": 1}
    assert out["by_severity"] == {"medium": 1}


async def test_policy_explain_counts_live_violations(mcp_cfg, fake_kube):
    fake_kube.custom["policyreports"] = [REPORT]
    fake_kube.custom["clusterpolicyreports"] = []
    fake_kube.custom["clusterpolicies"] = [
        {
            "metadata": {
                "name": "require-labels",
                "annotations": {"policies.kyverno.io/title": "Require Labels"},
            },
            "spec": {
                "validationFailureAction": "Audit",
                "rules": [{"name": "check-team", "validate": {"message": "label missing"}}],
            },
        }
    ]
    out = await call(
        server_for("security", mcp_cfg), "policy_explain", {"policy": "require-labels"}
    )
    assert out["title"] == "Require Labels"
    assert out["validation_failure_action"] == "Audit"
    assert out["current_violations"] == 1


# --------------------------------------------------------------------- cost --


@respx.mock
async def test_cost_by_aggregates_opencost_allocation(mcp_cfg):
    route = respx.get(f"{OPENCOST_URL}/allocation/compute").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "demo": {"totalCost": 12.5, "cpuCost": 8.0, "ramCost": 4.5},
                        "adhar-system": {"totalCost": 40.0, "cpuCost": 30.0, "ramCost": 10.0},
                    }
                ]
            },
        )
    )
    out = await call(server_for("cost", mcp_cfg), "cost_by", {"window": "7d"})
    assert out["total_cost"] == pytest.approx(52.5)
    assert out["rows"][0]["name"] == "adhar-system"  # ranked by spend
    assert route.calls[0].request.url.params["aggregate"] == "namespace"


@respx.mock
async def test_budget_status_projects_monthly_spend(mcp_cfg):
    respx.get(f"{OPENCOST_URL}/allocation/compute").mock(
        return_value=httpx.Response(200, json={"data": [{"demo": {"totalCost": 30.0}}]})
    )
    out = await call(
        server_for("cost", mcp_cfg), "budget_status", {"monthly_budget": 20.0, "window": "30d"}
    )
    assert out["projected_monthly"] == pytest.approx(30.0)
    assert out["over_budget"] is True
    assert out["pct_of_budget"] == pytest.approx(150.0)


# ------------------------------------------------------------------ catalog --


async def test_template_params_lists_and_describes_golden_paths(mcp_cfg):
    server = server_for("catalog", mcp_cfg)
    listing = await call(server, "template_params", {})
    names = {g["name"] for g in listing["golden_paths"]}
    assert {"go-service", "python-service", "cnpg-database"} <= names

    one = await call(server, "template_params", {"golden_path": "cnpg-database"})
    assert "database" in one["required"]


@respx.mock
async def test_search_packages_joins_gitea_and_argocd(mcp_cfg, fake_kube):
    api = f"{GITEA_URL}/api/v1/repos/adhar/packages/contents"
    respx.get(f"{api}/").mock(
        return_value=httpx.Response(200, json=[{"name": "security", "type": "dir"}])
    )
    respx.get(f"{api}/security").mock(
        return_value=httpx.Response(
            200, json=[{"name": "vault", "type": "dir"}, {"name": "README.md", "type": "file"}]
        )
    )
    respx.post(f"{ARGOCD_URL}/api/v1/session").mock(
        return_value=httpx.Response(200, json={"token": "t"})
    )
    respx.get(f"{ARGOCD_URL}/api/v1/applications").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "metadata": {"name": "vault"},
                        "spec": {"source": {}},
                        "status": {
                            "sync": {"status": "Synced"},
                            "health": {"status": "Healthy"},
                        },
                    }
                ]
            },
        )
    )
    out = await call(server_for("catalog", mcp_cfg), "search_packages", {})
    assert out["count"] == 1
    assert out["packages"][0] == {
        "name": "vault",
        "category": "security",
        "deployed": True,
        "sync_status": "Synced",
        "health_status": "Healthy",
    }
