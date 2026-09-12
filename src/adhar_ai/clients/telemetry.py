"""Prometheus / Loki / Tempo HTTP query clients.

These are the real query APIs: `/api/v1/query`, `/api/v1/query_range` and
`/api/v1/alerts` on Prometheus, `/loki/api/v1/query_range` on Loki, and
`/api/search` + `/api/traces/{id}` on Tempo.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..config import TelemetryConfig
from .errors import BackendNotConfigured


def _rfc3339_nanos(seconds: float) -> str:
    return str(int(seconds * 1_000_000_000))


class TelemetryClient:
    def __init__(self, cfg: TelemetryConfig, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
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

    def _base(self, backend: str) -> str:
        url = {
            "Prometheus": self.cfg.prometheus_url,
            "Loki": self.cfg.loki_url,
            "Tempo": self.cfg.tempo_url,
        }[backend]
        if not url:
            raise BackendNotConfigured(backend, f"set {backend.upper()}_URL")
        return url

    async def _get(self, backend: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        resp = await self._http().get(f"{self._base(backend)}{path}", params=params)
        resp.raise_for_status()
        return dict(resp.json())

    # ------------------------------------------------------------ Prometheus --

    async def promql(self, query: str, time_: float | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"query": query}
        if time_ is not None:
            params["time"] = time_
        return await self._get("Prometheus", "/api/v1/query", params)

    async def promql_range(
        self, query: str, minutes: int = 60, step: str = "60s"
    ) -> dict[str, Any]:
        end = time.time()
        return await self._get(
            "Prometheus",
            "/api/v1/query_range",
            {"query": query, "start": end - minutes * 60, "end": end, "step": step},
        )

    async def alerts(self) -> dict[str, Any]:
        return await self._get("Prometheus", "/api/v1/alerts", {})

    # ------------------------------------------------------------------ Loki --

    async def logql(self, query: str, minutes: int = 60, limit: int = 100) -> dict[str, Any]:
        end = time.time()
        return await self._get(
            "Loki",
            "/loki/api/v1/query_range",
            {
                "query": query,
                "start": _rfc3339_nanos(end - minutes * 60),
                "end": _rfc3339_nanos(end),
                "limit": limit,
                "direction": "backward",
            },
        )

    # ----------------------------------------------------------------- Tempo --

    async def traceql(self, query: str, limit: int = 20) -> dict[str, Any]:
        return await self._get("Tempo", "/api/search", {"q": query, "limit": limit})

    async def trace(self, trace_id: str) -> dict[str, Any]:
        return await self._get("Tempo", f"/api/traces/{trace_id}", {})
