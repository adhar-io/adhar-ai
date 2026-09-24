"""The HTTP surface added by the agentic phases.

Tasks, approvals, agents, journeys, chores, capabilities, coverage and
multi-turn chat. The properties that matter are the ones a happy-path test
would never notice:

* a task outlives the request that created it, and reports its own state;
* approving a plan requires a write-capable credential, or the gate is decoration;
* `/chat` remembers, and one person's session is never handed to another;
* running a chore on demand is still bounded by the chore's own declaration.
"""

from __future__ import annotations

import time

import pytest
import yaml
from fastapi.testclient import TestClient

from adhar_ai.config import RuntimeEnv
from adhar_ai.runtime.agents import AgentRegistry
from adhar_ai.runtime.app import create_app
from adhar_ai.runtime.auth import AuthPolicy, Principal
from adhar_ai.runtime.autonomy import RuntimeConfig

from .test_runtime import CONFIGMAP_YAML, FakeGateway, FakeToolbox, _answer

PR = {"url": "https://gitea.adhar.localtest.me/adhar/packages/pulls/7", "number": 7}


def build(policy: AuthPolicy | None = None, turns=None, config_yaml: str = CONFIGMAP_YAML):
    toolbox = FakeToolbox({"propose_change": PR, "app_status": {"health": "Degraded"}})
    gateway = FakeGateway(turns or [_answer("all healthy")] * 20)
    cfg = RuntimeConfig.from_mapping(yaml.safe_load(config_yaml))
    app = create_app(
        cfg=cfg,
        envcfg=RuntimeEnv(),
        toolbox=toolbox,
        gateway=gateway,
        auth=policy or AuthPolicy(),
    )
    return app, toolbox, gateway


@pytest.fixture
def runtime():
    app, toolbox, gateway = build()
    with TestClient(app) as client:
        yield client, toolbox, gateway


