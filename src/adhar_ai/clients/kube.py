"""Read-only Kubernetes access.

Loads in-cluster config when running as a Pod (the `adhar-ai` ServiceAccount,
whose ClusterRole is get/list/watch only) and falls back to the local kubeconfig
for development. Only read verbs are exposed — this module deliberately has no
create/patch/delete surface, so there is no apply path to hijack.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from .errors import BackendNotConfigured


class KubeClient(Protocol):
    """The surface the tools use. A fake implementing this is enough for tests."""

    def list_pods(
        self, namespace: str | None, label_selector: str | None
    ) -> list[dict[str, Any]]: ...

    def get_pod(self, namespace: str, name: str) -> dict[str, Any]: ...

    def pod_logs(
        self, namespace: str, name: str, container: str | None, tail_lines: int
    ) -> str: ...

    def list_events(
        self, namespace: str | None, field_selector: str | None
    ) -> list[dict[str, Any]]: ...

    def list_workloads(self, namespace: str | None) -> list[dict[str, Any]]: ...

    def list_custom(
        self, group: str, version: str, plural: str, namespace: str | None
    ) -> list[dict[str, Any]]: ...

    def get_custom(
        self, group: str, version: str, plural: str, name: str, namespace: str | None
    ) -> dict[str, Any]: ...


def _sanitize(obj: Any) -> Any:
    """Kubernetes objects carry a lot of noise (managedFields, last-applied).
    Strip it so the model's context is spent on signal, and so the enormous
    kubectl.kubernetes.io/last-applied-configuration blob never reaches a prompt."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key == "managedFields":
                continue
            if key == "annotations" and isinstance(value, dict):
                value = {
                    k: v
                    for k, v in value.items()
                    if k != "kubectl.kubernetes.io/last-applied-configuration"
                }
            out[key] = _sanitize(value)
        return out
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


class RealKubeClient:
    """Thin wrapper over the official `kubernetes` client, reads only."""

    def __init__(self) -> None:
        from kubernetes import client, config  # imported lazily: dev boxes may lack a cluster

        try:
            config.load_incluster_config()
        except Exception:
            try:
                config.load_kube_config()
            except Exception as exc:  # pragma: no cover - environment dependent
                raise BackendNotConfigured(
                    "Kubernetes", "no in-cluster ServiceAccount and no kubeconfig"
                ) from exc
        self._core = client.CoreV1Api()
        self._apps = client.AppsV1Api()
        self._custom = client.CustomObjectsApi()
        self._api_client = client.ApiClient()

    def _to_dict(self, obj: Any) -> Any:
        return _sanitize(self._api_client.sanitize_for_serialization(obj))

    def list_pods(
        self, namespace: str | None = None, label_selector: str | None = None
    ) -> list[dict[str, Any]]:
        kwargs = {"label_selector": label_selector} if label_selector else {}
        result = (
            self._core.list_namespaced_pod(namespace, **kwargs)
            if namespace
            else self._core.list_pod_for_all_namespaces(**kwargs)
        )
        return [_pod_summary(self._to_dict(p)) for p in result.items]

    def get_pod(self, namespace: str, name: str) -> dict[str, Any]:
        return dict(self._to_dict(self._core.read_namespaced_pod(name, namespace)))

    def pod_logs(
        self, namespace: str, name: str, container: str | None = None, tail_lines: int = 200
    ) -> str:
        kwargs: dict[str, Any] = {"tail_lines": tail_lines}
        if container:
            kwargs["container"] = container
        return str(self._core.read_namespaced_pod_log(name, namespace, **kwargs))

    def list_events(
        self, namespace: str | None = None, field_selector: str | None = None
    ) -> list[dict[str, Any]]:
        kwargs = {"field_selector": field_selector} if field_selector else {}
        result = (
            self._core.list_namespaced_event(namespace, **kwargs)
            if namespace
            else self._core.list_event_for_all_namespaces(**kwargs)
        )
        return [_event_summary(self._to_dict(e)) for e in result.items]

    def list_workloads(self, namespace: str | None = None) -> list[dict[str, Any]]:
        result = (
            self._apps.list_namespaced_deployment(namespace)
            if namespace
            else self._apps.list_deployment_for_all_namespaces()
        )
        return [_deployment_summary(self._to_dict(d)) for d in result.items]

    def list_custom(
        self, group: str, version: str, plural: str, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        if namespace:
            payload = self._custom.list_namespaced_custom_object(group, version, namespace, plural)
        else:
            payload = self._custom.list_cluster_custom_object(group, version, plural)
        return [_sanitize(i) for i in (payload.get("items") or [])]

    def get_custom(
        self, group: str, version: str, plural: str, name: str, namespace: str | None = None
    ) -> dict[str, Any]:
        if namespace:
            payload = self._custom.get_namespaced_custom_object(
                group, version, namespace, plural, name
            )
        else:
            payload = self._custom.get_cluster_custom_object(group, version, plural, name)
        return dict(_sanitize(payload))


def _pod_summary(pod: dict[str, Any]) -> dict[str, Any]:
    meta, status, spec = pod.get("metadata", {}), pod.get("status", {}), pod.get("spec", {})
    containers = status.get("containerStatuses") or []
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "phase": status.get("phase"),
        "node": spec.get("nodeName"),
        "pod_ip": status.get("podIP"),
        "start_time": status.get("startTime"),
        "restarts": sum(int(c.get("restartCount") or 0) for c in containers),
        "ready": f"{sum(1 for c in containers if c.get('ready'))}/{len(containers)}",
        "containers": [
            {
                "name": c.get("name"),
                "ready": c.get("ready"),
                "restarts": c.get("restartCount"),
                "state": next(iter((c.get("state") or {}).keys()), None),
                "reason": _container_reason(c),
                "image": c.get("image"),
            }
            for c in containers
        ],
        "labels": meta.get("labels") or {},
    }


