"""The eval suite (ADR-0024 design §12).

Runs every scenario twice over, in two modes:

* **Scripted** — always, in CI, with no key. A sensible model is simulated and
  the assertions are about the system around it: the tool was offered, the
  grounding reached the prompt, the stage was respected, the write became a pull
  request. This is what catches harness regressions, which are most of what
  actually breaks.
* **Live** — opt-in, when `ADHAR_AI_EVAL_GATEWAY` points at a real gateway. The
  same scenarios, graded on whether a real model *chose* sensible tools and
  grounded its answer. This catches prompt and model regressions the scripted
  mode cannot see, and is the nightly job §12 asks for.

Run the live mode against anything OpenAI-compatible:

    ADHAR_AI_EVAL_GATEWAY=http://127.0.0.1:18400 \\
    ADHAR_AI_EVAL_MODEL=openai/gpt-4o-mini \\
      uv run pytest tests/evals -q -m eval
"""

from __future__ import annotations

import json
import os

import pytest

from adhar_ai.gateway.types import FunctionSpec, ToolSpec
from adhar_ai.runtime.autonomy import WritePolicy
from adhar_ai.runtime.loop import GatewayClient, Session, run
from adhar_ai.runtime.toolbox import RemoteTool

from .scenarios import ALL_SCENARIOS, Scenario, grade

EVAL_GATEWAY = os.environ.get("ADHAR_AI_EVAL_GATEWAY", "")
EVAL_MODEL = os.environ.get("ADHAR_AI_EVAL_MODEL", "")
#: The floor the suite must clear. A property of the MODEL, not of the system —
#: a small free model scores well below a frontier one on the same code — so it
#: is configurable, and a team pins it to what their chosen model actually does.
EVAL_FLOOR = float(os.environ.get("ADHAR_AI_EVAL_FLOOR", "0.7"))

#: The tools every scenario can draw on, with the access tags the real servers
#: publish — so `read-only` withholds exactly what it withholds in production.
TOOLBOX_TOOLS = {
    "app_status": ("gitops", "read", "Sync and health of an ArgoCD Application."),
    "sync_status": ("gitops", "read", "Sync status across every Application."),
    "logs": ("cluster", "read", "Container logs for a pod."),
    "get_events": ("cluster", "read", "Kubernetes events for an object."),
    "describe": ("cluster", "read", "Full detail of one pod."),
    "promql": ("observability", "read", "Run a PromQL query against Prometheus."),
    "cost_by": ("cost", "read", "Cost broken down by a dimension over a window."),
    "propose_change": ("gitops", "write", "Open a Gitea pull request with file changes."),
}


class EvalToolbox:
    """The seven domains, answering with the scenario's fixed results."""

    def __init__(self, results: dict) -> None:
        self.results = results
        self.invoked: list[tuple[str, dict]] = []
        self.errors: dict[str, str] = {}
        self.servers = {"gitops": "x", "cluster": "x", "observability": "x", "cost": "x"}
        self._tools = {
            name: RemoteTool(name, domain, description, {}, access)
            for name, (domain, access, description) in TOOLBOX_TOOLS.items()
        }

    @property
    def tools(self):
        return self._tools

    @property
    def unhealthy(self) -> list[str]:
        return []

    def specs(self, allowed=(), include_writes=True):
        return [
            ToolSpec(function=FunctionSpec(name=t.name, description=t.description))
            for t in self._tools.values()
            if (not allowed or t.name in allowed) and (include_writes or not t.is_write)
        ]

    async def call(self, name, arguments):
        if name not in self._tools:
            return {"error": f"unknown tool {name!r}", "available": sorted(self._tools)}
        self.invoked.append((name, arguments))
        return self.results.get(name, {"ok": True, "tool": name})

    async def connect(self):
        return None

    async def aclose(self):
        return None


class ScriptedModel:
    """A competent model, simulated.

    It calls each tool the scenario supplies results for, once, then answers.
    That is what a sensible model does here, so a scripted run exercises the
    system's behaviour around a correct model rather than the model itself.
    """

    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.requests: list[dict] = []
        self._pending = [
            name
            for name in scenario.tool_results
            if name in TOOLBOX_TOOLS
        ]

    async def chat(self, messages, tools, tenant, model=None, max_tokens=4096, bearer=""):
        self.requests.append({"messages": list(messages), "tools": list(tools or [])})
        offered = {t.function.name for t in (tools or [])}
        while self._pending:
            name = self._pending.pop(0)
            if name in offered:
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": f"call_{name}",
                                        "type": "function",
                                        "function": {"name": name, "arguments": "{}"},
                                    }
                                ],
                            }
                        }
                    ]
                }
        return {
            "choices": [
                {"message": {"role": "assistant", "content": self.scenario.scripted_answer}}
            ]
        }

    async def aclose(self):
        return None


