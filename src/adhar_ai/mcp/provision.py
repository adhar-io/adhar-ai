"""mcp-provision — Crossplane composite-resource state, and XR authoring as a PR.

Crossplane v2 namespaced model (ADR conventions): XRs are namespaced, composed
from `apiextensions.crossplane.io/v2` XRDs. Reads go through the Kubernetes API
using the `adhar-ai-readonly` ClusterRole's get/list/watch on
apiextensions.crossplane.io and platform.adhar.io.
"""

from __future__ import annotations

from typing import Any

from ..clients.kube import get_kube_client
from ..config import MCPConfig
from ..provenance import ORIGIN_LABELS
from .common.pr import open_pr, render_yaml_document
from .common.tools import access_tools

DOMAIN = "provision"

#: Composite kinds the platform ships (platform/controlplane/configuration/xrd).
XR_GROUP = "platform.adhar.io"
XR_VERSION = "v1alpha1"


def _plural(kind: str) -> str:
    lowered = kind.lower()
    if lowered.endswith("s"):
        return f"{lowered}es"
    if lowered.endswith("y"):
        return f"{lowered[:-1]}ies"
    return f"{lowered}s"


def _xr_summary(obj: dict[str, Any]) -> dict[str, Any]:
    meta, status = obj.get("metadata") or {}, obj.get("status") or {}
    conditions = status.get("conditions") or []
    return {
        "kind": obj.get("kind"),
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "ready": next(
            (c.get("status") for c in conditions if c.get("type") == "Ready"), "Unknown"
        ),
        "synced": next(
            (c.get("status") for c in conditions if c.get("type") == "Synced"), "Unknown"
        ),
        "conditions": conditions,
        "created": meta.get("creationTimestamp"),
    }


def register(mcp: Any, cfg: MCPConfig) -> None:
    read, write = access_tools(mcp, DOMAIN)

    @read
    async def list_xrs(kind: str, namespace: str | None = None) -> dict[str, Any]:
        """List Crossplane composite resources of one kind.

        kind: e.g. "CompositeCluster", "CompositeDatabase", "CompositeApplication".
        namespace: Crossplane v2 XRs are namespaced; omit for all namespaces.
        """
        items = get_kube_client().list_custom(XR_GROUP, XR_VERSION, _plural(kind), namespace)
        return {"kind": kind, "count": len(items), "items": [_xr_summary(i) for i in items]}

    @read
    async def xr_status(kind: str, name: str, namespace: str = "adhar-system") -> dict[str, Any]:
        """Full status (conditions, composed-resource refs) of one composite resource."""
        obj = get_kube_client().get_custom(XR_GROUP, XR_VERSION, _plural(kind), name, namespace)
        summary = _xr_summary(obj)
        summary["spec"] = obj.get("spec")
        summary["resource_refs"] = (obj.get("status") or {}).get("resourceRefs")
        return summary

    @write
    async def propose_xr(
        kind: str,
        name: str,
        spec: dict[str, Any],
        why: str,
        namespace: str = "adhar-system",
        repo: str = "packages",
        path: str | None = None,
    ) -> dict[str, Any]:
        """Render a Crossplane composite resource and open it as a Gitea PR.

        The XR is written as a manifest file; it is NOT applied. ArgoCD creates
        it only after a human merges the PR.
        """
        manifest = {
            "apiVersion": f"{XR_GROUP}/{XR_VERSION}",
            "kind": kind,
            "metadata": {"name": name, "namespace": namespace, "labels": dict(ORIGIN_LABELS)},
            "spec": spec,
        }
        target = path or f"infrastructure/crossplane-xrs/manifests/{name}-{kind.lower()}.yaml"
        content = render_yaml_document(manifest)
        ref = await open_pr(
            cfg.gitea,
            repo,
            [{"path": target, "content": content}],
            title=f"provision {kind} {name}",
            why=why,
            tool="propose_xr",
        )
        return ref.as_dict()
