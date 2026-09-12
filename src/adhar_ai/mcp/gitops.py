"""mcp-gitops — ArgoCD read tools plus `propose_change`, the platform's single
write path.
"""

from __future__ import annotations

from typing import Any

from ..clients.argocd import ArgoCDClient, summarize_application
from ..clients.kube import get_kube_client
from ..config import MCPConfig
from .common.pr import open_pr
from .common.tools import access_tools

DOMAIN = "gitops"

ARGO_GROUP, ARGO_VERSION, ARGO_PLURAL = "argoproj.io", "v1alpha1", "applications"


async def _applications(cfg: MCPConfig) -> list[dict[str, Any]]:
    """Prefer the ArgoCD REST API; fall back to the Application CRs via the
    Kubernetes API (the `adhar-ai-readonly` ClusterRole grants get/list/watch on
    argoproj.io/applications), so read tools still work unkeyed."""
    if cfg.argocd.configured:
        client = ArgoCDClient(cfg.argocd)
        try:
            return await client.list_applications()
        finally:
            await client.aclose()
    return get_kube_client().list_custom(ARGO_GROUP, ARGO_VERSION, ARGO_PLURAL, "adhar-system")


async def _application(cfg: MCPConfig, name: str) -> dict[str, Any]:
    if cfg.argocd.configured:
        client = ArgoCDClient(cfg.argocd)
        try:
            return await client.get_application(name)
        finally:
            await client.aclose()
    return get_kube_client().get_custom(
        ARGO_GROUP, ARGO_VERSION, ARGO_PLURAL, name, "adhar-system"
    )


def register(mcp: Any, cfg: MCPConfig) -> None:
    read, write = access_tools(mcp, DOMAIN)

    @read
    async def app_status(app: str) -> dict[str, Any]:
        """Sync and health status of one ArgoCD Application."""
        return summarize_application(await _application(cfg, app))

    @read
    async def sync_status(only_unhealthy: bool = False) -> dict[str, Any]:
        """Fleet-wide sync/health inventory of every ArgoCD Application.

        only_unhealthy: return just the OutOfSync or non-Healthy applications.
        """
        apps = [summarize_application(a) for a in await _applications(cfg)]
        if only_unhealthy:
            apps = [
                a
                for a in apps
                if a.get("sync_status") != "Synced" or a.get("health_status") != "Healthy"
            ]
        return {
            "count": len(apps),
            "out_of_sync": sum(1 for a in apps if a.get("sync_status") != "Synced"),
            "degraded": sum(1 for a in apps if a.get("health_status") not in {"Healthy", None}),
            "applications": apps,
        }

    @read
    async def app_diff(app: str) -> dict[str, Any]:
        """Live-vs-desired diff for one Application (ArgoCD managed-resources).

        Requires ARGOCD_URL plus a credential; the Kubernetes fallback cannot
        produce a diff, and says so rather than guessing.
        """
        if not cfg.argocd.configured:
            return {
                "app": app,
                "diff_available": False,
                "reason": (
                    "app_diff needs the ArgoCD REST API: set ARGOCD_URL and a credential "
                    "(ARGOCD_AUTH_TOKEN, or ARGOCD_PASSWORD from the argocd-credentials secret)"
                ),
            }
        client = ArgoCDClient(cfg.argocd)
        try:
            items = await client.managed_resources(app)
        finally:
            await client.aclose()
        changed = [
            {
                "kind": i.get("kind"),
                "name": i.get("name"),
                "namespace": i.get("namespace"),
                "diff": i.get("diff"),
            }
            for i in items
            if i.get("diff")
        ]
        return {"app": app, "diff_available": True, "changed": changed, "count": len(changed)}

    @write
    async def propose_change(
        repo: str, changes: list[dict[str, str]], title: str, why: str
    ) -> dict[str, Any]:
        """Open a Gitea pull request with the given file changes.

        This is the ONLY write path in Adhar AI. It never applies to a cluster;
        ArgoCD reconciles after a human merges the PR.

        repo: "packages" or "environments".
        changes: [{"path": "...", "content": "..."}] — full file contents.
        title: short PR title (prefixed "[adhar-ai]" automatically).
        why: human-readable rationale, recorded verbatim in the PR body.
        """
        ref = await open_pr(
            cfg.gitea, repo, changes, title, why, tool="propose_change", user=None
        )
        return ref.as_dict()
