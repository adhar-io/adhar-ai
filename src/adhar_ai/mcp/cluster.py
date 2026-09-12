"""mcp-cluster — read-only Kubernetes investigation tools.

RBAC ceiling is the `adhar-ai-readonly` ClusterRole (get/list/watch). No write
tool exists on this server.
"""

from __future__ import annotations

from typing import Any

from ..clients.kube import get_kube_client
from ..config import MCPConfig
from .common.tools import access_tools

DOMAIN = "cluster"


def register(mcp: Any, cfg: MCPConfig) -> None:
    read, write = access_tools(mcp, DOMAIN)

    @read
    async def list_pods(
        namespace: str | None = None, label_selector: str | None = None
    ) -> dict[str, Any]:
        """List pods with phase, readiness, restart counts and container states.

        namespace: restrict to one namespace (omit for cluster-wide).
        label_selector: standard Kubernetes selector, e.g. "app.kubernetes.io/part-of=adhar-ai".
        """
        pods = get_kube_client().list_pods(namespace, label_selector)
        return {"count": len(pods), "pods": pods}

    @read
    async def describe(namespace: str, name: str) -> dict[str, Any]:
        """Return the full (noise-stripped) spec and status of one pod."""
        return get_kube_client().get_pod(namespace, name)

    @read
    async def get_events(
        namespace: str | None = None, involved_object: str | None = None
    ) -> dict[str, Any]:
        """Recent Kubernetes events, newest last.

        involved_object: filter to one object name (field selector involvedObject.name).
        """
        selector = f"involvedObject.name={involved_object}" if involved_object else None
        events = get_kube_client().list_events(namespace, selector)
        return {"count": len(events), "events": events}

    @read
    async def logs(
        namespace: str, name: str, container: str | None = None, tail_lines: int = 200
    ) -> dict[str, Any]:
        """Tail container logs for one pod (read-only, RBAC-scoped to pods/log)."""
        text = get_kube_client().pod_logs(namespace, name, container, tail_lines)
        return {"pod": f"{namespace}/{name}", "container": container, "lines": text.splitlines()}

    @read
    async def resource_health(namespace: str | None = None) -> dict[str, Any]:
        """Deployment rollout health — desired vs ready replicas, plus the
        unhealthy pods behind any shortfall."""
        kube = get_kube_client()
        workloads = kube.list_workloads(namespace)
        degraded = [
            w
            for w in workloads
            if (w.get("replicas_desired") or 0) > (w.get("replicas_ready") or 0)
        ]
        pods = kube.list_pods(namespace, None)
        unhealthy = [
            p
            for p in pods
            if p.get("phase") not in {"Running", "Succeeded"} or int(p.get("restarts") or 0) > 0
        ]
        return {
            "workloads_total": len(workloads),
            "workloads_degraded": degraded,
            "pods_unhealthy": unhealthy,
            "healthy": not degraded and not unhealthy,
        }