def _settle(client: TestClient, task_id: str, timeout: float = 5.0) -> dict:
    """Poll a task to a terminal state, as a caller would."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = client.get(f"/tasks/{task_id}").json()
        if task["state"] in {"done", "failed", "cancelled", "awaiting_approval", "blocked"}:
            return task
        time.sleep(0.02)
    raise AssertionError(f"task never settled: {client.get(f'/tasks/{task_id}').json()}")


# ------------------------------------------------------------------- tasks --


def test_a_task_is_accepted_immediately_and_finishes_behind_the_caller(runtime):
    """The point of the whole primitive: the answer outlives the request."""
    client, _, _ = runtime
    accepted = client.post("/tasks", json={"prompt": "the checkout pod is crashlooping"})
    assert accepted.status_code == 200

    body = accepted.json()
    assert body["id"].startswith("task-")
    assert body["state"] in {"queued", "running", "planning"}

    settled = _settle(client, body["id"])
    assert settled["state"] == "done"
    assert settled["agent"] == "incident"
    assert settled["result"]


def test_a_task_can_be_addressed_to_a_named_agent(runtime):
    client, _, _ = runtime
    task = client.post(
        "/tasks", json={"prompt": "anything at all", "agent": "cost"}
    ).json()
    assert _settle(client, task["id"])["agent"] == "cost"


def test_reading_a_task_that_does_not_exist_is_a_404(runtime):
    client, _, _ = runtime
    response = client.get("/tasks/task-nope")
    assert response.status_code == 404
    assert "task-nope" in str(response.json())


def test_recent_tasks_are_listed(runtime):
    client, _, _ = runtime
    first = client.post("/tasks", json={"prompt": "why is argocd degraded?"}).json()
    _settle(client, first["id"])

    listing = client.get("/tasks").json()
    assert listing["count"] >= 1
    assert first["id"] in [t["id"] for t in listing["tasks"]]


def test_an_unauthenticated_task_is_pinned_to_read_only(runtime):
    """Reaching the port is not the same as being able to open a pull request."""
    client, toolbox, _ = runtime
    task = client.post(
        "/tasks", json={"prompt": "bump the chart", "autonomy": "scoped"}
    ).json()
    assert _settle(client, task["id"])["autonomy"] == "read-only"
    assert not any(name == "propose_change" for name, _ in toolbox.invoked)


# --------------------------------------------------------------- approvals --


APPROVAL_YAML = CONFIGMAP_YAML.replace("default: suggest", "default: approve-to-apply")


class ByHeader(AuthPolicy):
    """Write-capable only when the caller sends `X-Writer`.

    Lets one app answer both a writer and a reader, which is what the approval
    gate actually has to survive: the same runtime, two callers.
    """

    def principal(self, request, *, allow_webhook_token: bool = False) -> Principal:
        writer = request.headers.get("X-Writer") == "yes"
        return Principal(subject="ada" if writer else "reader", write_allowed=writer)


def test_approving_a_plan_requires_a_write_capable_credential():
    """Otherwise the approval gate is decoration.

    A task that reached `awaiting_approval` is one whose stage can change the
    platform, so releasing it is exactly as privileged as writing.
    """
    app, _, _ = build(
        policy=ByHeader(),
        turns=[_answer("1. read the chart\n2. open a PR")] + [_answer("done")] * 10,
        config_yaml=APPROVAL_YAML,
    )
    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={"prompt": "roll back the payments release"},
            headers={"X-Writer": "yes"},
        ).json()
        assert task["state"] == "awaiting_approval"
        assert task["plan"]
        assert task["waiting_on"]

        refused = client.post(f"/tasks/{task['id']}/approve", json={"approve": True})
        assert refused.status_code == 403
        assert "write-capable" in str(refused.json())
        # And it is still waiting, not quietly released.
        assert client.get(f"/tasks/{task['id']}").json()["state"] == "awaiting_approval"


def test_a_writer_releases_the_plan_and_the_task_runs():
    class Writer(AuthPolicy):
        def principal(self, request, *, allow_webhook_token: bool = False) -> Principal:
            return Principal(subject="ada", write_allowed=True)

    app, _, _ = build(
        policy=Writer(),
        turns=[_answer("1. read the chart\n2. open a PR")] + [_answer("done")] * 10,
        config_yaml=APPROVAL_YAML,
    )
    with TestClient(app) as client:
        task = client.post(
            "/tasks", json={"prompt": "roll back the payments release"}
        ).json()
        assert task["state"] == "awaiting_approval"

        approved = client.post(f"/tasks/{task['id']}/approve", json={"approve": True})
        assert approved.status_code == 200
        settled = _settle(client, task["id"])
        assert settled["state"] == "done"
        assert any("approved by ada" in step["description"] for step in settled["plan"])


def test_rejecting_a_plan_cancels_the_task_with_the_reason():
    class Writer(AuthPolicy):
        def principal(self, request, *, allow_webhook_token: bool = False) -> Principal:
            return Principal(subject="ada", write_allowed=True)

    app, _, _ = build(
        policy=Writer(),
        turns=[_answer("1. do the thing")] + [_answer("done")] * 5,
        config_yaml=APPROVAL_YAML,
    )
    with TestClient(app) as client:
        task = client.post("/tasks", json={"prompt": "roll back the release"}).json()
        rejected = client.post(
            f"/tasks/{task['id']}/approve",
            json={"approve": False, "note": "we are mid-incident"},
        ).json()
        assert rejected["state"] == "cancelled"
        # The reason survives on the plan: `waiting_on` is cleared by the
        # transition, so a rejection recorded only there would be unreadable.
        assert any("mid-incident" in step["description"] for step in rejected["plan"])
        assert any("rejected by ada" in step["description"] for step in rejected["plan"])


def test_approving_a_task_that_is_not_waiting_is_a_conflict(runtime):
    client, _, _ = runtime
    task = client.post("/tasks", json={"prompt": "why is argocd degraded?"}).json()
    _settle(client, task["id"])
    response = client.post(f"/tasks/{task['id']}/approve", json={"approve": True})
    assert response.status_code == 409


# ------------------------------------------------------------------ agents --


def test_the_roster_is_published_with_what_each_agent_may_reach(runtime):
    client, _, _ = runtime
    agents = client.get("/agents").json()["agents"]
    by_name = {a["name"]: a for a in agents}

    assert {"incident", "cost", "security", "platform", "release", "guide"} <= set(by_name)
    assert by_name["guide"]["ceiling"] == "read-only"
    assert by_name["incident"]["tools"]


def test_routing_can_be_asked_about_without_running_anything(runtime):
    """A Console needs to show "this will go to the cost agent" before sending."""
    client, _, gateway = runtime
    decision = client.post("/agents/route", json={"prompt": "why did our spend jump?"}).json()
    assert decision["agent"] == "cost"
    assert 0.0 < decision["confidence"] <= 1.0
    assert gateway.requests == [], "routing must not cost a completion"


# ---------------------------------------------------------------- journeys --


def test_a_slack_event_is_answered_in_its_own_thread(runtime):
    client, _, _ = runtime
    response = client.post(
        "/journeys/slack",
        json={"event": {"text": "<@U1> why is argocd degraded?", "channel": "C9", "ts": "1.5"}},
    )
    assert response.status_code == 200
    reply = response.json()["reply"]
    assert reply["channel"] == "C9"
    assert reply["thread_ts"] == "1.5"
    assert reply["blocks"]


def test_a_pull_request_review_declares_itself_and_cannot_merge(runtime):
    client, _, _ = runtime
    reply = client.post(
        "/journeys/pull-request",
        json={
            "repository": {"full_name": "adhar/packages"},
            "pull_request": {
                "number": 3,
                "title": "bump cnpg",
                "body": "looks fine",
                "base": {"ref": "main"},
                "head": {"ref": "bump"},
            },
        },
    ).json()["reply"]

    assert reply.startswith("### Adhar AI review")
    assert "cannot merge, apply or deploy" in reply


def test_an_unknown_surface_is_a_404_rather_than_a_guess(runtime):
    client, _, _ = runtime
    assert client.post("/journeys/carrier-pigeon", json={}).status_code == 404


def test_an_empty_payload_is_skipped_rather_than_sent_to_the_model(runtime):
    client, _, gateway = runtime
    body = client.post("/journeys/slack", json={"event": {"text": "  ", "channel": "C1"}}).json()
    assert "skipped" in body
    assert gateway.requests == []


def test_a_second_message_in_a_slack_thread_continues_the_conversation(runtime):
    """The whole point of threading the session through the journey.

    Without it every message in a thread starts from nothing, and a follow-up
    re-investigates what the agent was just told.
    """
    client, _, gateway = runtime
    event = {"channel": "C9", "ts": "1.5", "thread_ts": "1.5", "user": "U7"}
    client.post("/journeys/slack", json={"event": {**event, "text": "why is argocd degraded?"}})
    client.post("/journeys/slack", json={"event": {**event, "text": "and what do I do?"}})

    second = " ".join(str(m.content or "") for m in gateway.requests[-1]["messages"])
    assert "why is argocd degraded?" in second, "the thread's first question was forgotten"


# ------------------------------------------------------------------ chores --


def test_the_chore_catalogue_is_published_with_everything_off(runtime):
    client, _, _ = runtime
    chores = client.get("/chores").json()["chores"]
    assert len(chores) >= 7
    assert all(c["enabled"] is False for c in chores)
    assert all(c["dryRun"] is True for c in chores)


def test_a_chore_can_be_run_on_demand_within_its_own_declaration(runtime):
    client, toolbox, _ = runtime
    run = client.post("/chores/security-findings/run", json={}).json()
    assert run["chore"] == "security-findings"
    # In dry-run it reports rather than proposes, whatever the model asks for.
    assert run["proposals"] == 0
    assert not any(name == "propose_change" for name, _ in toolbox.invoked)


def test_running_a_chore_that_does_not_exist_lists_the_ones_that_do(runtime):
    client, _, _ = runtime
    response = client.post("/chores/delete-everything/run", json={})
    assert response.status_code == 404
    assert "certificate-expiry" in str(response.json())


# --------------------------------------------------------------- discovery --


def test_the_capability_catalogue_answers_what_can_i_ask(runtime):
    client, _, _ = runtime
    catalogue = client.get("/capabilities").json()

    assert catalogue["agents"]
    assert {d["domain"] for d in catalogue["domains"]} == {"gitops", "cost"}
    assert catalogue["tryAsking"]
    assert all(q["question"].endswith("?") for q in catalogue["tryAsking"])
    assert "pull request" in catalogue["writes"]


def test_coverage_starts_empty_and_records_what_went_badly(runtime):
    client, _, _ = runtime
    assert client.get("/coverage").json()["gaps"] == 0

    # This runtime has no documents indexed, so a question it answers from the
    # model's prior alone is recorded as a documentation gap.
    client.post("/chat", json={"prompt": "how do I rotate the signing key?"})
    report = client.get("/coverage").json()
    assert report["gaps"] == 1
    assert report["topics"][0]["reasons"] == ["no-grounding"]
    assert "signing key" in report["topics"][0]["example"]


def test_a_human_downvote_becomes_a_coverage_gap(runtime):
    client, _, _ = runtime
    client.post(
        "/feedback",
        json={"chunk_ids": [], "helpful": False, "question": "how do I rotate the signing key?"},
    )
    report = client.get("/coverage").json()
    assert report["byReason"]["unhelpful"] == 1
    assert "signing key" in report["topics"][0]["example"]


# -------------------------------------------------------------------- chat --


def test_chat_remembers_the_previous_exchange_in_the_same_session(runtime):
    client, _, gateway = runtime
    client.post("/chat", json={"prompt": "why is argocd degraded?", "session": "s1"})
    client.post("/chat", json={"prompt": "and what should I do?", "session": "s1"})

    second = " ".join(str(m.content or "") for m in gateway.requests[-1]["messages"])
    assert "why is argocd degraded?" in second
    assert "all healthy" in second, "the previous answer was not replayed"


def test_chat_without_a_session_stays_stateless(runtime):
    client, _, gateway = runtime
    client.post("/chat", json={"prompt": "first question"})
    client.post("/chat", json={"prompt": "second question"})

    second = " ".join(str(m.content or "") for m in gateway.requests[-1]["messages"])
    assert "first question" not in second


def test_two_sessions_never_see_each_others_history(runtime):
    client, _, gateway = runtime
    client.post("/chat", json={"prompt": "what is in the payments namespace?", "session": "a"})
    client.post("/chat", json={"prompt": "anything else?", "session": "b"})

    second = " ".join(str(m.content or "") for m in gateway.requests[-1]["messages"])
    assert "payments" not in second


def test_chat_reports_the_session_back_so_a_ui_can_show_the_depth(runtime):
    client, _, _ = runtime
    body = client.post("/chat", json={"prompt": "why?", "session": "s1"}).json()
    assert body["session"]["id"] == "s1"
    assert body["session"]["turns"] == 1


# ----------------------------------------------------------------- healthz --


def test_healthz_reports_every_new_subsystem(runtime):
    """An operator must be able to see whether tasks are durable.

    A runtime reporting "ok" while losing every task on rollout is the failure
    this line exists to prevent.
    """
    client, _, _ = runtime
    health = client.get("/healthz").json()

    assert health["tasks"]["durable"] is False
    assert "no database" in health["tasks"]["storage"]
    assert health["tasks"]["workers"] >= 1
    assert "incident" in health["agents"]
    assert health["chores"]["enabled"] == []
    assert health["conversations"]["conversations"] == 0
    assert health["coverage"]["gaps"] == 0


# ------------------------------------------------- chat goes through agents --
#
# These are the regression tests for the defect that made the whole roster
# invisible in practice: `/chat` ran a generic assistant holding every tool,
# while the specialists existed, were listed at `/agents`, were routable at
# `/agents/route`, and were reached by nothing a person actually used.


def test_chat_routes_to_a_specialist_and_says_which(runtime):
    client, _, _ = runtime
    body = client.post("/chat", json={"prompt": "the checkout pod is crashlooping"}).json()
    assert body["agent"] == "incident"
    assert body["routing_confidence"] > 0


def test_chat_offers_only_the_routed_agents_tools(runtime):
    """The defect this file exists for.

    Before, every question was answered with the whole read surface, so a cost
    question could read pod logs and a how-to question could diff an app.
    """
    client, _, gateway = runtime
    client.post("/chat", json={"prompt": "why did our spend jump last month?"})

    offered = {spec.function.name for spec in gateway.requests[-1]["tools"]}
    allowed = set(AgentRegistry().get("cost").tools)
    assert offered, "no tools were offered at all"
    assert offered <= allowed, f"cost agent was handed {offered - allowed}"


def test_two_different_questions_get_two_different_tool_sets(runtime):
    """A roster that offers the same tools to everyone is not a roster."""
    client, _, gateway = runtime
    client.post("/chat", json={"prompt": "the checkout pod is crashlooping"})
    incident_tools = {s.function.name for s in gateway.requests[-1]["tools"]}
    client.post("/chat", json={"prompt": "how do I scaffold a new Go service?"})
    guide_tools = {s.function.name for s in gateway.requests[-1]["tools"]}

    assert incident_tools != guide_tools
    assert guide_tools < incident_tools or not (guide_tools & incident_tools)


def test_chat_carries_the_agents_own_instructions(runtime):
    client, _, gateway = runtime
    client.post("/chat", json={"prompt": "the checkout pod is crashlooping"})
    sent = " ".join(str(m.content or "") for m in gateway.requests[-1]["messages"])
    assert "You are on call" in sent, "the agent's role instructions never reached the model"


def test_chat_applies_the_agents_ceiling_not_just_the_callers():
    """A `read-only` agent stays read-only for a caller who may write.

    The caller here is write-capable and the ConfigMap default is `suggest`,
    so without the agent's own ceiling this run would reach `suggest` and be
    offered the PR-opening tools. The narrowing belongs to the ROLE.
    """
    app, _, gateway = build(policy=ByHeader())
    with TestClient(app) as client:
        writer = {"X-Writer": "yes"}
        # A writer asking an `incident` question does reach `suggest`.
        incident = client.post(
            "/chat", json={"prompt": "the checkout pod is crashlooping"}, headers=writer
        ).json()
        assert incident["agent"] == "incident"
        assert incident["autonomy"] == "suggest"
        assert any(
            s.function.name == "propose_change" for s in gateway.requests[-1]["tools"]
        )

        # The same writer asking the `guide` does not.
        guide = client.post(
            "/chat",
            json={"prompt": "how do I scaffold a new Go service?", "autonomy": "scoped"},
            headers=writer,
        ).json()
        assert guide["agent"] == "guide"
        assert guide["autonomy"] == "read-only"
        assert not any(
            s.function.name == "propose_change" for s in gateway.requests[-1]["tools"]
        )


def test_chat_can_address_a_specialist_directly(runtime):
    client, _, _ = runtime
    body = client.post("/chat", json={"prompt": "anything at all", "agent": "security"}).json()
    assert body["agent"] == "security"
    # Naming an agent is a choice, not a guess.
    assert body["routing_confidence"] == 1.0


def test_chat_still_meters_against_the_caller_not_the_agent(runtime):
    """Budgets are kept per caller.

    Routing must not silently re-label every interactive run as `agent:cost`,
    which would pool every user's spend into one bucket per role.
    """
    client, _, gateway = runtime
    client.post("/chat", json={"prompt": "why did our spend jump?", "session": "s-budget"})
    assert gateway.requests[-1]["tenant"] == "s-budget"
    assert not gateway.requests[-1]["tenant"].startswith("agent:")


def test_chat_honours_a_requested_model(runtime):
    """The model name is agentgateway's routing key, so dropping it misroutes."""
    client, _, gateway = runtime
    client.post("/chat", json={"prompt": "why is it down?", "model": "anthropic/claude-x"})
    assert gateway.requests[-1]["model"] == "anthropic/claude-x"