def _container_reason(container: dict[str, Any]) -> str | None:
    state = container.get("state") or {}
    for value in state.values():
        if isinstance(value, dict) and value.get("reason"):
            return str(value["reason"])
    last = (container.get("lastState") or {}).get("terminated") or {}
    return str(last["reason"]) if last.get("reason") else None


def _event_summary(event: dict[str, Any]) -> dict[str, Any]:
    involved = event.get("involvedObject") or {}
    return {
        "type": event.get("type"),
        "reason": event.get("reason"),
        "message": event.get("message"),
        "count": event.get("count"),
        "last_timestamp": event.get("lastTimestamp") or event.get("eventTime"),
        "namespace": (event.get("metadata") or {}).get("namespace"),
        "object": f"{involved.get('kind')}/{involved.get('name')}",
    }


def _deployment_summary(dep: dict[str, Any]) -> dict[str, Any]:
    meta, status, spec = dep.get("metadata", {}), dep.get("status", {}), dep.get("spec", {})
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "replicas_desired": spec.get("replicas"),
        "replicas_ready": status.get("readyReplicas") or 0,
        "replicas_available": status.get("availableReplicas") or 0,
        "updated": status.get("updatedReplicas") or 0,
        "conditions": [
            {"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason")}
            for c in (status.get("conditions") or [])
        ],
    }


_CLIENT: KubeClient | None = None


def get_kube_client() -> KubeClient:
    """Process-wide singleton. `ADHAR_AI_KUBE_DISABLED=1` makes the failure
    explicit for local runs with no cluster."""
    global _CLIENT
    if os.environ.get("ADHAR_AI_KUBE_DISABLED") == "1":
        raise BackendNotConfigured("Kubernetes", "disabled via ADHAR_AI_KUBE_DISABLED=1")
    if _CLIENT is None:
        _CLIENT = RealKubeClient()
    return _CLIENT


def set_kube_client(client: KubeClient | None) -> None:
    """Test seam: inject a fake implementing the KubeClient protocol."""
    global _CLIENT
    _CLIENT = client
