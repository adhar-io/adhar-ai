"""Event-driven operators.

Each declares a trigger, an allowed-tool set and an autonomy level (all read
from the `adhar-ai-config` ConfigMap), produces a structured `Finding`, and —
when autonomy permits — a Gitea PR via the gitops MCP write tool.
"""

from __future__ import annotations

from .alert_triage import AlertTriage
from .base import Operator, OperatorContext
from .build_failure import BuildFailure
from .cost_advisor import CostAdvisor
from .drift_explain import DriftExplain
from .upgrade_preflight import UpgradePreflight

REGISTRY: dict[str, type[Operator]] = {
    op.name: op
    for op in (AlertTriage, DriftExplain, CostAdvisor, UpgradePreflight, BuildFailure)
}

__all__ = [
    "REGISTRY",
    "AlertTriage",
    "BuildFailure",
    "CostAdvisor",
    "DriftExplain",
    "Operator",
    "OperatorContext",
    "UpgradePreflight",
]
