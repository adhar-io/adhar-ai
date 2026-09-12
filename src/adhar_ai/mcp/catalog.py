"""mcp-catalog — the package catalogue (Gitea repos + ArgoCD apps) and golden
paths, plus scaffolding as a Gitea PR.

The catalogue is derived from real state: the `packages` repo tree in Gitea and
the ArgoCD Application inventory. Nothing is hard-coded from a stale list.
"""

from __future__ import annotations

from typing import Any

from ..clients.argocd import ArgoCDClient, summarize_application
from ..clients.gitea import GiteaClient
from ..clients.kube import get_kube_client
from ..config import MCPConfig
from ..provenance import ORIGIN_LABELS
from .common.pr import open_pr, render_yaml_document
from .common.tools import access_tools

DOMAIN = "catalog"

#: Golden paths the catalogue can scaffold. Each declares required params so
#: `template_params` reports a real contract rather than free-form guesses.
GOLDEN_PATHS: dict[str, dict[str, Any]] = {
    "go-service": {
        "description": "Go HTTP service: Deployment + Service + HTTPRoute, wired to the "
        "platform Gateway and ArgoCD via the packages ApplicationSet.",
        "params": {
            "name": "DNS-1123 service name",
            "namespace": "target namespace (default: the app's own)",
            "image": "container image reference",
            "port": "container port (default 8080)",
            "hostname": "public hostname on the Adhar gateway",
        },
        "required": ["name", "image", "hostname"],
    },
    "python-service": {
        "description": "Python (FastAPI/uvicorn) service with the same platform wiring.",
        "params": {
            "name": "DNS-1123 service name",
            "namespace": "target namespace",
            "image": "container image reference",
            "port": "container port (default 8000)",
            "hostname": "public hostname on the Adhar gateway",
        },
        "required": ["name", "image", "hostname"],
    },
    "cnpg-database": {
        "description": "CloudNativePG Cluster on the shared platform Postgres pattern "
        "(ServerSideApply sync-option included — CNPG Clusters need it).",
        "params": {
            "name": "cluster name",
            "namespace": "target namespace",
            "database": "database name",
            "owner": "database owner role",
            "instances": "replica count (default 1)",
            "storage": "PVC size, e.g. 5Gi",
        },
        "required": ["name", "database", "owner"],
    },
}


def _render(golden_path: str, params: dict[str, Any]) -> list[dict[str, str]]:
    name = str(params["name"])
    namespace = str(params.get("namespace") or name)
    labels = {"app.kubernetes.io/name": name, **ORIGIN_LABELS}
    if golden_path == "cnpg-database":
        manifest = {
            "apiVersion": "postgresql.cnpg.io/v1",
            "kind": "Cluster",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": labels,
                # CNPG Clusters overflow client-side apply's last-applied
                # annotation; without SSA ArgoCD force-fails and drops the DB.
                "annotations": {"argocd.argoproj.io/sync-options": "ServerSideApply=true"},
            },
            "spec": {
                "instances": int(params.get("instances") or 1),
                "storage": {"size": str(params.get("storage") or "5Gi")},
                "monitoring": {"enablePodMonitor": True},
                "bootstrap": {
                    "initdb": {
                        "database": str(params["database"]),
                        "owner": str(params["owner"]),
                    }
                },
            },
        }
        return [
            {
                "path": f"data/{name}/manifests/cluster.yaml",
                "content": render_yaml_document(manifest),
            }
        ]

    port = int(params.get("port") or (8000 if golden_path == "python-service" else 8080))
    hostname = str(params["hostname"])
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 65532,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": name,
                            "image": str(params["image"]),
                            "ports": [{"name": "http", "containerPort": port}],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "128Mi"},
                                "limits": {"cpu": "500m", "memory": "512Mi"},
                            },
                            "readinessProbe": {
                                "httpGet": {"path": "/healthz", "port": "http"},
                                "initialDelaySeconds": 5,
                                "periodSeconds": 10,
                            },
                        }
                    ],
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "type": "ClusterIP",
            "selector": {"app.kubernetes.io/name": name},
            "ports": [{"name": "http", "port": port, "targetPort": "http"}],
        },
    }
    route = {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "parentRefs": [{"name": "adhar-gateway", "namespace": "adhar-system"}],
            "hostnames": [hostname],
            "rules": [
                {
                    "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                    "backendRefs": [{"name": name, "port": port}],
                }
            ],
        },
    }
    base = f"application/{name}/manifests"
    return [
        {"path": f"{base}/deployment.yaml", "content": render_yaml_document(deployment)},
        {"path": f"{base}/service.yaml", "content": render_yaml_document(service)},
        {"path": f"{base}/httproute.yaml", "content": render_yaml_document(route)},
    ]


