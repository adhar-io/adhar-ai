"""mcp-cost — OpenCost allocation queries. Read-only, no write tool."""

from __future__ import annotations

from typing import Any

from ..clients.opencost import OpenCostClient, summarize_allocation
from ..config import MCPConfig
from .common.tools import access_tools

DOMAIN = "cost"

VALID_DIMENSIONS = (
    "namespace",
    "controller",
    "pod",
    "node",
    "cluster",
    "label:app.kubernetes.io/part-of",
)


def register(mcp: Any, cfg: MCPConfig) -> None:
    read, write = access_tools(mcp, DOMAIN)

    client = OpenCostClient(cfg.telemetry.opencost_url)

    @read
    async def cost_by(
        dimension: str = "namespace", window: str = "7d", top: int = 20
    ) -> dict[str, Any]:
        """Cost broken down by a dimension over a window.

        dimension: namespace | controller | pod | node | cluster | label:<key>
        window: OpenCost window, e.g. "24h", "7d", "30d".
        """
        payload = await client.allocation(window=window, aggregate=dimension)
        rows = summarize_allocation(payload, top=top)
        return {
            "dimension": dimension,
            "window": window,
            "total_cost": round(sum(float(r["total_cost"]) for r in rows), 4),
            "rows": rows,
        }

    @read
    async def budget_status(
        monthly_budget: float, window: str = "30d", dimension: str = "namespace"
    ) -> dict[str, Any]:
        """Compare observed spend against a stated monthly budget.

        The budget is an input, not a stored value — Adhar does not (yet) keep
        budgets in cluster state, so the caller must supply it.
        """
        payload = await client.allocation(window=window, aggregate=dimension)
        rows = summarize_allocation(payload, top=200)
        total = sum(float(r["total_cost"]) for r in rows)
        days = _window_days(window)
        projected = (total / days) * 30 if days else None
        return {
            "window": window,
            "observed_cost": round(total, 4),
            "projected_monthly": round(projected, 4) if projected is not None else None,
            "monthly_budget": monthly_budget,
            "over_budget": bool(projected is not None and projected > monthly_budget),
            "pct_of_budget": (
                round(100 * projected / monthly_budget, 1)
                if projected is not None and monthly_budget
                else None
            ),
            "top_contributors": rows[:10],
        }

    @read
    async def showback(
        window: str = "30d", label: str = "app.kubernetes.io/part-of"
    ) -> dict[str, Any]:
        """Per-team/per-app showback aggregated by a Kubernetes label."""
        payload = await client.allocation(window=window, aggregate=f"label:{label}")
        rows = summarize_allocation(payload, top=100)
        total = sum(float(r["total_cost"]) for r in rows)
        for row in rows:
            row["share_pct"] = round(100 * float(row["total_cost"]) / total, 2) if total else 0.0
        return {"window": window, "label": label, "total_cost": round(total, 4), "rows": rows}


def _window_days(window: str) -> float:
    try:
        if window.endswith("d"):
            return float(window[:-1])
        if window.endswith("h"):
            return float(window[:-1]) / 24
        if window.endswith("m"):
            return float(window[:-1]) / (24 * 60)
    except ValueError:
        pass
    return 0.0
