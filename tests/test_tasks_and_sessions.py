"""Durable tasks, the queue that runs them, and conversation memory.

The task state machine is the piece with the most to get wrong: a task that can
go from `done` back to `running` because some path forgot to check is a task
whose history nobody can trust. So the transitions are tested as a machine —
every legal move and a representative illegal one — rather than only along the
happy path.

The store is tested in its in-memory mode, which is not a shortcut: that mode is
what `docker compose` and a bare `adhar-ai runtime` actually run, and it is the
one whose behaviour is easiest to get subtly wrong because nothing enforces it.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from adhar_ai.runtime.sessions import (
    DEFAULT_TURNS,
    MAX_TURN_CHARS,
    ConversationStore,
)
from adhar_ai.runtime.tasks import (
    TERMINAL,
    TRANSITIONS,
    PlanStep,
    Task,
    TaskError,
    TaskQueue,
    TaskStore,
)

# ------------------------------------------------------------ state machine --


def test_every_state_is_reachable_and_every_terminal_state_is_a_dead_end():
    """The machine must be connected and must actually terminate.

    A state nothing can reach is dead code pretending to be policy; a terminal
    state with an outgoing edge is not terminal.
    """
    reachable = {"queued"} | {to for froms in TRANSITIONS.values() for to in froms}
    assert reachable == set(TRANSITIONS), "every declared state must be reachable"
    for state in TERMINAL:
        assert TRANSITIONS[state] == frozenset(), f"{state} must be terminal"


def test_a_finished_task_cannot_be_restarted():
    task = Task(prompt="why is argocd degraded?")
    task.transition("running")
    task.transition("done")
    with pytest.raises(TaskError) as exc:
        task.transition("running")
    # The message has to name the states, because this surfaces in a 409 body
    # and "invalid transition" tells the caller nothing actionable.
    assert "done" in str(exc.value) and "running" in str(exc.value)


def test_approval_gate_holds_the_task_and_records_what_it_waits_on():
    task = Task(prompt="bump the cnpg chart")
    task.transition("planning")
    task.transition("awaiting_approval", reason="3 steps proposed; approve to run")
    assert task.waiting_on == "3 steps proposed; approve to run"
    assert not task.terminal
    # Releasing it clears the reason — a running task waiting on nothing must
    # not still advertise a pending decision.
    task.transition("running", reason="approved")
    assert task.waiting_on == ""


def test_cancel_is_reachable_from_every_non_terminal_state():
    for state in set(TRANSITIONS) - TERMINAL:
        assert "cancelled" in TRANSITIONS[state], f"{state} cannot be cancelled"


def test_title_is_derived_and_truncated_but_the_prompt_is_kept_whole():
    prompt = "why is " + "everything " * 40 + "broken?"
    task = Task(prompt=prompt)
    assert len(task.title) <= 73
    assert task.title.endswith("…")
    assert task.prompt == prompt


def test_lineage_records_each_agent_that_held_the_task():
    task = Task(prompt="costs are up")
    task.hand_to("cost", "routed")
    task.hand_to("platform", "needs workload detail")
    task.hand_to("cost", "back with the answer")
    assert task.lineage == ["cost", "platform"]
    assert task.agent == "cost"


def test_a_task_round_trips_through_its_dict_form():
    """`as_dict` is the JSONB payload, so anything it drops is lost on restart."""
    task = Task(
        prompt="p",
        agent="incident",
        requester="ada",
        session="slack:C1:170.5",
        parent_id="task-parent",
    )
    task.plan = [PlanStep(description="look at the pods", done=True)]
    task.artifacts = [{"url": "https://gitea/pulls/1"}]
    task.transition("running")

    restored = Task.from_dict(task.as_dict())
    assert restored.as_dict() == task.as_dict()
    assert restored.session == "slack:C1:170.5"
    assert restored.plan[0].done is True


# -------------------------------------------------------------------- store --


async def test_in_memory_store_reports_honestly_that_it_is_not_durable():
    store = TaskStore(dsn="")
    assert await store.prepare() is False
    assert store.durable is False
    # `/healthz` renders this, and an operator reading "ok" while tasks are
    # being lost on every rollout is the failure this string exists to prevent.
    assert "no database" in store.status


async def test_saved_tasks_come_back_and_recent_is_newest_first():
    store = TaskStore(dsn="")
    first = Task(prompt="one")
    second = Task(prompt="two")
    await store.save(first)
    second.updated_at = first.updated_at + 1
    await store.save(second)

    assert (await store.get(first.id)).prompt == "one"
    assert await store.get("task-nope") is None
    assert [t.prompt for t in await store.recent(10)][0] == "two"


async def test_resumable_returns_interrupted_work_and_ignores_finished_work():
    store = TaskStore(dsn="")
    running = Task(prompt="mid-flight")
    running.transition("running")
    done = Task(prompt="finished")
    done.transition("running")
    done.transition("done")
    await store.save(running)
    await store.save(done)

    resumable = await store.resumable()
    assert [t.id for t in resumable] == [running.id]


async def test_memory_store_is_bounded_so_a_busy_runtime_cannot_grow_forever():
    store = TaskStore(dsn="", max_memory=5)
    for i in range(20):
        await store.save(Task(prompt=f"task {i}"))
    assert len(await store.recent(100)) == 5


# -------------------------------------------------------------------- queue --


async def _drain(queue: TaskQueue, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = queue.snapshot()
        if snapshot["queued"] == 0 and snapshot["running"] == 0:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"queue did not drain: {queue.snapshot()}")


async def test_the_queue_runs_submitted_work_behind_the_caller():
    store = TaskStore(dsn="")
    ran: list[str] = []

    async def runner(task: Task) -> None:
        ran.append(task.id)
        task.transition("running")
        task.transition("done")
        await store.save(task)

    queue = TaskQueue(store, runner, workers=2)
    await queue.start()
    try:
        task = await queue.submit(Task(prompt="go"))
        await _drain(queue)
        assert ran == [task.id]
        assert (await store.get(task.id)).state == "done"
    finally:
        await queue.close(grace=1.0)


async def test_a_runner_that_raises_fails_the_task_rather_than_killing_the_worker():
    """A crash in one task must not take the pool down.

    This is the difference between one bad question and a runtime that silently
    stops processing work — a failure mode nothing would report, because the
    queue keeps accepting and never runs anything again.
    """
    store = TaskStore(dsn="")
    calls: list[str] = []

    async def runner(task: Task) -> None:
        calls.append(task.prompt)
        if task.prompt == "boom":
            raise RuntimeError("the gateway exploded")
        task.transition("running")
        task.transition("done")
        await store.save(task)

    queue = TaskQueue(store, runner, workers=1)
    await queue.start()
    try:
        bad = await queue.submit(Task(prompt="boom"))
        await _drain(queue)
        failed = await store.get(bad.id)
        assert failed.state == "failed"
        assert "the gateway exploded" in failed.error

        # The same worker must still take the next task.
        good = await queue.submit(Task(prompt="fine"))
        await _drain(queue)
        assert (await store.get(good.id)).state == "done"
        assert calls == ["boom", "fine"]
    finally:
        await queue.close(grace=1.0)


async def test_a_draining_queue_refuses_new_work_instead_of_dropping_it():
    store = TaskStore(dsn="")

    async def runner(task: Task) -> None:
        return None

    queue = TaskQueue(store, runner, workers=1)
    await queue.start()
    await queue.close(grace=0.1)
    with pytest.raises(TaskError):
        await queue.submit(Task(prompt="too late"))
    assert queue.snapshot()["accepting"] is False


async def test_resume_re_enqueues_interrupted_work_and_says_so_on_the_task():
    store = TaskStore(dsn="")
    interrupted = Task(prompt="was running when the pod went")
    interrupted.transition("running")
    await store.save(interrupted)

    done: list[Task] = []

    async def runner(task: Task) -> None:
        done.append(task)
        task.transition("done")
        await store.save(task)

    queue = TaskQueue(store, runner, workers=1)
    await queue.start()
    try:
        assert await queue.resume() == 1
        await _drain(queue)
        assert [t.id for t in done] == [interrupted.id]
        # A resumed run starts over, and saying so beats pretending the restart
        # never happened.
        assert "restart" in done[0].error
    finally:
        await queue.close(grace=1.0)


# ------------------------------------------------------------ conversations --


def test_a_conversation_replays_prior_turns_as_alternating_messages():
    store = ConversationStore()
    conversation = store.open("slack:C1:1.0", "ada")
    conversation.record("why is argocd degraded?", "its repo-server is OOMKilled", ["app_status"])

    history = conversation.history()
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[0]["content"] == "why is argocd degraded?"
    assert "OOMKilled" in history[1]["content"]


def test_only_the_last_few_turns_are_kept():
    store = ConversationStore(turns=DEFAULT_TURNS)
    conversation = store.open("s", "ada")
    for i in range(DEFAULT_TURNS + 3):
        conversation.record(f"question {i}", f"answer {i}")
    assert len(conversation.turns) == DEFAULT_TURNS
    # The oldest fell off; the newest is still there.
    assert "question 0" not in str(conversation.history())
    assert f"question {DEFAULT_TURNS + 2}" in str(conversation.history())


def test_a_long_answer_is_truncated_rather_than_dropped():
    store = ConversationStore()
    conversation = store.open("s", "ada")
    conversation.record("q", "x" * (MAX_TURN_CHARS * 3))
    replayed = conversation.history()[1]["content"]
    assert len(replayed) <= MAX_TURN_CHARS + 8
    assert replayed.endswith("[…]")


def test_the_context_note_names_what_was_already_called():
    store = ConversationStore()
    conversation = store.open("s", "ada")
    conversation.record("why?", "because", ["app_status", "get_events"])
    conversation.record("and now?", "still", ["app_status"])
    note = conversation.context_note()
    assert "`app_status`" in note and "`get_events`" in note
    # Deduplicated: the same tool named twice reads as two different tools.
    assert note.count("app_status") == 1


def test_a_conversation_with_no_tool_calls_adds_no_note():
    store = ConversationStore()
    conversation = store.open("s", "ada")
    conversation.record("hello", "hi")
    assert conversation.context_note() == ""


def test_a_different_requester_never_inherits_someone_elses_window():
    """The one property here that is a security property, not a UX one.

    A guessed or collided session id must not hand one person's cluster detail
    to another, so a mismatched requester gets a fresh conversation.
    """
    store = ConversationStore()
    ada = store.open("shared-id", "ada")
    ada.record("what is in the payments namespace?", "three secrets and a job")

    grace = store.open("shared-id", "grace")
    assert grace.history() == []
    assert "payments" not in str(grace.history())


def test_a_silent_conversation_expires():
    store = ConversationStore(ttl=60.0)
    conversation = store.open("s", "ada")
    conversation.record("q", "a")
    conversation.updated_at = time.time() - 120

    assert store.get("s", "ada") is None
    assert store.open("s", "ada").history() == []


def test_the_store_is_bounded_and_evicts_the_least_recently_used():
    store = ConversationStore(max_conversations=3)
    for i in range(3):
        store.open(f"s{i}", "ada").record("q", "a")
    store.open("s0", "ada")  # touch the oldest, making s1 the coldest
    store.open("s3", "ada")

    assert store.get("s1", "ada") is None
    assert store.get("s0", "ada") is not None
    assert store.snapshot()["conversations"] == 3


def test_expire_sweeps_stale_conversations():
    store = ConversationStore(ttl=30.0)
    store.open("old", "ada").updated_at = time.time() - 600
    store.open("new", "ada")
    assert store.expire() == 1
    assert store.get("new", "ada") is not None