def test_a_handoff_in_chat_is_followed_rather_than_shown_to_the_user(runtime):
    """Otherwise the person reads the literal string `HANDOFF: security — …`."""
    app, _, gateway = build(
        turns=[
            _answer("HANDOFF: security — this restart follows a policy denial"),
            _answer("Kyverno is blocking the pod."),
        ]
    )
    with TestClient(app) as client:
        body = client.post("/chat", json={"prompt": "the pod is crashlooping"}).json()
    assert "HANDOFF" not in body["text"]
    assert body["agent"] == "security"
    assert "Kyverno" in body["text"]


def test_chat_grounding_is_scoped_to_what_the_agent_should_read(runtime):
    """`knowledge_kinds` was declared on every agent and read by nothing."""
    client, _, _ = runtime
    asked: list[tuple] = []

    class Knowledge:
        async def grounding_with_ids(self, query, k=5, kinds=()):
            asked.append((query, kinds))
            return ["# a passage"], [7]

    client.app.state.orchestrator.knowledge = Knowledge()
    body = client.post("/chat", json={"prompt": "is this image vulnerable to a CVE?"}).json()

    assert body["agent"] == "security"
    assert asked and asked[-1][1] == AgentRegistry().get("security").knowledge_kinds
    assert body["grounding_chunk_ids"] == [7]
    assert body["grounded_on"] == ["a passage"]


