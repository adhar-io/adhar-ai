"""Specialized agents, the router, handoff rules and the orchestrator.

The properties worth holding here are all about *authority*, and every one of
them is a property the happy path would never notice:

* an agent's ceiling narrows the caller's stage and can never widen it;
* an agent cannot hand a task to a colleague it did not declare, nor invent one;
* a refused handoff falls back to answering instead of failing the task;
* a handoff chain terminates.

Multi-agent systems fail by passing work in circles and by quietly acquiring
permissions on the way round. Both are tested directly.
"""

from __future__ import annotations

import pytest
import yaml

from adhar_ai.runtime.agents import (
    DEFAULT_AGENTS,
    GENERALIST,
    MAX_HANDOFFS,
    AgentRegistry,
    HandoffError,
    check_handoff,
)
from adhar_ai.runtime.autonomy import RuntimeConfig, rank
from adhar_ai.runtime.orchestrator import Orchestrator, _parse_handoff, plan_first
from adhar_ai.runtime.tasks import Task, TaskStore

from .test_runtime import CONFIGMAP_YAML, FakeGateway, FakeToolbox, _answer, _tool_turn


def _config(**overrides) -> RuntimeConfig:
    data = yaml.safe_load(CONFIGMAP_YAML)
    data.setdefault("autonomy", {})
    for key, value in overrides.items():
        data["autonomy"][key] = value
    return RuntimeConfig.from_mapping(data)


def _orchestrator(gateway, toolbox=None, **kwargs) -> Orchestrator:
    store = TaskStore(dsn="")
    return Orchestrator(
        config=kwargs.pop("config", _config()),
        registry=kwargs.pop("registry", AgentRegistry()),
        toolbox=toolbox or FakeToolbox(),
        gateway=gateway,
        store=store,
        **kwargs,
    )


# ------------------------------------------------------------------ roster --


def test_the_shipped_roster_is_internally_consistent():
    """Every escalation target must exist, or a handoff is a dead end at 3am."""
    registry = AgentRegistry()
    names = set(registry.names)
    for agent in DEFAULT_AGENTS:
        assert set(agent.escalates_to) <= names, agent.name
    assert GENERALIST in names


def test_an_escalation_target_that_does_not_exist_is_rejected_at_construction():
    with pytest.raises(ValueError, match="not in the roster"):
        AgentRegistry.from_mapping(
            {"tester": {"escalatesTo": ["nobody-by-that-name"], "keywords": ["x"]}}
        )


def test_config_overrides_the_roster_but_always_keeps_a_terminus():
    registry = AgentRegistry.from_mapping(
        {"dba": {"role": "databases", "keywords": ["postgres", "cnpg"], "ceiling": "suggest"}}
    )
    assert "dba" in registry
    # Routing must terminate somewhere even if the operator listed one agent.
    assert GENERALIST in registry
    assert registry.route("the cnpg cluster is down")[0] == "dba"


# ------------------------------------------------------------------ router --


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("the checkout pod is crashlooping", "incident"),
        ("why did our spend jump last month?", "cost"),
        ("is this image vulnerable to anything?", "security"),
        ("how do I scaffold a new Go service?", "guide"),
        ("the app has been OutOfSync since the deploy", "release"),
    ],
)
def test_the_router_reaches_the_obvious_specialist(prompt, expected):
    assert AgentRegistry().route(prompt)[0] == expected


def test_inflected_platform_vocabulary_still_routes():
    """`crashloop`/`crashlooping`/`crashlooped` are the same signal.

    A whole-word match on the stem misses every inflected form, which is most
    of how people actually write.
    """
    registry = AgentRegistry()
    for phrasing in ("crashloop", "crashlooping", "crashlooped"):
        assert registry.route(f"the pod is {phrasing}")[0] == "incident"


def test_an_unrecognised_question_goes_to_the_generalist_with_no_confidence():
    agent, confidence = AgentRegistry().route("tell me a joke about badgers")
    assert agent == GENERALIST
    assert confidence == 0.0


def test_a_single_weak_match_does_not_report_certainty():
    """Undamped `best / total` is 1.0 whenever exactly one agent scored at all.

    That reads as "certain" on the flimsiest possible evidence, and anything
    downstream that gates on confidence would be gating on noise.
    """
    _, confidence = AgentRegistry().route("how do I do this?")
    assert confidence < 0.75


# ----------------------------------------------------------------- ceiling --


def test_an_agents_ceiling_narrows_an_administrators_stage():
    """Authority flows downward only.

    A read-only agent stays read-only for a caller at `scoped`, because the
    narrowing is a property of the role rather than of the request.
    """
    registry = AgentRegistry()
    orchestrator = _orchestrator(
        FakeGateway([]), registry=registry, config=_config(default="scoped")
    )
    guide = registry.get("guide")
    assert guide.ceiling == "read-only"

    task = Task(prompt="how do I scaffold a service?", agent="guide", autonomy="scoped")
    assert orchestrator.ceiling_for(task, "guide") == "read-only"


