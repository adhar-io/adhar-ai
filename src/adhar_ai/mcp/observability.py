"""mcp-observability — PromQL / LogQL / TraceQL, SLO burn rate, correlation.

Read-only: these are query APIs, and no write tool exists on this server.
"""

from __future__ import annotations

from typing import Any

from ..clients.telemetry import TelemetryClient
from ..config import MCPConfig
from .common.tools import access_tools

DOMAIN = "observability"


def _series(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = (payload.get("data") or {}).get("result") or []
    out = []
    for item in result:
        out.append(
            {
                "metric": item.get("metric"),
                "value": item.get("value"),
                "values": item.get("values"),
            }
        )
    return out


def register(mcp: Any, cfg: MCPConfig) -> None:
    read, write = access_tools(mcp, DOMAIN)

    telemetry = TelemetryClient(cfg.telemetry)

    @read
    async def promql(query: str, range_minutes: int = 0, step: str = "60s") -> dict[str, Any]:
        """Run a PromQL query against the platform Prometheus.

        range_minutes: 0 for an instant query, >0 for a range query.
        """
        payload = (
            await telemetry.promql_range(query, range_minutes, step)
            if range_minutes > 0
            else await telemetry.promql(query)
        )
        return {"query": query, "status": payload.get("status"), "series": _series(payload)}

    @read
    async def logql(query: str, minutes: int = 60, limit: int = 100) -> dict[str, Any]:
        """Run a LogQL query against Loki, e.g. '{namespace="adhar-system"} |= "error"'."""
        payload = await telemetry.logql(query, minutes, limit)
        streams = (payload.get("data") or {}).get("result") or []
        return {
            "query": query,
            "stream_count": len(streams),
            "streams": [
                {"labels": s.get("stream"), "entries": (s.get("values") or [])[:limit]}
                for s in streams
            ],
        }

    @read
    async def traceql(query: str, limit: int = 20) -> dict[str, Any]:
        """Search Tempo with TraceQL, e.g. '{ duration > 2s }'."""
        payload = await telemetry.traceql(query, limit)
        return {"query": query, "traces": payload.get("traces") or payload.get("metrics") or []}

    @read
    async def slo_burn(
        slo_metric: str, objective: float = 0.99, windows: str = "5m,1h,6h"
    ) -> dict[str, Any]:
        """Multi-window error-budget burn rate for an SLI ratio metric.

        slo_metric: a PromQL *ratio* expression yielding the GOOD-event rate
          in [0,1], e.g. 'sum(rate(http_requests_total{code!~"5.."}[WINDOW]))
          / sum(rate(http_requests_total[WINDOW]))'. The literal token WINDOW
          is substituted per window.
        objective: the SLO target, e.g. 0.99.
        """
        budget = 1.0 - objective
        results = []
        for window in [w.strip() for w in windows.split(",") if w.strip()]:
            expr = slo_metric.replace("WINDOW", window)
            payload = await telemetry.promql(expr)
            series = _series(payload)
            good = None
            if series and series[0].get("value"):
                try:
                    good = float(series[0]["value"][1])
                except (TypeError, ValueError, IndexError):
                    good = None
            burn = None if good is None or budget <= 0 else round((1.0 - good) / budget, 3)
            results.append(
                {"window": window, "good_ratio": good, "burn_rate": burn, "query": expr}
            )
        return {"objective": objective, "error_budget": budget, "windows": results}

    @read
    async def correlate(
        namespace: str, workload: str, minutes: int = 30
    ) -> dict[str, Any]:
        """Pull metrics, logs and firing alerts for one workload into a single
        view — the first call an alert triage usually makes.

        Backends that are not configured are reported as such; nothing is
        invented to fill the gap.
        """
        out: dict[str, Any] = {"namespace": namespace, "workload": workload, "minutes": minutes}

        async def attempt(key: str, coro: Any) -> None:
            try:
                out[key] = await coro
            except Exception as exc:
                out[key] = {"unavailable": f"{type(exc).__name__}: {exc}"}

        restart_expr = (
            f'sum by (pod) (increase(kube_pod_container_status_restarts_total'
            f'{{namespace="{namespace}",pod=~"{workload}.*"}}[{minutes}m]))'
        )
        await attempt("restarts", telemetry.promql(restart_expr))
        await attempt(
            "logs",
            telemetry.logql(
                f'{{namespace="{namespace}"}} |= "{workload}"', minutes=minutes, limit=50
            ),
        )
        await attempt("alerts", telemetry.alerts())
        if isinstance(out.get("alerts"), dict) and "data" in out["alerts"]:
            firing = (out["alerts"].get("data") or {}).get("alerts") or []
            out["alerts"] = [
                a
                for a in firing
                if (a.get("labels") or {}).get("namespace") == namespace
                or workload in str(a.get("labels"))
            ]
        return out
