"""OpenCost HTTP API client (`/allocation/compute`, `/assets`)."""

from __future__ import annotations

from typing import Any

import httpx

from .errors import BackendNotConfigured


class OpenCostClient:
    def __init__(self, url: str, client: httpx.AsyncClient | None = None) -> None:
        self.url = url.rstrip("/")
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=60.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.url:
            raise BackendNotConfigured("OpenCost", "set OPENCOST_URL")
        resp = await self._http().get(f"{self.url}{path}", params=params)
        resp.raise_for_status()
        return dict(resp.json())

    async def allocation(
        self, window: str = "7d", aggregate: str = "namespace", accumulate: bool = True
    ) -> dict[str, Any]:
        return await self._get(
            "/allocation/compute",
            {"window": window, "aggregate": aggregate, "accumulate": str(accumulate).lower()},
        )

    async def assets(self, window: str = "7d") -> dict[str, Any]:
        return await self._get("/assets", {"window": window, "accumulate": "true"})


def summarize_allocation(payload: dict[str, Any], top: int = 20) -> list[dict[str, Any]]:
    """OpenCost returns `data: [ {key: {...costs}} ]`. Flatten to a ranked list."""
    data = payload.get("data") or []
    rows: dict[str, dict[str, Any]] = {}
    for window in data:
        if not isinstance(window, dict):
            continue
        for key, alloc in window.items():
            if not isinstance(alloc, dict):
                continue
            row = rows.setdefault(key, {"name": key, "total_cost": 0.0})
            row["total_cost"] += float(alloc.get("totalCost") or 0.0)
            for field in ("cpuCost", "ramCost", "gpuCost", "pvCost", "networkCost"):
                row[field] = row.get(field, 0.0) + float(alloc.get(field) or 0.0)
            row["efficiency"] = alloc.get("totalEfficiency")
    ranked = sorted(rows.values(), key=lambda r: float(r["total_cost"]), reverse=True)
    return ranked[:top]
