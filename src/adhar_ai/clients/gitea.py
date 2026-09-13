"""Gitea REST client.

Read: repositories, file contents, trees — used by the catalog tools.
Write: **branch + file change + pull request only**. There is no method here
that touches a cluster; `open_pull_request` is the single write path the whole
agent has (ADR-0024 §3).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import GiteaConfig, gitea_api_base
from .errors import BackendNotConfigured


@dataclass(slots=True)
class PRRef:
    repo: str
    number: int
    url: str
    branch: str
    files: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "number": self.number,
            "url": self.url,
            "branch": self.branch,
            "files": self.files,
        }


class GiteaClient:
    def __init__(self, cfg: GiteaConfig, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
        self._client = client
        self._owns_client = client is None

    @property
    def api(self) -> str:
        if not self.cfg.api_url:
            raise BackendNotConfigured("Gitea", "set GITEA_API_URL")
        # Accepts a bare origin or one that already carries /api/v1; see
        # `gitea_api_base` for why both shapes reach this code in practice.
        return gitea_api_base(self.cfg.api_url)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"Accept": "application/json"}
            if self.cfg.bot_token:
                headers["Authorization"] = f"token {self.cfg.bot_token}"
            self._client = httpx.AsyncClient(timeout=30.0, headers=headers)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        resp = await self._http().request(method, f"{self.api}{path}", **kwargs)
        resp.raise_for_status()
        return resp

    # ---------------------------------------------------------------- reads --

    async def list_org_repos(self, org: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        org = org or self.cfg.org
        resp = await self._request("GET", f"/orgs/{org}/repos", params={"limit": limit})
        return list(resp.json())

    async def get_file(self, repo: str, path: str, ref: str = "main") -> dict[str, Any]:
        resp = await self._request(
            "GET", f"/repos/{self.cfg.org}/{repo}/contents/{path}", params={"ref": ref}
        )
        return dict(resp.json())

    async def get_file_text(self, repo: str, path: str, ref: str = "main") -> str:
        meta = await self.get_file(repo, path, ref)
        content = meta.get("content") or ""
        if meta.get("encoding") == "base64":
            return base64.b64decode(content).decode("utf-8", "replace")
        return str(content)

    async def list_dir(self, repo: str, path: str = "", ref: str = "main") -> list[dict[str, Any]]:
        resp = await self._request(
            "GET", f"/repos/{self.cfg.org}/{repo}/contents/{path}", params={"ref": ref}
        )
        payload = resp.json()
        return list(payload) if isinstance(payload, list) else [payload]

    async def get_branch_sha(self, repo: str, branch: str = "main") -> str:
        resp = await self._request("GET", f"/repos/{self.cfg.org}/{repo}/branches/{branch}")
        return str(resp.json()["commit"]["id"])

    # --------------------------------------------------------------- writes --
    # The only mutating surface in this entire codebase.

    async def create_branch(self, repo: str, new_branch: str, base: str = "main") -> dict[str, Any]:
        resp = await self._request(
            "POST",
            f"/repos/{self.cfg.org}/{repo}/branches",
            json={"new_branch_name": new_branch, "old_branch_name": base},
        )
        return dict(resp.json())

    async def put_file(
        self, repo: str, path: str, content: str, branch: str, message: str
    ) -> dict[str, Any]:
        """Create or update one file on ``branch``. Existing files need their
        blob SHA, so probe first and fall through to a create on 404."""
        body: dict[str, Any] = {
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
            "message": message,
        }
        method = "POST"
        try:
            existing = await self.get_file(repo, path, ref=branch)
            if sha := existing.get("sha"):
                body["sha"] = sha
                method = "PUT"
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
        resp = await self._request(
            method, f"/repos/{self.cfg.org}/{repo}/contents/{path}", json=body
        )
        return dict(resp.json())

    async def create_pull_request(
        self,
        repo: str,
        head: str,
        title: str,
        body: str,
        base: str = "main",
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"head": head, "base": base, "title": title, "body": body}
        resp = await self._request("POST", f"/repos/{self.cfg.org}/{repo}/pulls", json=payload)
        pr = dict(resp.json())
        if labels:
            await self._apply_labels(repo, int(pr["number"]), labels)
        return pr

    async def _apply_labels(self, repo: str, number: int, labels: list[str]) -> None:
        """Best-effort: label the PR by id, creating missing labels first.
        A label failure must never lose an otherwise-good proposal."""
        try:
            resp = await self._request("GET", f"/repos/{self.cfg.org}/{repo}/labels")
            existing = {str(item["name"]): int(item["id"]) for item in resp.json()}
            ids: list[int] = []
            for name in labels:
                if name not in existing:
                    created = await self._request(
                        "POST",
                        f"/repos/{self.cfg.org}/{repo}/labels",
                        json={"name": name, "color": "#7c3aed"},
                    )
                    existing[name] = int(created.json()["id"])
                ids.append(existing[name])
            await self._request(
                "POST", f"/repos/{self.cfg.org}/{repo}/issues/{number}/labels", json={"labels": ids}
            )
        except (httpx.HTTPError, KeyError, ValueError):
            return
