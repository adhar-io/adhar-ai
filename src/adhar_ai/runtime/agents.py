"""Specialized agents: who does what, and who they can pass it to.

The four "operators" were prompt variations on one loop — same tool set, same
ceiling, same knowledge. That is not specialization, it is four system prompts.
A specialized agent is defined by what it *cannot* do as much as by what it can:

    tools       a cost question must not reach the pull-request tools
    ceiling     a guide agent is read-only whatever the ConfigMap says
    knowledge   a security question should weight runbooks over meeting notes
    escalation  and when it is out of its depth, it must know who to ask

All four are **declarative**, read from `adhar-ai-config`. Once the registry
exists, an agent is configuration rather than code — which is the point, because
the roster is a thing platform teams will want to change without a release.

## Routing

A router picks the agent. It is deliberately **lexical, not a model call**:
spending a completion to decide which agent should spend a completion doubles
the latency and the cost of every request, and gets it wrong in ways that are
hard to debug. Keyword scoring over each agent's declared vocabulary is
predictable, instant, free, and when it is wrong the fix is a word in a config
file rather than a prompt-tuning session.

An unroutable request goes to the generalist, which is the honest outcome.

## Handoff

Handoff is explicit, reason-bearing and depth-capped. Multi-agent systems fail by
passing work in circles while every participant believes someone else owns it, so
a transfer records who held the task, why it moved, and refuses once the chain
gets long enough to look like a loop.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Any

from .autonomy import LADDER, rank

log = logging.getLogger("adhar_ai.agents")

#: How many times one task may change hands. Four is enough for a real chain
#: (incident finds a cert problem, hands to security, who needs a config change,
#: who hands to platform) and short enough that a loop is caught on its first
#: revolution.
MAX_HANDOFFS = 4

#: Added to the denominator when reporting routing confidence, so one weak
#: keyword hit does not come back as certainty. Tuned so a single match reports
#: around 0.3 and three agreeing matches around 0.6.
DAMPING = 2.0

#: The agent a request goes to when nothing scores. Not a failure mode — most
#: questions are general, and routing them to a specialist would be worse.
GENERALIST = "generalist"


@dataclass(slots=True)
class AgentSpec:
    """One agent's authority, in full."""

    name: str
    #: One line, shown in the capability catalogue and used by the router as
    #: additional vocabulary.
    role: str = ""
    #: Appended to the shared system prompt. Describes the agent's job, not its
    #: constraints — constraints are enforced structurally, and a prompt that
    #: restates them invites the model to treat them as negotiable.
    instructions: str = ""
    #: Tools this agent may use. Empty means every tool its ceiling allows,
    #: which is what the generalist gets.
    tools: tuple[str, ...] = ()
    #: The highest rung this agent may reach, whatever the ConfigMap default is.
    ceiling: str = "read-only"
    #: Knowledge kinds to prefer. Empty means no preference.
    knowledge_kinds: tuple[str, ...] = ()
    #: Words that route to this agent.
    keywords: tuple[str, ...] = ()
    #: Agents this one may hand to. Empty means none — an agent that cannot
    #: escalate is a deliberate choice, not an oversight.
    escalates_to: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        rank(self.ceiling)  # a typo must not silently widen authority

    def ceiling_for(self, requested: str) -> str:
        """The stage this agent may actually run at.

        Narrows, never widens. An agent declared `read-only` stays read-only
        even if the ConfigMap default is `scoped` and the caller is an admin.
        """
        return LADDER[min(rank(requested), rank(self.ceiling))]

    def may_hand_to(self, other: str) -> bool:
        return other in self.escalates_to

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "tools": list(self.tools) or "all permitted by ceiling",
            "ceiling": self.ceiling,
            "knowledgeKinds": list(self.knowledge_kinds),
            "escalatesTo": list(self.escalates_to),
        }


