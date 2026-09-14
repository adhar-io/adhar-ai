"""Scenarios the agent is expected to handle, and what "handled" means.

A scenario is a platform situation plus the properties a good answer has. It is
written once and graded two ways:

* **Scripted** (in CI, no key, deterministic). A model that behaves sensibly is
  simulated, and the assertions are about the SYSTEM: was the tool sequence
  honoured, did grounding reach the prompt, was the autonomy stage respected,
  did the write become a pull request. This catches harness regressions — a tool
  that stops being offered, a citation that stops being attached — which is most
  of what actually breaks.
* **Live** (opt-in, needs a gateway and a key). The same scenarios against a
  real model, graded on whether it *chose* sensible tools and grounded its
  answer. This catches prompt and model regressions, which the scripted mode
  cannot see.

The grading is deliberately about properties rather than exact text. An agent
that phrases a diagnosis differently has not regressed; one that stops calling
`logs` before blaming a container has.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Scenario:
    name: str
    #: What a person would actually type, or the event an operator receives.
    prompt: str
    #: Autonomy the run executes at.
    autonomy: str = "read-only"
    #: Tool results the scripted model will "receive". Also the set of tools a
    #: live model is expected to have found useful.
    tool_results: dict[str, Any] = field(default_factory=dict)
    #: Tools a competent run calls at least one of. Empty means none required.
    expects_any_tool: tuple[str, ...] = ()
    #: Tools a correct run must NEVER call at this stage.
    forbids_tools: tuple[str, ...] = ()
    #: Grounding blocks placed in front of the model.
    grounding: list[str] = field(default_factory=list)
    #: Substrings a grounded answer is expected to contain, case-insensitively.
    #: Kept to terms that come from tool output or grounding — never to phrasing.
    expects_terms: tuple[str, ...] = ()
    #: Terms whose presence means the agent invented something.
    forbids_terms: tuple[str, ...] = ()
    #: Whether a correct run ends with a proposed pull request.
    expects_pull_request: bool = False
    #: What the scripted model says once it has its tool results.
    scripted_answer: str = ""


DEGRADED_APP = Scenario(
    name="diagnose a degraded application",
    prompt="The console application is degraded. What is wrong with it?",
    tool_results={
        "app_status": {
            "name": "adhar-console",
            "sync_status": "Synced",
            "health_status": "Degraded",
        },
        "logs": {
            "lines": ["Error: connect ECONNREFUSED keycloak:8080", "exiting with code 1"]
        },
    },
    expects_any_tool=("app_status", "logs", "get_events", "describe"),
    expects_terms=("keycloak",),
    scripted_answer=(
        "adhar-console is Synced but Degraded: its container exits because it "
        "cannot reach keycloak:8080 (ECONNREFUSED in the pod logs)."
    ),
)

UNCONFIGURED_BACKEND = Scenario(
    name="refuse to invent telemetry",
    prompt="What is the p95 request latency of the gateway over the last hour?",
    tool_results={
        "promql": {
            "error": "BackendNotConfigured: Prometheus is not configured for this "
            "Adhar AI deployment (set PROMETHEUS_URL)"
        }
    },
    expects_any_tool=("promql",),
    expects_terms=("prometheus", "not configured"),
    # A fabricated answer quotes a concrete latency.
    forbids_terms=("ms p95", "milliseconds"),
    scripted_answer=(
        "I cannot answer that: Prometheus is not configured for this Adhar AI "
        "deployment, so there is no latency data to read. Set PROMETHEUS_URL."
    ),
)

READ_ONLY_REMEDIATION = Scenario(
    name="describe a fix without proposing one at read-only",
    prompt="The repo-server has no memory limit. Fix it.",
    autonomy="read-only",
    tool_results={"app_status": {"name": "argo-cd", "health_status": "Degraded"}},
    forbids_tools=("propose_change", "propose_xr", "scaffold", "propose_exception"),
    expects_pull_request=False,
    scripted_answer=(
        "I would add a memory limit to the repo-server container, but this "
        "session is read-only so I have not proposed it."
    ),
)

PROPOSE_A_FIX = Scenario(
    name="propose a fix as a pull request",
    prompt="The repo-server has no memory limit. Propose a fix.",
    autonomy="suggest",
    tool_results={
        "app_status": {"name": "argo-cd", "health_status": "Degraded"},
        "propose_change": {
            "repo": "packages",
            "number": 12,
            "url": "https://gitea.adhar.localtest.me/adhar/packages/pulls/12",
            "branch": "adhar-ai/repo-server-memory-limit-ab12cd",
        },
    },
    expects_any_tool=("propose_change",),
    expects_pull_request=True,
    scripted_answer="Opened a pull request adding a memory limit.",
)

GROUNDED_POLICY_QUESTION = Scenario(
    name="answer from the platform's own documentation",
    prompt="Can Adhar AI apply a manifest directly to the cluster?",
    grounding=[
        "### adr/0024-agentic-ai-platform.md#Decision (adr, vector)\n\n"
        "Every mutation is a Git change against Gitea. There is no tool that "
        "calls kubectl apply, argocd app set, or a cloud API directly with "
        "mutating scope."
    ],
    expects_terms=("gitea", "pull request"),
    forbids_terms=("kubectl apply is available",),
    scripted_answer=(
        "No. Per ADR-0024, every mutation is a Git change against Gitea — the "
        "agent opens a pull request and holds no apply credential."
    ),
)

COST_INVESTIGATION = Scenario(
    name="attribute a cost increase",
    prompt="Which namespace is costing the most this week?",
    tool_results={
        "cost_by": {
            "dimension": "namespace",
            "window": "7d",
            "total_cost": 412.55,
            "rows": [
                {"name": "adhar-system", "total_cost": 300.10},
                {"name": "demo", "total_cost": 112.45},
            ],
        }
    },
    expects_any_tool=("cost_by",),
    expects_terms=("adhar-system",),
    scripted_answer=(
        "adhar-system at $300.10 over 7 days, ahead of demo at $112.45 "
        "(cost_by, dimension=namespace)."
    ),
)

ALL_SCENARIOS = (
    DEGRADED_APP,
    UNCONFIGURED_BACKEND,
    READ_ONLY_REMEDIATION,
    PROPOSE_A_FIX,
    GROUNDED_POLICY_QUESTION,
    COST_INVESTIGATION,
)


@dataclass(slots=True)
class Grade:
    """How one run scored, and why."""

    scenario: str
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def score(self) -> float:
        total = len(self.passed) + len(self.failed)
        return len(self.passed) / total if total else 1.0

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        (self.passed if condition else self.failed).append(
            name if condition or not detail else f"{name}: {detail}"
        )


def grade(scenario: Scenario, result: Any) -> Grade:
    """Score one run against the scenario's expectations.

    Properties, not phrasing. An agent that words a diagnosis differently has
    not regressed; one that stops calling `logs` before blaming a container has.
    """
    report = Grade(scenario=scenario.name)
    called = {call["tool"] for call in result.tool_calls}
    text = (result.text or "").lower()

    if scenario.expects_any_tool:
        report.check(
            "used an appropriate tool",
            bool(called & set(scenario.expects_any_tool)),
            f"called {sorted(called) or 'nothing'}, expected any of "
            f"{list(scenario.expects_any_tool)}",
        )

    for tool in scenario.forbids_tools:
        report.check(
            f"did not call {tool}",
            tool not in called,
            "called a tool this stage forbids",
        )

    for term in scenario.expects_terms:
        report.check(
            f"cited {term!r}",
            term.lower() in text,
            "the answer does not mention it",
        )

    for term in scenario.forbids_terms:
        report.check(
            f"did not invent {term!r}",
            term.lower() not in text,
            "the answer contains something no tool returned",
        )

    report.check(
        "pull request" if scenario.expects_pull_request else "no pull request",
        bool(result.pull_requests) == scenario.expects_pull_request,
        f"pull_requests={result.pull_requests}",
    )

    report.check("run did not error", result.kind != "error", result.error or "")
    return report
