"""upgrade-preflight — manually triggered readiness check before an upgrade."""

from __future__ import annotations

from typing import Any

from .base import Operator


class UpgradePreflight(Operator):
    name = "upgrade-preflight"
    trigger = "manual"
    default_allowed_tools = ("resource_health", "app_status", "findings", "propose_change")

    def title(self, event: dict[str, Any]) -> str:
        target = event.get("target") or event.get("version") or "the platform"
        return f"upgrade preflight: {target}"

    def severity(self, event: dict[str, Any]) -> str:
        return "info"

    def subject(self, event: dict[str, Any]) -> dict[str, Any]:
        return {
            "target": event.get("target"),
            "version": event.get("version"),
            "namespace": event.get("namespace"),
        }

    def prompt(self, event: dict[str, Any]) -> str:
        target = event.get("target") or "the Adhar platform"
        version = event.get("version")
        namespace = event.get("namespace")
        scope = f' in namespace "{namespace}"' if namespace else ""
        version_line = f" to version {version}" if version else ""
        write = (
            "If a blocking issue has a clear config fix, call propose_change to open a PR."
            if self.policy.may_write
            else "Do not propose a change; this operator is read-only."
        )
        return f"""Run a pre-upgrade readiness check for {target}{version_line}{scope}.

1. `resource_health` — are all workloads at their desired replica count, and is
   anything crash-looping or restarting?
2. `app_status` — is the owning ArgoCD Application Synced and Healthy? An upgrade
   on top of an already-degraded app compounds the failure.
3. `findings` — are there failing Kyverno policies that the upgrade would trip?

Produce a go/no-go with a numbered list of blocking issues (each with the evidence
and the tool it came from) and a separate list of non-blocking warnings. If any tool
reports its backend is unconfigured, list that as an unknown rather than a pass.
{write}"""