#: The shipped roster. Deliberately opinionated: these are the six jobs an
#: internal developer platform actually has people for. Overridden wholesale by
#: `agents:` in `adhar-ai-config`.
DEFAULT_AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec(
        name="incident",
        role="Triage an alert, correlate signals, find the cause, propose the fix.",
        instructions=(
            "You are on call. Establish what is broken, what changed, and what the "
            "blast radius is, in that order. Prefer evidence over inference: name the "
            "pod, the event, the log line. If the cause is outside your tools, say so "
            "and name what you would need."
        ),
        tools=(
            "app_status", "sync_status", "app_diff", "list_pods", "describe",
            "get_events", "logs", "resource_health", "promql", "logql",
            "correlate", "slo_burn", "propose_change",
        ),
        ceiling="suggest",
        knowledge_kinds=("runbook", "incident", "finding"),
        keywords=(
            "alert", "down", "degraded", "failing", "crash", "crashloop", "outage",
            "error", "broken", "restart", "unhealthy", "incident", "why is",
            "not working", "timeout", "5xx", "oom",
        ),
        escalates_to=("security", "platform", "release"),
    ),
    AgentSpec(
        name="cost",
        role="Attribute spend, find waste, right-size workloads, report showback.",
        instructions=(
            "Answer with figures and the dimension they were measured on. A cost claim "
            "without a window and a breakdown is not an answer. When you recommend a "
            "change, say what it saves and what it risks."
        ),
        tools=("cost_by", "budget_status", "showback", "resource_health", "promql"),
        ceiling="suggest",
        knowledge_kinds=("doc", "adr", "finding"),
        keywords=(
            "cost", "spend", "budget", "expensive", "showback", "chargeback",
            "savings", "waste", "rightsize", "right-size", "bill", "usage",
            "idle", "overprovisioned",
        ),
        escalates_to=("platform",),
    ),
    AgentSpec(
        name="security",
        role="Explain a policy, chase a finding, assemble compliance evidence.",
        instructions=(
            "Distinguish a policy violation from a vulnerability from a misconfiguration; "
            "they have different owners and different fixes. When proposing an exception, "
            "state the scope, the expiry and what compensating control remains."
        ),
        tools=(
            "findings", "policy_explain", "posture", "propose_exception",
            "describe", "list_pods", "get_events",
        ),
        ceiling="approve-to-apply",
        knowledge_kinds=("adr", "runbook", "incident"),
        keywords=(
            "security", "policy", "kyverno", "cve", "vulnerab", "compliance",
            "rbac", "permission", "forbidden", "denied", "exception", "posture",
            "audit", "soc2", "cis", "certificate", "secret",
        ),
        escalates_to=("platform", "incident"),
    ),
    AgentSpec(
        name="platform",
        role="Scaffold from a golden path, author a package, wire a dependency.",
        instructions=(
            "Follow the conventions already in the repository rather than general "
            "practice — cite the package or ADR you are following. A scaffold that does "
            "not look like its neighbours is a scaffold someone has to rewrite."
        ),
        tools=(
            "search_packages", "template_params", "scaffold", "propose_change",
            "list_xrs", "xr_status", "propose_xr", "app_status",
        ),
        ceiling="suggest",
        knowledge_kinds=("adr", "package", "doc"),
        keywords=(
            "scaffold", "create", "new service", "golden path", "package", "template",
            "provision", "bootstrap", "add a", "set up", "install", "crossplane",
            "composite", "xr", "onboard a service",
        ),
        escalates_to=("security", "release"),
    ),
    AgentSpec(
        name="release",
        role="Watch a promotion, explain a failed sync, recommend a rollback.",
        instructions=(
            "Be specific about which environment and which revision. A recommendation to "
            "roll back must name what it rolls back to and what is lost by doing it."
        ),
        tools=("sync_status", "app_status", "app_diff", "resource_health", "propose_change"),
        ceiling="approve-to-apply",
        knowledge_kinds=("runbook", "adr", "doc"),
        keywords=(
            "deploy", "release", "promote", "promotion", "rollback", "roll back",
            "sync", "outofsync", "out of sync", "drift", "kargo", "argocd",
            "canary", "revision", "version",
        ),
        escalates_to=("incident", "platform"),
    ),
    AgentSpec(
        name="guide",
        role="Answer how-to questions and onboard developers. Touches nothing.",
        instructions=(
            "You are teaching. Prefer the platform's own documentation over general "
            "knowledge, cite it, and give the exact command or path. If the platform "
            "does not document it, say so — an invented convention is worse than a gap."
        ),
        tools=("search_packages", "template_params"),
        ceiling="read-only",
        knowledge_kinds=("doc", "adr", "runbook", "package"),
        keywords=(
            "how do i", "how to", "what is", "where is", "explain", "documentation",
            "getting started", "onboard", "tutorial", "guide", "convention",
            "should i", "can i", "difference between",
        ),
        escalates_to=(),
    ),
    AgentSpec(
        name=GENERALIST,
        role="Anything that does not clearly belong to a specialist.",
        instructions="",
        tools=(),
        ceiling="suggest",
        keywords=(),
        escalates_to=("incident", "cost", "security", "platform", "release", "guide"),
    ),
)


