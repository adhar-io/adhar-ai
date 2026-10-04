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
    async def list_resources(
        group: str, version: str, plural: str, namespace: str | None = None
    ) -> dict[str, Any]:
        """List custom resources of one kind, summarised: name, namespace, readiness,
        and the facts that matter for that kind.

        group/version/plural name the API, e.g.
          gateway.networking.k8s.io / v1 / httproutes      -> hostnames and backends
          postgresql.cnpg.io / v1 / clusters               -> instances, ready, primary
          cert-manager.io / v1 / certificates              -> dnsNames, notAfter, ready
          external-secrets.io / v1 / externalsecrets       -> target secret, synced
          argoproj.io / v1alpha1 / applications            -> sync and health
        namespace: restrict to one namespace (omit for cluster-wide).

        Read-only. Full objects are not returned: a CRD list is kilobytes of
        status per item, and this is for inventory rather than debugging —
        `describe` is for one object in detail.
        """
        items = get_kube_client().list_custom(group, version, plural, namespace)
        return {
            "kind": plural,
            "count": len(items),
            "items": [_resource_summary(plural, item) for item in items],
        }

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


def _conditions(obj: dict[str, Any]) -> dict[str, str]:
    """`{type: status}` for the conditions a human reads first."""
    out: dict[str, str] = {}
    for c in (obj.get("status") or {}).get("conditions") or []:
        if isinstance(c, dict) and c.get("type"):
            out[str(c["type"])] = str(c.get("status", ""))
    return out


def _resource_summary(plural: str, obj: dict[str, Any]) -> dict[str, Any]:
    """The facts that matter for a kind, and nothing else.

    Each branch is a few fields chosen because they answer the question
    somebody listing that kind is asking: a route for its hostnames, a
    certificate for its expiry, a database for whether it has a primary.
    """
    meta = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    conditions = _conditions(obj)
    summary: dict[str, Any] = {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "created": meta.get("creationTimestamp"),
    }
    ready = conditions.get("Ready")
    if ready is not None:
        summary["ready"] = ready == "True"

    if plural in ("httproutes", "grpcroutes", "tlsroutes"):
        summary["hostnames"] = list(spec.get("hostnames") or [])
        summary["backends"] = [
            f"{b.get('name')}:{b.get('port')}" if b.get("port") else str(b.get("name"))
            for rule in spec.get("rules") or []
            for b in (rule or {}).get("backendRefs") or []
            if isinstance(b, dict)
        ]
        accepted = [
            c.get("status")
            for parent in status.get("parents") or []
            for c in (parent or {}).get("conditions") or []
            if isinstance(c, dict) and c.get("type") == "Accepted"
        ]
        if accepted:
            summary["accepted"] = all(a == "True" for a in accepted)
    elif plural == "clusters":  # CNPG
        summary["instances"] = spec.get("instances")
        summary["readyInstances"] = status.get("readyInstances")
        summary["primary"] = status.get("currentPrimary")
        summary["phase"] = status.get("phase")
        summary["image"] = spec.get("imageName")
    elif plural == "certificates":
        summary["dnsNames"] = list(spec.get("dnsNames") or [])
        summary["issuer"] = (spec.get("issuerRef") or {}).get("name")
        summary["secret"] = spec.get("secretName")
        summary["notAfter"] = status.get("notAfter")
        summary["renewalTime"] = status.get("renewalTime")
    elif plural == "externalsecrets":
        summary["target"] = (spec.get("target") or {}).get("name") or meta.get("name")
        summary["store"] = (spec.get("secretStoreRef") or {}).get("name")
        summary["synced"] = conditions.get("Ready") == "True"
        summary["refreshInterval"] = spec.get("refreshInterval")
    elif plural == "applications":
        summary["sync"] = (status.get("sync") or {}).get("status")
        summary["health"] = (status.get("health") or {}).get("status")
        summary["revision"] = (status.get("sync") or {}).get("revision", "")[:12]
    else:
        # Unknown kind: the conditions and the scalar status fields, which is
        # what a generic `kubectl get` would show.
        summary["conditions"] = conditions
        summary["status"] = {
            k: v for k, v in status.items() if not isinstance(v, (dict, list))
        }
    return summary
