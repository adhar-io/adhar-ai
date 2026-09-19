"""What the platform can do, and what it cannot yet answer.

Two surfaces that compound with use.

**The capability catalogue** answers "what can I ask you?" — generated from the
live tool inventory, the agent roster, the knowledge base and the chore
catalogue rather than hand-written. A hand-written list of capabilities is wrong
within a release, and wrong in the direction that matters: it promises things
that no longer exist.

**Coverage gaps** are the loop that makes the platform teach itself. Every run
that ends badly — no grounding retrieved, no tool called, an error, an empty
answer — is recorded as a gap with the question that caused it. That queue is
what a human writes the missing runbook from, and the knowledge base then
indexes it, and the next person asking gets an answer.

The second is the more valuable of the two, and the less obvious. A platform
assistant's real failure mode is not being wrong; it is being unhelpful in a way
nobody reports, over and over, until people stop asking.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("adhar_ai.discovery")

#: Why a run counted as a gap. Ordered from most to least diagnostic.
GAP_REASONS = (
    "no-grounding",  # the knowledge base had nothing; a docs gap
    "no-tools",  # nothing was looked at; likely a routing or scope gap
    "error",  # the run failed outright
    "empty-answer",  # the model produced nothing usable
    "unhelpful",  # a human said so, via /feedback
)


@dataclass(slots=True)
class Gap:
    """One question the platform answered badly."""

    question: str
    reason: str
    at: float = field(default_factory=time.time)
    agent: str = ""
    task_id: str = ""
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "reason": self.reason,
            "agent": self.agent,
            "taskId": self.task_id,
            "detail": self.detail,
            "at": self.at,
        }


class CoverageLog:
    """Questions the platform could not answer well, and what to do about them.

    In-process and bounded. Unlike a task, a gap is a *statistic* — its value is
    in the pattern across many, and any single one is recoverable from the audit
    stream. What matters is that the pattern is visible somewhere a human will
    look, which is `/coverage`, rather than only in a log nobody greps.
    """

    def __init__(self, limit: int = 300) -> None:
        self.limit = limit
        self._gaps: list[Gap] = []

    def record(self, gap: Gap) -> None:
        self._gaps.append(gap)
        del self._gaps[: -self.limit]
        log.info("coverage gap (%s): %s", gap.reason, gap.question[:120])

    def observe_run(
        self,
        question: str,
        result: Any,
        agent: str = "",
        task_id: str = "",
        grounded: bool = True,
    ) -> Gap | None:
        """Classify a finished run, recording a gap if it went badly.

        Deliberately mechanical. A model grading its own answers would be both
        expensive and unreliable in the one direction that matters — a model
        that produced a bad answer is not well placed to notice.
        """
        reason = ""
        detail = ""
        if getattr(result, "kind", "") == "error":
            reason, detail = "error", str(getattr(result, "error", ""))[:200]
        elif not (getattr(result, "text", "") or "").strip():
            reason = "empty-answer"
        elif not grounded:
            # The most actionable of the five, and the reason it outranks
            # `no-tools`: it names a MISSING DOCUMENT. Somebody can write that.
            reason = "no-grounding"
        elif not getattr(result, "tool_calls", None):
            # Answered from the model's prior alone. Sometimes right, but for a
            # platform question it usually means the tools did not cover it.
            reason = "no-tools"

        if not reason:
            return None
        gap = Gap(
            question=question[:400],
            reason=reason,
            agent=agent,
            task_id=task_id,
            detail=detail,
        )
        self.record(gap)
        return gap

    def record_unhelpful(self, question: str, detail: str = "") -> None:
        """A human said the answer did not help. The strongest signal there is."""
        self.record(Gap(question=question[:400], reason="unhelpful", detail=detail[:200]))

    def report(self, limit: int = 50) -> dict[str, Any]:
        """What to write next, ordered by how often it has been asked for.

        Clustered on the leading words of a question, which is crude and works:
        people asking the same unanswerable thing phrase it similarly, and a
        precise clustering would cost an embedding call per gap for a list a
        human is going to read anyway.
        """
        by_reason = Counter(gap.reason for gap in self._gaps)
        clusters: dict[str, list[Gap]] = {}
        for gap in self._gaps:
            key = " ".join(gap.question.lower().split()[:5])
            clusters.setdefault(key, []).append(gap)

        ranked = sorted(clusters.items(), key=lambda kv: len(kv[1]), reverse=True)
        return {
            "gaps": len(self._gaps),
            "byReason": dict(by_reason),
            "topics": [
                {
                    "asked": len(gaps),
                    "example": gaps[-1].question,
                    "reasons": sorted({g.reason for g in gaps}),
                    "lastAsked": gaps[-1].at,
                }
                for _, gaps in ranked[:limit]
            ],
            "recent": [gap.as_dict() for gap in self._gaps[-20:]],
        }

    def snapshot(self) -> dict[str, Any]:
        return {"gaps": len(self._gaps), "byReason": dict(Counter(g.reason for g in self._gaps))}


async def capability_catalogue(
    toolbox: Any,
    agents: Any,
    knowledge: Any = None,
    chores: Any = None,
) -> dict[str, Any]:
    """Everything the platform can currently do, derived rather than declared.

    This is what a Console "what can I ask?" panel renders, and what an
    onboarding journey walks a new developer through. It is generated on every
    request because the honest answer changes: a domain whose MCP server is down
    genuinely cannot do anything right now, and saying so is more useful than
    listing what it could do if it were up.
    """
    tools_by_domain: dict[str, list[dict[str, str]]] = {}
    for tool in getattr(toolbox, "tools", {}).values():
        tools_by_domain.setdefault(tool.domain, []).append(
            {
                "name": tool.name,
                "access": tool.access,
                "description": (tool.description or "").split("\n")[0][:160],
            }
        )

    unavailable = list(getattr(toolbox, "unhealthy", []))
    catalogue: dict[str, Any] = {
        "agents": agents.describe() if agents else [],
        "domains": [
            {
                "domain": domain,
                "available": domain not in unavailable,
                "tools": sorted(tools, key=lambda t: t["name"]),
            }
            for domain, tools in sorted(tools_by_domain.items())
        ],
        "unavailable": unavailable,
        "writes": (
            "Every write tool opens a Gitea pull request and nothing else. "
            "There is no apply, sync, helm or cloud-mutation tool anywhere."
        ),
    }
    if chores is not None:
        catalogue["chores"] = chores.describe()
    if knowledge is not None:
        try:
            stats = await knowledge.stats()
            catalogue["knowledge"] = {
                "mode": stats.get("mode"),
                "documents": stats.get("documents"),
                "chunks": stats.get("chunks"),
                "sources": stats.get("sources"),
                "graph": (stats.get("graph") or {}).get("nodes"),
            }
        except Exception as exc:  # noqa: BLE001
            catalogue["knowledge"] = {"error": str(exc)}
    return catalogue


#: Questions worth putting in front of somebody who has just arrived. Chosen so
#: each demonstrates a *different* capability rather than six phrasings of one,
#: and so none of them can change anything.
ONBOARDING_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("Which applications are out of sync, and why?", "live cluster state"),
    ("What breaks if the cnpg package goes down?", "dependency graph"),
    ("How do I scaffold a new Go service?", "golden paths and documentation"),
    ("Which namespace cost the most last week?", "cost attribution"),
    ("Why is this policy blocking my deployment?", "policy explanation"),
    ("What can you change, and what can you not?", "the agent's own limits"),
)
