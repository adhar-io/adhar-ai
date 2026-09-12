"""Shared fixtures.

No test reaches the network: HTTP is intercepted with respx, and the Kubernetes
client is a fake implementing the `KubeClient` protocol.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from adhar_ai.clients.kube import set_kube_client
from adhar_ai.config import ArgoCDConfig, GiteaConfig, MCPConfig, TelemetryConfig
from adhar_ai.mcp.server import build_server

GITEA_URL = "http://gitea-http.adhar-system.svc.cluster.local:3000"
ARGOCD_URL = "http://argo-cd-argocd-server.adhar-system.svc.cluster.local:80"
PROM_URL = "http://kube-prometheus-stack-prometheus.adhar-system.svc.cluster.local:9090"
LOKI_URL = "http://loki.adhar-system.svc.cluster.local:3100"
TEMPO_URL = "http://tempo.adhar-system.svc.cluster.local:3100"
OPENCOST_URL = "http://opencost.adhar-system.svc.cluster.local:9003"


class FakeKubeClient:
    """In-memory stand-in for the read-only Kubernetes client."""

    def __init__(self) -> None:
        self.pods: list[dict[str, Any]] = [
            {
                "name": "adhar-ai-runtime-abc",
                "namespace": "adhar-system",
                "phase": "Running",
                "restarts": 0,
                "ready": "1/1",
                "containers": [],
                "labels": {"app.kubernetes.io/name": "adhar-ai-runtime"},
            },
            {
                "name": "broken-xyz",
                "namespace": "demo",
                "phase": "CrashLoopBackOff",
                "restarts": 7,
                "ready": "0/1",
                "containers": [{"name": "app", "ready": False, "reason": "CrashLoopBackOff"}],
                "labels": {},
            },
        ]
        self.events = [
            {
                "type": "Warning",
                "reason": "BackOff",
                "message": "Back-off restarting failed container",
                "object": "Pod/broken-xyz",
                "namespace": "demo",
                "count": 12,
                "last_timestamp": None,
            }
        ]
        self.workloads = [
            {
                "name": "broken",
                "namespace": "demo",
                "replicas_desired": 2,
                "replicas_ready": 0,
                "replicas_available": 0,
                "updated": 2,
                "conditions": [],
            }
        ]
        self.custom: dict[str, list[dict[str, Any]]] = {}

    def list_pods(self, namespace=None, label_selector=None):
        rows = [p for p in self.pods if namespace is None or p["namespace"] == namespace]
        if label_selector:
            key, _, value = label_selector.partition("=")
            rows = [p for p in rows if p.get("labels", {}).get(key) == value]
        return rows

    def get_pod(self, namespace, name):
        for pod in self.pods:
            if pod["namespace"] == namespace and pod["name"] == name:
                return pod
        raise KeyError(f"{namespace}/{name}")

    def pod_logs(self, namespace, name, container=None, tail_lines=200):
        return "line one\nline two\npanic: boom"

    def list_events(self, namespace=None, field_selector=None):
        return [e for e in self.events if namespace is None or e["namespace"] == namespace]

    def list_workloads(self, namespace=None):
        return [w for w in self.workloads if namespace is None or w["namespace"] == namespace]

    def list_custom(self, group, version, plural, namespace=None):
        return list(self.custom.get(plural, []))

    def get_custom(self, group, version, plural, name, namespace=None):
        for item in self.custom.get(plural, []):
            if (item.get("metadata") or {}).get("name") == name:
                return item
        raise KeyError(name)


@pytest.fixture
def fake_kube():
    client = FakeKubeClient()
    set_kube_client(client)
    yield client
    set_kube_client(None)


@pytest.fixture
def gitea_cfg() -> GiteaConfig:
    return GiteaConfig(
        api_url=GITEA_URL,
        org="adhar",
        bot_user="adhar-ai-bot",
        bot_token="test-token",
        write_enabled=True,
        write_repos=("packages", "environments"),
    )


@pytest.fixture
def readonly_gitea_cfg() -> GiteaConfig:
    return GiteaConfig(api_url=GITEA_URL, org="adhar", write_enabled=False)


@pytest.fixture
def mcp_cfg(gitea_cfg: GiteaConfig) -> MCPConfig:
    return MCPConfig(
        domain="gitops",
        gitea=gitea_cfg,
        argocd=ArgoCDConfig(url=ARGOCD_URL, username="admin", password="hunter2"),
        telemetry=TelemetryConfig(
            prometheus_url=PROM_URL,
            loki_url=LOKI_URL,
            tempo_url=TEMPO_URL,
            opencost_url=OPENCOST_URL,
        ),
    )


def server_for(domain: str, cfg: MCPConfig):
    return build_server(
        MCPConfig(
            domain=domain,
            gitea=cfg.gitea,
            argocd=cfg.argocd,
            telemetry=cfg.telemetry,
        )
    )


async def call(server, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Invoke a tool through the real MCP server and decode its payload."""
    result = await server.call_tool(name, args)
    if result.is_error:
        raise AssertionError(f"tool {name} errored: {_text(result)}")
    if result.structured_content:
        sc = result.structured_content
        return sc.get("result", sc) if len(sc) == 1 and "result" in sc else sc
    return json.loads(_text(result))


async def call_raw(server, name: str, args: dict[str, Any]):
    return await server.call_tool(name, args)


def _text(result) -> str:
    return "\n".join(b.text for b in (result.content or []) if getattr(b, "type", "") == "text")