def test_chat_records_one_turn_and_one_coverage_gap_not_two(runtime):
    """Both the route and the orchestrator used to be able to record these."""
    client, _, _ = runtime
    client.post("/chat", json={"prompt": "how do I rotate the signing key?", "session": "s1"})
    body = client.post("/chat", json={"prompt": "and the other one?", "session": "s1"}).json()

    assert body["session"]["turns"] == 2
    assert client.get("/coverage").json()["gaps"] == 2


def test_chat_does_not_leave_a_task_row_behind(runtime):
    """An answer inside the request is not a task.

    One row per chat message would bury the long-running work `/tasks` exists
    to show, and would fill a table with 30-day retention at chat volume.
    """
    client, _, _ = runtime
    for i in range(5):
        client.post("/chat", json={"prompt": f"question {i} about pods"})
    assert client.get("/tasks").json()["count"] == 0

    # A real task still lands.
    task = client.post("/tasks", json={"prompt": "why is argocd degraded?"}).json()
    _settle(client, task["id"])
    assert client.get("/tasks").json()["count"] == 1


def test_a_chat_session_records_no_task_id_it_cannot_resolve(runtime):
    """A dangling id in the session is worse than no id."""
    client, _, _ = runtime
    client.post("/chat", json={"prompt": "why is it down?", "session": "s-dangle"})
    body = client.post("/chat", json={"prompt": "and now?", "session": "s-dangle"}).json()
    for task_id in body["session"]["tasks"]:
        assert client.get(f"/tasks/{task_id}").status_code == 200, task_id
    assert body["session"]["tasks"] == []