def session_for(scenario: Scenario) -> Session:
    return Session(
        autonomy=scenario.autonomy,
        grounding=list(scenario.grounding),
        write_policy=WritePolicy(),
        max_steps=6,
        max_tool_calls=8,
        model=EVAL_MODEL or None,
    )


# --------------------------------------------------------------------------- #
# Scripted: runs in CI, no key, deterministic
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=lambda s: s.name)
async def test_scenario_scripted(scenario: Scenario) -> None:
    toolbox = EvalToolbox(scenario.tool_results)
    model = ScriptedModel(scenario)

    result = await run(model, toolbox, session_for(scenario), scenario.prompt)
    report = grade(scenario, result)

    assert report.ok, (
        f"{scenario.name} scored {report.score:.0%}\n"
        + "\n".join(f"  FAILED  {f}" for f in report.failed)
    )


async def test_grounding_reaches_the_model() -> None:
    """A scenario's grounding must arrive in the system prompt, or every
    citation assertion in this suite is passing for the wrong reason."""
    from .scenarios import GROUNDED_POLICY_QUESTION as scenario

    model = ScriptedModel(scenario)
    await run(model, EvalToolbox({}), session_for(scenario), scenario.prompt)

    system = model.requests[0]["messages"][0].content
    assert "Grounding" in system
    assert "0024-agentic-ai-platform" in system


async def test_read_only_scenarios_are_never_offered_a_write_tool() -> None:
    """The forbids_tools check catches a write that was CALLED. This catches one
    that was merely offered, which is the earlier and more useful failure."""
    for scenario in ALL_SCENARIOS:
        if scenario.autonomy != "read-only":
            continue
        model = ScriptedModel(scenario)
        await run(model, EvalToolbox(scenario.tool_results), session_for(scenario), scenario.prompt)
        offered = {t.function.name for t in model.requests[0]["tools"]}
        assert "propose_change" not in offered, scenario.name


# --------------------------------------------------------------------------- #
# Live: opt-in, needs a real gateway
# --------------------------------------------------------------------------- #


live = pytest.mark.skipif(
    not EVAL_GATEWAY,
    reason="set ADHAR_AI_EVAL_GATEWAY (and ADHAR_AI_EVAL_MODEL) to grade a real model",
)


@live
@pytest.mark.eval
@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=lambda s: s.name)
async def test_scenario_live(scenario: Scenario, request) -> None:
    """The same scenario against a real model.

    Reported rather than hard-asserted on the citation checks: a model may
    legitimately phrase an answer without a term the scenario hoped for, and a
    suite that fails on wording is a suite people stop running. The STRUCTURAL
    expectations — a read-only run opening no pull request, a forbidden tool
    never being called — are asserted, because those are guarantees rather than
    preferences.
    """
    toolbox = EvalToolbox(scenario.tool_results)
    gateway = GatewayClient(EVAL_GATEWAY, default_model=EVAL_MODEL)
    try:
        result = await run(gateway, toolbox, session_for(scenario), scenario.prompt)
    finally:
        await gateway.aclose()

    report = grade(scenario, result)
    print(f"\n{scenario.name}: {report.score:.0%}")
    for item in report.passed:
        print(f"  PASS  {item}")
    for item in report.failed:
        print(f"  FAIL  {item}")
    print(f"  tools: {[c['tool'] for c in result.tool_calls] or 'none'}")
    print(f"  answer: {(result.text or '')[:200]}")

    # Guarantees, not preferences.
    assert result.kind != "error", result.error
    for tool in scenario.forbids_tools:
        assert tool not in {c["tool"] for c in result.tool_calls}
    if not scenario.expects_pull_request:
        assert not result.pull_requests

    request.node.eval_score = report.score


@live
@pytest.mark.eval
async def test_live_suite_scores_above_the_floor(capsys) -> None:
    """One weak answer is noise; a suite that drops below the floor is a
    regression. Deliberately a floor rather than a target: an eval suite that
    demands perfection from a nondeterministic system gets disabled."""
    toolbox_results = {s.name: s for s in ALL_SCENARIOS}
    scores = []
    for scenario in toolbox_results.values():
        gateway = GatewayClient(EVAL_GATEWAY, default_model=EVAL_MODEL)
        try:
            result = await run(
                gateway,
                EvalToolbox(scenario.tool_results),
                session_for(scenario),
                scenario.prompt,
            )
        finally:
            await gateway.aclose()
        scores.append(grade(scenario, result).score)

    mean = sum(scores) / len(scores)
    print(
        json.dumps(
            {
                "model": EVAL_MODEL or "gateway default",
                "scenarios": len(scores),
                "mean_score": round(mean, 3),
                "floor": EVAL_FLOOR,
            },
            indent=2,
        )
    )
    assert mean >= EVAL_FLOOR, (
        f"{EVAL_MODEL or 'the configured model'} scored {mean:.0%}, below the "
        f"{EVAL_FLOOR:.0%} floor. Set ADHAR_AI_EVAL_FLOOR if this model is "
        f"expected to score lower."
    )