def test_no_agent_can_widen_beyond_the_configmap_default():
    registry = AgentRegistry()
    orchestrator = _orchestrator(
        FakeGateway([]), registry=registry, config=_config(default="read-only")
    )
    for name in registry.names:
        task = Task(prompt="x", agent=name, autonomy="scoped")
        assert rank(orchestrator.ceiling_for(task, name)) <= rank("read-only")


# ----------------------------------------------------------------- handoff --


def test_an_agent_cannot_hand_to_a_colleague_it_did_not_declare():
    registry = AgentRegistry()
    task = Task(prompt="x", agent="guide")
    with pytest.raises(HandoffError, match="may not hand"):
        check_handoff(registry, task, "cost", "looks like a budget question")


def test_an_agent_cannot_invent_a_colleague():
    registry = AgentRegistry()
    task = Task(prompt="x", agent="incident")
    with pytest.raises(HandoffError, match="not in the roster"):
        check_handoff(registry, task, "the-database-team", "over to you")


def test_an_agent_cannot_hand_a_task_to_itself():
    registry = AgentRegistry()
    task = Task(prompt="x", agent="incident")
    with pytest.raises(HandoffError, match="itself"):
        check_handoff(registry, task, "incident", "thinking about it")


def test_a_handoff_must_state_a_reason():
    registry = AgentRegistry()
    task = Task(prompt="x", agent=GENERALIST)
    with pytest.raises(HandoffError, match="reason"):
        check_handoff(registry, task, "incident", "   ")


def test_a_long_chain_is_refused_as_a_loop():
    registry = AgentRegistry()
    task = Task(prompt="x", agent=GENERALIST)
    task.lineage = ["a"] * MAX_HANDOFFS
    with pytest.raises(HandoffError, match="loop rather than progress"):
        check_handoff(registry, task, "incident", "one more hop")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("HANDOFF: cost — this is a budget question", ("cost", "this is a budget question")),
        ("HANDOFF: cost -- budget", ("cost", "budget")),
        ("handoff: `security` - CVE triage", ("security", "CVE triage")),
        ("HANDOFF: incident", ("incident", "no reason given")),
    ],
)
def test_a_handoff_line_is_parsed_from_the_first_line(text, expected):
    assert _parse_handoff(text) == expected


def test_mentioning_a_handoff_mid_answer_is_an_answer_not_a_transfer():
    """Otherwise a complete answer is thrown away because it used the word."""
    text = (
        "The repo-server is OOMKilled at 512Mi. Raise the limit to 1Gi.\n"
        "If that does not hold, HANDOFF: platform — capacity planning."
    )
    assert _parse_handoff(text) is None


def test_an_empty_answer_is_not_a_handoff():
    assert _parse_handoff("") is None
    assert _parse_handoff("no idea, sorry") is None


# ------------------------------------------------------------ orchestration --


async def test_a_task_runs_under_the_routed_agent_and_finishes():
    gateway = FakeGateway(
        [_tool_turn("app_status", {"name": "argocd"}), _answer("It is OOMKilled.")]
    )
    toolbox = FakeToolbox({"app_status": {"health": "Degraded"}})
    orchestrator = _orchestrator(gateway, toolbox)

    task = Task(prompt="the argocd pod is crashlooping")
    await orchestrator.execute(task)

    assert task.state == "done"
    assert task.agent == "incident"
    assert "OOMKilled" in task.result
    assert task.audit_id


async def test_an_agent_only_sees_its_own_tools():
    """A cost agent with the incident toolbox is not a cost agent."""
    gateway = FakeGateway([_answer("Spend is flat.")])
    toolbox = FakeToolbox()
    orchestrator = _orchestrator(gateway, toolbox)

    await orchestrator.execute(Task(prompt="why did our spend jump last month?"))

    offered = {spec.function.name for spec in gateway.requests[0]["tools"]}
    allowed = set(AgentRegistry().get("cost").tools)
    assert offered <= allowed
    assert "propose_change" not in offered


async def test_a_handoff_re_runs_the_task_under_the_receiving_agent():
    gateway = FakeGateway(
        [
            _answer("HANDOFF: platform — this is a capacity question, not an incident"),
            _answer("The node pool is undersized."),
        ]
    )
    orchestrator = _orchestrator(gateway)
    task = Task(prompt="the pod is crashlooping")
    await orchestrator.execute(task)

    assert task.state == "done"
    assert task.agent == "platform"
    assert task.lineage == ["incident"]
    assert "undersized" in task.result
    # A handoff is a NEW run, not a continuation: the receiving agent must not
    # inherit the previous agent's message history, which came from tools it is
    # not allowed to call.
    second = gateway.requests[1]["messages"]
    assert [m.role for m in second] == ["system", "user"]
    assert not any("capacity question" in str(m.content or "") for m in second)