def register(mcp: Any, cfg: MCPConfig) -> None:
    read, write = access_tools(mcp, DOMAIN)

    @read
    async def search_packages(query: str = "", category: str = "") -> dict[str, Any]:
        """Search the platform package catalogue.

        Sources: the `packages` repo tree in Gitea (what exists in Git) joined
        with the ArgoCD Application inventory (what is actually deployed).
        """
        packages: list[dict[str, Any]] = []
        errors: dict[str, str] = {}

        gitea = GiteaClient(cfg.gitea)
        try:
            # The `packages` repo has the category directories at its ROOT —
            # the ApplicationSet's manifestPath values are `security/vault/manifests`.
            categories = (
                [category]
                if category
                else [
                    str(e.get("name"))
                    for e in await gitea.list_dir("packages", "")
                    if e.get("type") == "dir"
                ]
            )
            for cat in categories:
                for entry in await gitea.list_dir("packages", cat):
                    if entry.get("type") == "dir":
                        packages.append({"name": entry.get("name"), "category": cat})
        except Exception as exc:
            errors["gitea"] = f"{type(exc).__name__}: {exc}"
        finally:
            await gitea.aclose()

        deployed: dict[str, dict[str, Any]] = {}
        try:
            if cfg.argocd.configured:
                argo = ArgoCDClient(cfg.argocd)
                try:
                    apps = await argo.list_applications()
                finally:
                    await argo.aclose()
            else:
                apps = get_kube_client().list_custom(
                    "argoproj.io", "v1alpha1", "applications", "adhar-system"
                )
            for app in apps:
                summary = summarize_application(app)
                deployed[str(summary["name"])] = summary
        except Exception as exc:
            errors["argocd"] = f"{type(exc).__name__}: {exc}"

        for pkg in packages:
            live = deployed.get(str(pkg["name"]))
            pkg["deployed"] = live is not None
            if live:
                pkg["sync_status"] = live.get("sync_status")
                pkg["health_status"] = live.get("health_status")

        if query:
            needle = query.lower()
            packages = [p for p in packages if needle in str(p["name"]).lower()]

        result: dict[str, Any] = {"count": len(packages), "packages": packages}
        if errors:
            result["partial"] = errors
        return result

    @read
    async def template_params(golden_path: str = "") -> dict[str, Any]:
        """List golden paths, or the parameter contract of one of them."""
        if not golden_path:
            return {
                "golden_paths": [
                    {"name": k, "description": v["description"]} for k, v in GOLDEN_PATHS.items()
                ]
            }
        if golden_path not in GOLDEN_PATHS:
            return {
                "error": f"unknown golden path {golden_path!r}",
                "available": sorted(GOLDEN_PATHS),
            }
        return {"golden_path": golden_path, **GOLDEN_PATHS[golden_path]}

    @write
    async def scaffold(
        golden_path: str, params: dict[str, Any], why: str, repo: str = "packages"
    ) -> dict[str, Any]:
        """Render a golden path into manifests and open them as a Gitea PR.

        Nothing is applied: the PR is reviewed and merged by a human, and
        ArgoCD's ApplicationSet picks the package up from the merged tree.
        """
        if golden_path not in GOLDEN_PATHS:
            return {
                "error": f"unknown golden path {golden_path!r}",
                "available": sorted(GOLDEN_PATHS),
            }
        missing = [p for p in GOLDEN_PATHS[golden_path]["required"] if not params.get(p)]
        if missing:
            return {"error": f"missing required params: {missing}", "golden_path": golden_path}
        changes = _render(golden_path, params)
        ref = await open_pr(
            cfg.gitea,
            repo,
            changes,
            title=f"scaffold {golden_path} {params['name']}",
            why=why,
            tool="scaffold",
        )
        return ref.as_dict()
