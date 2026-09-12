"""ArgoCD REST client.

Authenticates with the platform's `argocd-credentials` convention: either a
pre-minted `ARGOCD_AUTH_TOKEN`, or admin credentials from which a session token
is minted via `POST /api/v1/session` (the same approach adhar-console uses, so
the token can never go stale).

Read-only by construction: there is no sync/rollback/delete method here.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..config import ArgoCDConfig
from .errors import BackendNotConfigured


class ArgoCDClient:
    def __init__(self, cfg: ArgoCDConfig, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
        self._client = client
        self._owns_client = client is None
        self._token = cfg.token

    @property
    def configured(self) -> bool:
        return self.cfg.configured

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0, verify=self.cfg.verify_tls)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _session_token(self) -> str:
        if self._token:
            return self._token
        if not self.cfg.url:
            raise BackendNotConfigured("ArgoCD", "set ARGOCD_URL")
        if not self.cfg.password:
            raise BackendNotConfigured(
                "ArgoCD", "set ARGOCD_AUTH_TOKEN or ARGOCD_PASSWORD (argocd-credentials secret)"
            )
        resp = await self._http().post(
            f"{self.cfg.url}/api/v1/session",
            json={"username": self.cfg.username, "password": self.cfg.password},
        )
        resp.raise_for_status()
        self._token = str(resp.json()["token"])
        return self._token

    async def _get(self, path: str, **kwargs: Any) -> Any:
        token = await self._session_token()
        resp = await self._http().get(
            f"{self.cfg.url}/api/v1{path}",
            headers={"Authorization": f"Bearer {token}"},
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json()

    # ----------------------------------------------------------------------- #

    async def list_applications(self, selector: str | None = None) -> list[dict[str, Any]]:
        params = {"selector": selector} if selector else None
        payload = await self._get("/applications", params=params)
        return list(payload.get("items") or [])

    async def get_application(self, name: str) -> dict[str, Any]:
        return dict(await self._get(f"/applications/{name}"))

    async def managed_resources(self, name: str) -> list[dict[str, Any]]:
        """`/managed-resources` is ArgoCD's live-vs-desired diff feed."""
        payload = await self._get(f"/applications/{name}/managed-resources")
        return list(payload.get("items") or [])


def summarize_application(app: dict[str, Any]) -> dict[str, Any]:
    """Flatten an ArgoCD Application (REST payload or the Kubernetes CR — the
    shapes are identical) into the fields the tools return."""
    meta = app.get("metadata") or {}
    spec = app.get("spec") or {}
    status = app.get("status") or {}
    sync = status.get("sync") or {}
    health = status.get("health") or {}
    source = spec.get("source") or (spec.get("sources") or [{}])[0]
    operation = status.get("operationState") or {}
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "project": spec.get("project"),
        "sync_status": sync.get("status"),
        "health_status": health.get("status"),
        "health_message": health.get("message"),
        "revision": sync.get("revision"),
        "repo_url": source.get("repoURL"),
        "path": source.get("path"),
        "target_revision": source.get("targetRevision"),
        "destination": spec.get("destination"),
        "last_operation_phase": operation.get("phase"),
        "last_operation_message": operation.get("message"),
        "conditions": status.get("conditions") or [],
    }
