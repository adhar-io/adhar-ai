"""Who the platform helps, at which stage of their work, and with what.

An assistant with thirty-five tools and nine agents still fails the person who
does not know that any of it applies to them. The two failures look nothing
alike from the inside and identical from the outside:

* **Absent** — nobody has wired a tool for this kind of work at all, so the
  honest answer is "I cannot see that". Before `mcp/delivery.py` existed, every
  question about a failed build was this.
* **Unreachable** — the tools exist and the person never finds out. A developer
  does not know `previews` can tell them where their pull request is deployed,
  so they ask a colleague instead, and the assistant's usage says it is working
  fine.

This module is the map that makes both visible. Each `Offer` is one thing the
platform can do, for one kind of person, at one stage of their work, naming the
agent that answers it, the tools that do the answering, a question that actually
routes there, and where you say it. That is enough to be three different things
at once:

1. **A product surface.** `GET /assist` is what a console panel, a CLI
   `adhar ai what-can-you-do`, or an onboarding tour renders. Filtered by the
   caller's own groups it answers "what can I ask you, for the work I am doing
   right now", which is the question people actually have.
2. **A test.** Every offer names an agent that must exist, tools that must exist
   in the live registry, and tools that the named agent must actually hold.
   Delete a tool, rename an agent, or narrow a roster and the suite fails with
   the offer that became a lie. "Integrated" stops being a claim.
3. **A gap report.** `gaps()` lists the (persona, stage) pairs with nothing
   behind them, from each persona's own declared stages. A hole here is a
   roadmap item, stated by the code rather than remembered by a person.

The matrix is deliberately hand-written, unlike the capability catalogue next
door in `discovery.py`, which is generated. A generated matrix could only tell
you what exists; the judgement being recorded here is that *this* is the
question *this* person asks at *this* point, and that judgement is the content.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------------- #
# Stages
# --------------------------------------------------------------------------- #

#: The stages of platform work, in the order someone meets them. Not a
#: workflow engine — a vocabulary, so "assistance at every stage" is a claim
#: with a definition and `gaps()` can be computed against it.
STAGES: tuple[tuple[str, str], ...] = (
    ("learn", "Understand the platform and find the convention that already exists"),
    ("scaffold", "Create a service, an environment or an infrastructure resource"),
    ("build", "Turn a commit into an image: CI, buildpacks, the failing step"),
    ("deploy", "Get a revision to an environment: sync, promotion, preview"),
    ("operate", "Keep it running: alerts, health, logs, incidents"),
    ("optimise", "Spend less and run faster: cost attribution, waste, SLOs"),
    ("secure", "Policy, vulnerabilities, posture and compliance evidence"),
    ("evolve", "Change the platform itself: upgrades, migrations, new packages"),
)

STAGE_NAMES: tuple[str, ...] = tuple(name for name, _ in STAGES)


# --------------------------------------------------------------------------- #
# Personas
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Persona:
    """One kind of person using the platform.

    `groups` are the Keycloak groups that imply this persona, which is how the
    caller's own token picks one without anybody filling in a profile. `stages`
    is what this person legitimately needs help with — the denominator in the
    coverage calculation, so a persona claiming a stage it has no offer for
    shows up as a gap rather than being quietly dropped.
    """

    name: str
    description: str
    groups: tuple[str, ...] = ()
    stages: tuple[str, ...] = ()


PERSONAS: tuple[Persona, ...] = (
    Persona(
        name="newcomer",
        description="Just given access. Does not yet know what the platform does.",
        # No group requirement on purpose: an unauthenticated or
        # freshly-provisioned caller is pinned to read-only anyway, and the
        # person who most needs orientation is the one with the fewest claims.
        groups=(),
        stages=("learn", "scaffold"),
    ),
    Persona(
        name="developer",
        description="Ships an application on the platform.",
        groups=("platform-developer",),
        stages=("learn", "scaffold", "build", "deploy", "operate"),
    ),
    Persona(
        name="data-engineer",
        description="Runs Spark jobs, streams, warehouses and ML workloads on the platform.",
        groups=("platform-developer",),
        stages=("build", "deploy", "operate", "optimise"),
    ),
    Persona(
        name="sre",
        description="On call for what is running. Owns the incident and the rollback.",
        groups=("platform-admin",),
        stages=("deploy", "operate", "optimise"),
    ),
    Persona(
        name="platform-engineer",
        description="Owns the platform itself: packages, golden paths, upgrades.",
        groups=("platform-admin",),
        stages=("scaffold", "operate", "secure", "evolve"),
    ),
    Persona(
        name="security-engineer",
        description="Owns policy, vulnerabilities and the evidence an auditor asks for.",
        groups=("platform-admin",),
        stages=("secure", "evolve"),
    ),
    Persona(
        name="finops",
        description="Answerable for what the platform costs and who it is spent on.",
        groups=("platform-developer",),
        stages=("optimise",),
    ),
)

PERSONA_NAMES: tuple[str, ...] = tuple(p.name for p in PERSONAS)

#: Which persona a specialist agent implies. Used to refine a persona derived
#: from groups: a token says what someone MAY do, the question they just asked
#: says what they ARE doing, and the second is the better basis for "what else
#: can I ask". Agents with no entry (`guide`, `generalist`, `platform`) do not
#: identify a persona on their own.
AGENT_PERSONA: dict[str, str] = {
    "incident": "sre",
    "release": "sre",
    "delivery": "developer",
    "data": "data-engineer",
    "cost": "finops",
    "security": "security-engineer",
}


# --------------------------------------------------------------------------- #
# Offers
# --------------------------------------------------------------------------- #

#: Where a request arrives. Every one of these is a surface that exists:
#: `cli` is `adhar ai`, `chat` is `POST /chat`, `mcp` is the federated endpoint
#: an external client (Claude Code, an IDE) connects to, `slack` and `review`
#: are journey surfaces, `chore` is the scheduled proactive path, and
#: `operator` is an inbound platform event (an Alertmanager alert, an Argo CD
#: notification).
ENTRYPOINTS: tuple[str, ...] = ("cli", "chat", "mcp", "slack", "review", "chore", "operator")


@dataclass(frozen=True, slots=True)
class Offer:
    """One thing the platform can do for one person at one stage."""

    persona: str
    stage: str
    agent: str
    ask: str
    tools: tuple[str, ...] = ()
    entrypoints: tuple[str, ...] = ("cli", "chat", "mcp")
    #: What makes this worth offering rather than obvious. Shown in the console
    #: panel under the question.
    why: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "persona": self.persona,
            "stage": self.stage,
            "agent": self.agent,
            "ask": self.ask,
            "tools": list(self.tools),
            "entrypoints": list(self.entrypoints),
            "why": self.why,
        }


OFFERS: tuple[Offer, ...] = (
    # ---------------------------------------------------------- newcomer ---
    Offer(
        persona="newcomer",
        stage="learn",
        agent="guide",
        ask="What is this platform, and what should I do first?",
        tools=(),
        why=(
            "Answered from the platform's own documentation, ADRs and package "
            "catalogue rather than general Kubernetes knowledge, and cites what it used."
        ),
    ),
    Offer(
        persona="newcomer",
        stage="learn",
        agent="guide",
        ask="What golden paths exist, and which one fits a Python API?",
        tools=("search_packages", "template_params"),
        why="The templates are read live, so the answer cannot describe a path that was removed.",
    ),
    Offer(
        persona="newcomer",
        stage="scaffold",
        agent="platform",
        ask="Scaffold a new Go service called checkout in the ml project.",
        tools=("search_packages", "template_params", "scaffold"),
        why="Opens a pull request against the templates' own conventions; a human merges it.",
    ),
    # --------------------------------------------------------- developer ---
    Offer(
        persona="developer",
        stage="learn",
        agent="guide",
        ask="Where do environment variables for my service come from on this platform?",
        tools=(),
        why="Convention questions are answered from the repository, not from habit.",
    ),
    Offer(
        persona="developer",
        stage="scaffold",
        agent="platform",
        ask="Add a Postgres database to the checkout service in dev.",
        tools=("search_packages", "list_xrs", "xr_status", "propose_xr"),
        why="Composes the platform's own CompositeDatabase rather than a hand-written manifest.",
    ),
    Offer(
        persona="developer",
        stage="build",
        agent="delivery",
        ask="Why did my build fail?",
        tools=("pipeline_runs", "pipeline_run", "build_logs"),
        entrypoints=("cli", "chat", "mcp", "slack", "review"),
        why=(
            "Finds the run, names the failing task and step, and quotes the log lines "
            "that say why — rather than describing the pipeline."
        ),
    ),
    Offer(
        persona="developer",
        stage="build",
        agent="delivery",
        ask="Did the image for checkout actually update after my merge?",
        tools=("builds", "pipeline_runs"),
        why="kpack reports a lifecycle phase and a digest; both answer this exactly.",
    ),
    Offer(
        persona="developer",
        stage="deploy",
        agent="delivery",
        ask="Where is my pull request deployed, and is it healthy?",
        tools=("previews", "app_status"),
        why="Preview environments are discoverable by PR number instead of by naming convention.",
    ),
    Offer(
        persona="developer",
        stage="operate",
        agent="incident",
        ask="Why is checkout crashlooping in dev?",
        tools=("list_pods", "describe", "logs", "get_events", "insights"),
        entrypoints=("cli", "chat", "mcp", "slack"),
        why="Starts from what the cluster already knows is wrong, then narrows to your pod.",
    ),
    # ----------------------------------------------------- data-engineer ---
    Offer(
        persona="data-engineer",
        stage="build",
        agent="data",
        ask="Why did the nightly Spark job fail?",
        tools=("list_resources", "describe", "logs"),
        why=(
            "Reads sparkoperator.k8s.io/v1beta2 SparkApplications and then the driver "
            "pod's own logs, which is where the stack trace is."
        ),
    ),
    Offer(
        persona="data-engineer",
        stage="deploy",
        agent="data",
        ask="Did the dbt workflow run after the last merge?",
        tools=("list_resources", "get_events"),
        why="Argo Workflows are custom resources; the agent names the API rather than guessing.",
    ),
    Offer(
        persona="data-engineer",
        stage="operate",
        agent="data",
        ask="Which Spark applications failed in the last day, and did they share a cause?",
        tools=("list_resources", "insights", "logs"),
        entrypoints=("cli", "chat", "mcp", "chore"),
        why="Groups failures by cause instead of listing them one at a time.",
    ),
    Offer(
        persona="data-engineer",
        stage="optimise",
        agent="cost",
        ask="What do our Spark jobs cost by namespace this month?",
        tools=("cost_by", "showback"),
        why="Attributed on a dimension and a window, so the figure can be checked.",
    ),
    # --------------------------------------------------------------- sre ---
    Offer(
        persona="sre",
        stage="operate",
        agent="incident",
        ask="Triage this alert: what broke, what changed, what is the blast radius?",
        tools=("insights", "correlate", "promql", "logql", "pipeline_runs", "promotions"),
        entrypoints=("operator", "slack", "chat", "cli"),
        why=(
            "Arrives on its own from Alertmanager, and answers in that order — including "
            "what was built and promoted, which is usually what changed."
        ),
    ),
    Offer(
        persona="sre",
        stage="deploy",
        agent="release",
        ask="Did the prod promotion land, and what would a rollback cost us?",
        tools=("sync_status", "app_status", "app_diff", "promotions"),
        entrypoints=("cli", "chat", "mcp", "operator"),
        why="A rollback recommendation must name the revision it goes back to; this one does.",
    ),
    Offer(
        persona="sre",
        stage="optimise",
        agent="incident",
        ask="Is the checkout SLO burning, and what is driving it?",
        tools=("slo_burn", "promql", "correlate"),
        why="Burn rate first, then the signal that correlates with it.",
    ),
    # -------------------------------------------------- platform-engineer ---
    Offer(
        persona="platform-engineer",
        stage="scaffold",
        agent="platform",
        ask="Author a package for Temporal following this repository's conventions.",
        tools=("search_packages", "propose_change"),
        why="Follows the neighbouring packages and cites the ADR it is honouring.",
    ),
    Offer(
        persona="platform-engineer",
        stage="operate",
        agent="platform",
        ask="What is k8sgpt reporting across the platform right now?",
        tools=("insights", "list_resources", "app_status"),
        entrypoints=("cli", "chat", "mcp", "chore"),
        why="Twenty-two analyzers' findings, grouped by cause rather than by object.",
    ),
    Offer(
        persona="platform-engineer",
        stage="secure",
        agent="security",
        ask="Which policies are still in Audit that should be Enforce?",
        tools=("posture", "policy_explain"),
        why="Reads the live policy set, so the answer matches this cluster's staging.",
    ),
    Offer(
        persona="platform-engineer",
        stage="evolve",
        agent="platform",
        ask="What breaks if we upgrade this cluster, and what should we fix first?",
        tools=("list_xrs", "xr_status", "list_resources", "insights", "app_status"),
        entrypoints=("cli", "chat", "operator"),
        why=(
            "The upgrade-preflight operator runs the same question on demand and "
            "reports what it found rather than a generic checklist."
        ),
    ),
    # -------------------------------------------------- security-engineer ---
    Offer(
        persona="security-engineer",
        stage="secure",
        agent="security",
        ask="What are the critical findings right now, and which have a fix available?",
        tools=("findings", "posture", "list_resources"),
        entrypoints=("cli", "chat", "mcp", "chore"),
        why="Separates a policy violation from a vulnerability from a misconfiguration.",
    ),
    Offer(
        persona="security-engineer",
        stage="secure",
        agent="security",
        ask="Propose a scoped, expiring exception for this Kyverno denial.",
        tools=("policy_explain", "propose_exception"),
        why=(
            "The exception arrives as a pull request stating scope, expiry and the "
            "compensating control — reviewable, and never applied by the agent."
        ),
    ),
    Offer(
        persona="security-engineer",
        stage="evolve",
        agent="security",
        ask="Assemble the evidence for our pod-security controls for an auditor.",
        tools=("posture", "findings", "policy_explain"),
        why=(
            "Evidence is quoted from the live policy reports, with what is NOT "
            "covered said plainly."
        ),
    ),
    # ------------------------------------------------------------ finops ---
    Offer(
        persona="finops",
        stage="optimise",
        agent="cost",
        ask="What did team-web spend last month, and where is the waste?",
        tools=("cost_by", "showback", "budget_status", "resource_health"),
        entrypoints=("cli", "chat", "mcp", "chore"),
        why=(
            "Every figure carries its window and dimension, and a recommendation says "
            "what it saves and what it risks."
        ),
    ),
)


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #


def persona_for(groups: tuple[str, ...] | list[str] = (), agent: str = "") -> str:
    """The persona to assist, from the caller's groups and what they just asked.

    The agent wins when it maps: a token says what someone MAY do, and the
    question they just asked says what they ARE doing. Someone in
    `platform-admin` asking a cost question is being a finops person for the
    next five minutes, and the offers worth showing them are the finops ones.
    """
    if agent and agent in AGENT_PERSONA:
        return AGENT_PERSONA[agent]
    held = {g.lstrip("/") for g in groups}
    for persona in PERSONAS:
        # In roster order, so the broadest admin persona wins over the narrower
        # ones that share its group.
        if persona.groups and held.intersection(persona.groups):
            return persona.name
    return "newcomer"


def offers(persona: str = "", stage: str = "", entrypoint: str = "") -> list[Offer]:
    """The offers matching any combination of persona, stage and entrypoint."""
    rows = list(OFFERS)
    if persona:
        rows = [o for o in rows if o.persona == persona]
    if stage:
        rows = [o for o in rows if o.stage == stage]
    if entrypoint:
        rows = [o for o in rows if entrypoint in o.entrypoints]
    return rows


def gaps() -> list[dict[str, str]]:
    """(persona, stage) pairs a persona needs and nothing yet offers.

    Computed, not maintained. This is the honest half of the matrix: a platform
    assistant's worst failure is being unhelpful in a way nobody records, and
    this is where that becomes a list somebody can work through.
    """
    covered = {(o.persona, o.stage) for o in OFFERS}
    missing = []
    for persona in PERSONAS:
        for stage in persona.stages:
            if (persona.name, stage) not in covered:
                missing.append({"persona": persona.name, "stage": stage})
    return missing


def report(
    persona: str = "", stage: str = "", groups: tuple[str, ...] | list[str] = ()
) -> dict[str, Any]:
    """What `GET /assist` returns.

    With no filter it is the whole map — which is what a console panel and the
    documentation render. With the caller's groups it leads with the offers for
    the persona their own token implies, because "what can I ask you" is almost
    always meant as "what can I ask you, about my work".
    """
    chosen = persona or (persona_for(groups) if groups else "")
    selected = offers(chosen, stage)
    return {
        "stages": [{"stage": name, "description": desc} for name, desc in STAGES],
        "personas": [
            {
                "persona": p.name,
                "description": p.description,
                "groups": list(p.groups),
                "stages": list(p.stages),
            }
            for p in PERSONAS
        ],
        "persona": chosen or None,
        "offers": [o.as_dict() for o in selected],
        "count": len(selected),
        "gaps": gaps(),
        "entrypoints": {
            "cli": "adhar ai ask / adhar ai chat",
            "chat": "POST /chat on the runtime",
            "mcp": "the federated MCP endpoint (Claude Code, an IDE, an agent)",
            "slack": "POST /journeys/slack",
            "review": "POST /journeys/review — a pull-request comment",
            "chore": "a scheduled chore; see GET /chores",
            "operator": "an inbound platform event; see POST /operators/{name}/event",
        },
    }