async def test_the_handoff_is_recorded_in_the_plan_so_the_chain_is_readable():
    gateway = FakeGateway(
        [
            _answer("HANDOFF: security — the restart follows a policy denial"),
            _answer("Kyverno is blocking the pod."),
        ]
    )
    orchestrator = _orchestrator(gateway)
    task = Task(prompt="the pod is crashlooping")
    await orchestrator.execute(task)

    steps = " ".join(step.description for step in task.plan)
    assert "handed from incident to security" in steps
    assert "policy denial" in steps


async def test_a_refused_handoff_falls_back_to_answering_rather_than_failing():
    """Refusing is not failing.

    An agent that wants to forward somewhere it is not allowed should not kill
    the task — the generalist can answer anything, so the task lands there.
    """
    gateway = FakeGateway(
        [_answer("HANDOFF: cost — not my problem"), _answer("Here is the answer anyway.")]
    )
    orchestrator = _orchestrator(gateway)
    task = Task(prompt="how do I scaffold a new service?", agent="guide")
    await orchestrator.execute(task)

    assert task.state == "done"
    assert task.agent == GENERALIST
    assert "handoff refused" in task.error


async def test_an_endless_chain_of_handoffs_terminates():
    """Every agent forwarding forever must stop, not spin the worker."""
    gateway = FakeGateway([_answer("HANDOFF: generalist — not mine")] * 20)
    orchestrator = _orchestrator(gateway)
    task = Task(prompt="the pod is crashlooping")
    await orchestrator.execute(task)

    assert task.terminal
    assert len(gateway.requests) <= len(AgentRegistry().names) + 1


async def test_a_failed_run_fails_the_task_with_the_reason():
    gateway = FakeGateway([{"choices": [{"message": {"role": "assistant", "content": ""}}]}])
    orchestrator = _orchestrator(gateway)
    task = Task(prompt="the pod is crashlooping")
    await orchestrator.execute(task)

    assert task.state == "failed"
    assert task.error


async def test_conversation_history_reaches_a_task_raised_from_a_thread():
    from adhar_ai.runtime.sessions import ConversationStore

    conversations = ConversationStore()
    conversation = conversations.open("slack:C1:1.0", "ada")
    conversation.record("why is argocd degraded?", "its repo-server is OOMKilled", ["app_status"])

    gateway = FakeGateway([_answer("Raise the memory limit to 1Gi.")])
    orchestrator = _orchestrator(gateway, conversations=conversations)
    task = Task(
        prompt="and what should I do about it?",
        requester="ada",
        session="slack:C1:1.0",
        agent="incident",
    )
    await orchestrator.execute(task)

    sent = " ".join(str(m.content or "") for m in gateway.requests[0]["messages"])
    assert "OOMKilled" in sent, "the follow-up did not see the previous turn"
    assert "`app_status`" in sent, "the agent was not told what had already been called"
    # And the new answer joins the thread, so a third question continues.
    assert len(conversation.turns) == 2
    assert task.id in conversation.task_ids


async def test_a_chore_task_has_no_conversation_and_still_runs():
    from adhar_ai.runtime.sessions import ConversationStore

    gateway = FakeGateway([_answer("Nothing expiring.")])
    orchestrator = _orchestrator(gateway, conversations=ConversationStore())
    task = Task(prompt="find expiring certificates", trigger="chore:certificate-expiry")
    await orchestrator.execute(task)
    assert task.state == "done"


# -------------------------------------------------------------- plan-first --


async def test_a_plan_is_written_and_the_task_waits_at_a_stage_that_can_act():
    gateway = FakeGateway([_answer("1. check the chart version\n2. open a PR bumping it")])
    orchestrator = _orchestrator(gateway, config=_config(default="approve-to-apply"))
    # Routes to `release`, whose ceiling is `approve-to-apply` — an agent
    # capped at `read-only` would correctly skip planning entirely.
    task = Task(prompt="roll back the payments release", autonomy="approve-to-apply")

    assert await plan_first(orchestrator, task) is True
    assert task.state == "awaiting_approval"
    assert len(task.plan) == 2
    assert task.waiting_on
    # Planning never acts, regardless of the stage that triggered it.
    assert gateway.requests[0]["tools"]
    assert not any(
        spec.function.name == "propose_change" for spec in gateway.requests[0]["tools"]
    )


async def test_no_plan_is_written_at_a_stage_that_is_already_reviewable():
    """At `suggest` nothing lands without a pull request somebody merges, so a
    plan step would be ceremony that costs a completion."""
    gateway = FakeGateway([])
    orchestrator = _orchestrator(gateway, config=_config(default="suggest"))
    task = Task(prompt="bump the cnpg chart", autonomy="suggest")

    assert await plan_first(orchestrator, task) is False
    assert task.state == "queued"
    assert gateway.requests == []


async def test_an_unparseable_plan_still_produces_something_to_approve():
    gateway = FakeGateway([_answer("This needs no changes; the chart is current.")])
    orchestrator = _orchestrator(gateway, config=_config(default="scoped"))
    task = Task(prompt="roll back the payments release", autonomy="scoped")

    assert await plan_first(orchestrator, task) is True
    assert len(task.plan) == 1
    assert "no changes" in task.plan[0].description