class AgentRegistry:
    """The roster, and the router over it."""

    def __init__(self, agents: tuple[AgentSpec, ...] = DEFAULT_AGENTS) -> None:
        self._agents = {a.name: a for a in agents}
        if GENERALIST not in self._agents:
            # Routing must always terminate somewhere, so a roster that omits
            # the generalist gets the shipped one — but with its escalation
            # list narrowed to colleagues this roster actually has. The shipped
            # generalist escalates to all six specialists, and injecting that
            # unchanged into a two-agent roster would fail validation and take
            # the runtime down at startup over a default the operator never
            # asked for.
            shipped = next(a for a in DEFAULT_AGENTS if a.name == GENERALIST)
            self._agents[GENERALIST] = replace(
                shipped,
                escalates_to=tuple(t for t in shipped.escalates_to if t in self._agents),
            )
        self._validate()

    def _validate(self) -> None:
        """An escalation target that does not exist is a dead end at runtime."""
        for agent in self._agents.values():
            for target in agent.escalates_to:
                if target not in self._agents:
                    raise ValueError(
                        f"agent {agent.name!r} escalates to {target!r}, which is not "
                        f"in the roster ({sorted(self._agents)})"
                    )

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> AgentRegistry:
        """Build from the `agents:` block of `adhar-ai-config`.

        An empty or absent block keeps the shipped roster, so a platform that
        never thinks about agents still gets six sensible ones.
        """
        if not data:
            return cls()
        agents = []
        for name, raw in data.items():
            raw = raw or {}
            agents.append(
                AgentSpec(
                    name=name,
                    role=str(raw.get("role", "")),
                    instructions=str(raw.get("instructions", "")),
                    tools=tuple(raw.get("tools") or ()),
                    ceiling=str(raw.get("ceiling", "read-only")),
                    knowledge_kinds=tuple(raw.get("knowledgeKinds") or ()),
                    keywords=tuple(str(k).lower() for k in (raw.get("keywords") or ())),
                    escalates_to=tuple(raw.get("escalatesTo") or ()),
                )
            )
        return cls(tuple(agents))

    def __contains__(self, name: str) -> bool:
        return name in self._agents

    def get(self, name: str) -> AgentSpec:
        return self._agents.get(name) or self._agents[GENERALIST]

    @property
    def names(self) -> list[str]:
        return sorted(self._agents)

    def route(self, prompt: str) -> tuple[str, float]:
        """Pick an agent for a request. Returns `(name, confidence)`.

        Lexical rather than a model call, deliberately: spending a completion to
        decide who should spend a completion doubles latency and cost on every
        request, and misroutes in ways that are hard to debug. A keyword miss is
        fixed by adding a word to a config file.
        """
        text = f" {prompt.lower()} "
        scores: dict[str, float] = {}
        for agent in self._agents.values():
            if agent.name == GENERALIST:
                continue
            score = 0.0
            for keyword in agent.keywords:
                if len(keyword.split()) > 1:
                    # A phrase is a stronger signal than a word; "how do i" says
                    # far more about intent than "how".
                    if keyword in text:
                        score += 2.0
                # PREFIX, not whole word. Platform vocabulary inflects
                # constantly — crashloop/crashlooping, deploy/deploying/deployed,
                # provision/provisioned — and a word-boundary match on the stem
                # misses every inflected form, which is most real questions.
                elif re.search(rf"\b{re.escape(keyword)}", text):
                    score += 1.0
            if score:
                scores[agent.name] = score

        if not scores:
            return GENERALIST, 0.0
        best = max(scores, key=lambda k: scores[k])
        total = sum(scores.values())
        # Damped by DAMPING so a single weak match does not report certainty.
        # An unqualified `best / total` gives 1.0 whenever exactly one agent
        # scored at all, including when it scored once on a generic phrase.
        confidence = scores[best] / (total + DAMPING)
        return best, round(confidence, 3)

    def describe(self) -> list[dict[str, Any]]:
        """The capability catalogue, for `/agents` and for discovery."""
        return [self._agents[name].as_dict() for name in self.names]


@dataclass(slots=True)
class Handoff:
    """One agent passing a task to another."""

    to: str
    reason: str
    from_agent: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"from": self.from_agent, "to": self.to, "reason": self.reason}


class HandoffError(RuntimeError):
    """A transfer that must not happen."""


def check_handoff(registry: AgentRegistry, task: Any, to: str, reason: str) -> Handoff:
    """Validate a transfer before it happens.

    Three refusals, each for a failure mode multi-agent systems reliably have:
    a depth cap for circular passing, a declared-target check so an agent cannot
    invent a colleague, and a reason requirement so the chain is readable
    afterwards by someone who was not there.
    """
    current = registry.get(task.agent) if task.agent else registry.get(GENERALIST)

    if len(task.lineage) >= MAX_HANDOFFS:
        raise HandoffError(
            f"task {task.id} has already changed hands {len(task.lineage)} times "
            f"({' -> '.join([*task.lineage, task.agent])}); refusing to continue a "
            "chain this long, which is usually a loop rather than progress"
        )
    if to not in registry:
        raise HandoffError(
            f"{current.name!r} tried to hand to {to!r}, which is not in the roster "
            f"({registry.names})"
        )
    if to == current.name:
        raise HandoffError(f"{current.name!r} tried to hand the task to itself")
    if not current.may_hand_to(to):
        raise HandoffError(
            f"{current.name!r} may not hand to {to!r}; it escalates to "
            f"{list(current.escalates_to) or 'nobody'}"
        )
    if not reason.strip():
        raise HandoffError("a handoff must state its reason, so the chain can be read later")

    return Handoff(to=to, reason=reason.strip(), from_agent=current.name)
