"""Scheduled, narrow, individually-enablable automation.

This is where an agentic platform earns its keep and where it does the most
damage when wrong. A chore that opens twelve pull requests on a Monday morning
gets the whole layer switched off, and it deserves to.

So a chore is deliberately constrained:

* **Named and individually enabled.** Never a category flag. Turning on
  "automation" is not a decision anyone can reason about; turning on
  `certificate-expiry` is.
* **Capped.** `max_proposals` bounds what one run can open. The cap is the
  difference between a helpful morning and an unreviewable flood.
* **Dry-run by default.** A chore reports what it *would* do until somebody
  turns it live. You should be able to watch a chore be right for a fortnight
  before it is allowed to act.
* **Scoped.** It runs at the autonomy the ConfigMap gives it, which for
  unattended work means `scoped` and therefore the narrower write allow-list —
  empty until an operator enumerates it.
* **Idempotent.** Re-running must not duplicate a proposal, so a chore states
  how to recognise its own previous work.

A chore is not a cron job that calls an LLM. It is a question the platform asks
itself on a schedule, with a bounded licence to answer.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("adhar_ai.chores")


@dataclass(slots=True)
class ChoreSpec:
    """One named piece of recurring work."""

    name: str
    #: What it looks for, in one line. Shown in the catalogue.
    summary: str = ""
    #: The question put to the agent. Written as an instruction to investigate
    #: and propose, never as an instruction to act — acting is the autonomy
    #: stage's decision, not the prompt's.
    prompt: str = ""
    #: Tools it may use. Narrow: a chore with the whole toolbox is a chore whose
    #: behaviour nobody can predict.
    tools: tuple[str, ...] = ()
    #: Which specialized agent runs it.
    agent: str = "generalist"
    #: Cron-ish interval in seconds. Chores are daily or weekly things.
    interval: float = 86400.0
    #: OFF until somebody turns it on, one at a time.
    enabled: bool = False
    #: Reports rather than proposes. The default, so a chore can be watched
    #: being right before it is allowed to act.
    dry_run: bool = True
    #: Hard ceiling on pull requests from one run.
    max_proposals: int = 1
    #: How a repeat run recognises work it already proposed. Matched against
    #: open pull-request titles.
    dedupe_hint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "summary": self.summary,
            "agent": self.agent,
            "enabled": self.enabled,
            "dryRun": self.dry_run,
            "intervalSeconds": self.interval,
            "maxProposals": self.max_proposals,
            "tools": list(self.tools),
        }


#: The shipped catalogue. Every one is OFF and in dry-run: shipping automation
#: enabled by default would be making a decision that belongs to the operator.
DEFAULT_CHORES: tuple[ChoreSpec, ...] = (
    ChoreSpec(
        name="certificate-expiry",
        summary="Certificates approaching expiry, before the outage rather than during it.",
        prompt=(
            "Find TLS certificates in the platform that expire within 21 days. For each, "
            "report the secret, the issuer and the days remaining. If a renewal is "
            "configured but not progressing, say what is blocking it."
        ),
        tools=("list_pods", "describe", "get_events", "findings", "promql"),
        agent="security",
        interval=86400.0,
        dedupe_hint="certificate",
    ),
    ChoreSpec(
        name="drift-reconciliation",
        summary="Applications that have been OutOfSync long enough to be deliberate.",
        prompt=(
            "List applications that have been OutOfSync for more than 24 hours. For each, "
            "explain what differs and whether the cluster or the repository is correct. "
            "Propose a change only where the repository is clearly behind."
        ),
        tools=("sync_status", "app_status", "app_diff", "propose_change"),
        agent="release",
        interval=21600.0,
        dedupe_hint="drift",
    ),
    ChoreSpec(
        name="orphaned-resources",
        summary="PersistentVolumeClaims, Services and Secrets nothing references.",
        prompt=(
            "Find resources in the platform namespace that nothing appears to reference: "
            "bound PVCs with no pod, Services with no endpoints, Secrets no workload "
            "mounts. Report each with the evidence that it is unreferenced. Be "
            "conservative — a resource used by something you cannot see is not orphaned."
        ),
        tools=("list_pods", "describe", "resource_health"),
        agent="platform",
        interval=604800.0,
        dedupe_hint="orphan",
    ),
    ChoreSpec(
        name="failing-scorecards",
        summary="Turn a scorecard grade into the specific change that lifts it.",
        prompt=(
            "Find services scoring below C on the platform scorecards. For the worst one, "
            "identify the single highest-value criterion it fails and propose the concrete "
            "change that would fix it."
        ),
        tools=("search_packages", "app_status", "resource_health", "propose_change"),
        agent="platform",
        interval=604800.0,
        dedupe_hint="scorecard",
    ),
    ChoreSpec(
        name="cost-outliers",
        summary="Namespaces whose spend moved sharply against their own baseline.",
        prompt=(
            "Compare this week's cost by namespace against the previous week. Report any "
            "namespace that moved more than 30 percent, with both figures. Name a likely "
            "cause from workload changes where you can see one."
        ),
        tools=("cost_by", "budget_status", "showback", "resource_health"),
        agent="cost",
        interval=604800.0,
        dedupe_hint="cost",
    ),
    ChoreSpec(
        name="security-findings",
        summary="Policy violations and vulnerabilities that have gone unaddressed.",
        prompt=(
            "List failing policy reports and high-severity findings older than seven days. "
            "Group them by the underlying cause rather than listing every instance — ten "
            "pods failing one policy is one problem."
        ),
        tools=("findings", "policy_explain", "posture"),
        agent="security",
        interval=86400.0,
        dedupe_hint="finding",
    ),
    ChoreSpec(
        name="runbook-rot",
        summary="Documented procedures that reference things which no longer exist.",
        prompt=(
            "Check the platform runbooks against live state. Report procedures that "
            "reference a package, namespace or resource that is no longer present. These "
            "are the procedures that fail at three in the morning."
        ),
        tools=("search_packages", "list_pods", "sync_status"),
        agent="platform",
        interval=604800.0,
        dedupe_hint="runbook",
    ),
)


@dataclass
class ChoreRun:
    """One execution, for the catalogue and for `/healthz`."""

    chore: str
    at: float = field(default_factory=time.time)
    task_id: str = ""
    proposals: int = 0
    skipped: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "chore": self.chore,
            "at": self.at,
            "taskId": self.task_id,
            "proposals": self.proposals,
            **({"skipped": self.skipped} if self.skipped else {}),
            **({"error": self.error} if self.error else {}),
        }


class ChoreRegistry:
    """The catalogue, its schedule, and what each one last did."""

    def __init__(self, chores: tuple[ChoreSpec, ...] = DEFAULT_CHORES) -> None:
        self._chores = {c.name: c for c in chores}
        self._last_run: dict[str, float] = {}
        self.history: list[ChoreRun] = []

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> ChoreRegistry:
        """Overlay `chores:` from the ConfigMap onto the shipped catalogue.

        An overlay rather than a replacement: an operator enabling one chore
        should not have to restate the other six, and a new chore shipped in a
        later release should arrive disabled rather than absent.
        """
        registry = cls()
        for name, raw in (data or {}).items():
            raw = raw or {}
            existing = registry._chores.get(name)
            if existing is None:
                log.warning("config enables unknown chore %r; ignoring", name)
                continue
            registry._chores[name] = ChoreSpec(
                name=name,
                summary=existing.summary,
                prompt=str(raw.get("prompt") or existing.prompt),
                tools=tuple(raw.get("tools") or existing.tools),
                agent=str(raw.get("agent") or existing.agent),
                interval=float(raw.get("intervalSeconds") or existing.interval),
                enabled=bool(raw.get("enabled", existing.enabled)),
                dry_run=bool(raw.get("dryRun", existing.dry_run)),
                max_proposals=int(raw.get("maxProposals") or existing.max_proposals),
                dedupe_hint=existing.dedupe_hint,
            )
        return registry

    @property
    def enabled(self) -> list[ChoreSpec]:
        return [c for c in self._chores.values() if c.enabled]

    def get(self, name: str) -> ChoreSpec | None:
        return self._chores.get(name)

    def due(self, now: float | None = None) -> list[ChoreSpec]:
        """Enabled chores whose interval has elapsed."""
        now = now or time.time()
        return [
            chore
            for chore in self.enabled
            if now - self._last_run.get(chore.name, 0.0) >= chore.interval
        ]

    def record(self, run: ChoreRun) -> None:
        self._last_run[run.chore] = run.at
        self.history.append(run)
        # Bounded: this is a status surface, not an audit log. The audit stream
        # is the record, and it is built to be long.
        del self.history[:-100]

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                **chore.as_dict(),
                "lastRun": self._last_run.get(chore.name),
            }
            for chore in sorted(self._chores.values(), key=lambda c: c.name)
        ]

    def snapshot(self) -> dict[str, Any]:
        return {
            "catalogue": len(self._chores),
            "enabled": sorted(c.name for c in self.enabled),
            "live": sorted(c.name for c in self.enabled if not c.dry_run),
            "recent": [r.as_dict() for r in self.history[-5:]],
        }
