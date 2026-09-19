"""The durable unit of agentic work.

An agent run used to be an HTTP request: it began when the connection opened and
died when it closed. That single fact blocked almost everything an agentic
platform needs. You cannot hand off what does not outlive the request. You cannot
pause for a human approval without holding a socket open. You cannot provision a
cluster and report back in twenty minutes. You cannot survive the rollout that
interrupts you.

A `Task` is that missing primitive:

    an identifier, an owner, a state machine, a plan, a transcript, a parent

It is **queued rather than called**, so the caller gets an id immediately and the
work happens behind them. It **persists**, so a restart resumes rather than
loses. It can **pause**, so a human can approve a plan before it runs. And it has
a **parent**, so one agent handing work to another produces a chain you can
follow afterwards.

The state machine is small on purpose:

    queued ──► planning ──► awaiting_approval ──► running ──► done
       │           │                │                │  │
       │           └────────────────┴────────────────┘  ├──► blocked
       └──► cancelled                                   └──► failed

`awaiting_approval` is the rung that makes high-autonomy work reviewable: the
agent writes a plan, stops, and waits. Nothing about the plan is trusted until a
human says so, and because the task is durable that human can take an hour.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("adhar_ai.tasks")

#: Terminal states. A task in one of these is never picked up again.
TERMINAL = frozenset({"done", "failed", "cancelled"})

#: Every legal transition. Enforced rather than documented, because a state
#: machine that is only documented becomes a set of `if` statements that
#: disagree with each other within a month.
TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"planning", "running", "cancelled", "failed"}),
    "planning": frozenset({"awaiting_approval", "running", "failed", "cancelled"}),
    "awaiting_approval": frozenset({"running", "cancelled", "failed"}),
    "running": frozenset({"done", "failed", "blocked", "cancelled", "awaiting_approval"}),
    "blocked": frozenset({"running", "cancelled", "failed"}),
    "done": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


class TaskError(RuntimeError):
    """An illegal transition, or a task that cannot be acted on."""


@dataclass(slots=True)
class PlanStep:
    """One step of a plan the agent wrote before acting.

    A reactive loop cannot be reviewed before it acts — by the time you see what
    it did, it has done it. An explicit plan is what makes
    `approve-to-apply` mean something a human can actually approve.
    """

    description: str
    tool: str = ""
    done: bool = False
    result: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "tool": self.tool,
            "done": self.done,
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PlanStep:
        return cls(
            description=str(raw.get("description", "")),
            tool=str(raw.get("tool", "")),
            done=bool(raw.get("done", False)),
            result=str(raw.get("result", "")),
        )


@dataclass(slots=True)
class Task:
    """One unit of agentic work, durable across restarts."""

    id: str = field(default_factory=lambda: f"task-{secrets.token_hex(6)}")
    #: What was asked for, in the requester's words.
    prompt: str = ""
    #: Short human label, derived from the prompt unless given.
    title: str = ""
    #: Which specialized agent holds it. Empty means the router decides.
    agent: str = ""
    state: str = "queued"
    autonomy: str = "read-only"
    #: Who asked. `anonymous` for an unauthenticated caller.
    requester: str = "anonymous"
    #: What started it: `chat`, an operator name, or a chore name.
    trigger: str = "chat"
    #: The conversation this belongs to — a Slack thread, a pull request, a
    #: Console session. Lets a follow-up ask "how is that going?" and lets the
    #: answer land back in the thread that asked for it.
    session: str = ""
    #: Set when this task was handed over by another. The chain is what makes
    #: "the agent did it" a useful sentence again once there are six agents.
    parent_id: str = ""
    #: Every agent that has held this task, in order.
    lineage: list[str] = field(default_factory=list)
    plan: list[PlanStep] = field(default_factory=list)
    #: The answer, once there is one.
    result: str = ""
    #: Pull requests, findings, anything the run produced.
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    #: Why it is blocked or awaiting approval — what a human needs to decide.
    waiting_on: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    #: Correlates the task with the audit stream and its traces.
    audit_id: str = ""

    def __post_init__(self) -> None:
        if not self.title:
            self.title = self.prompt[:72] + ("…" if len(self.prompt) > 72 else "")

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    def transition(self, to: str, *, reason: str = "") -> None:
        """Move to a new state, or refuse.

        Refusing is the point. A task that can go from `done` back to `running`
        because some code path forgot is a task whose history cannot be trusted.
        """
        allowed = TRANSITIONS.get(self.state, frozenset())
        if to not in allowed:
            raise TaskError(
                f"task {self.id} cannot go from {self.state!r} to {to!r}; "
                f"legal next states are {sorted(allowed) or 'none — it is terminal'}"
            )
        log.info("task %s: %s -> %s%s", self.id, self.state, to, f" ({reason})" if reason else "")
        self.state = to
        self.waiting_on = reason if to in {"awaiting_approval", "blocked"} else ""
        self.updated_at = time.time()

    def hand_to(self, agent: str, reason: str) -> None:
        """Record that another agent has taken this task on."""
        if self.agent and self.agent != agent:
            self.lineage.append(self.agent)
        self.agent = agent
        self.waiting_on = ""
        self.updated_at = time.time()
        log.info("task %s handed to %s: %s", self.id, agent, reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "prompt": self.prompt,
            "agent": self.agent,
            "state": self.state,
            "autonomy": self.autonomy,
            "requester": self.requester,
            "trigger": self.trigger,
            "session": self.session,
            "parent_id": self.parent_id,
            "lineage": list(self.lineage),
            "plan": [step.as_dict() for step in self.plan],
            "result": self.result,
            "artifacts": list(self.artifacts),
            "error": self.error,
            "waiting_on": self.waiting_on,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "audit_id": self.audit_id,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Task:
        task = cls(
            id=str(raw.get("id") or f"task-{secrets.token_hex(6)}"),
            prompt=str(raw.get("prompt", "")),
            title=str(raw.get("title", "")),
            agent=str(raw.get("agent", "")),
            state=str(raw.get("state", "queued")),
            autonomy=str(raw.get("autonomy", "read-only")),
            requester=str(raw.get("requester", "anonymous")),
            trigger=str(raw.get("trigger", "chat")),
            session=str(raw.get("session", "")),
            parent_id=str(raw.get("parent_id", "")),
            lineage=list(raw.get("lineage") or []),
            plan=[PlanStep.from_dict(s) for s in (raw.get("plan") or [])],
            result=str(raw.get("result", "")),
            artifacts=list(raw.get("artifacts") or []),
            error=str(raw.get("error", "")),
            waiting_on=str(raw.get("waiting_on", "")),
            created_at=float(raw.get("created_at") or time.time()),
            updated_at=float(raw.get("updated_at") or time.time()),
            audit_id=str(raw.get("audit_id", "")),
        )
        return task


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS {table} (
  id          TEXT PRIMARY KEY,
  state       TEXT NOT NULL,
  agent       TEXT NOT NULL DEFAULT '',
  requester   TEXT NOT NULL DEFAULT '',
  parent_id   TEXT NOT NULL DEFAULT '',
  created_at  DOUBLE PRECISION NOT NULL,
  updated_at  DOUBLE PRECISION NOT NULL,
  payload     JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS {table}_state ON {table} (state, created_at);
CREATE INDEX IF NOT EXISTS {table}_recent ON {table} (updated_at DESC);
CREATE INDEX IF NOT EXISTS {table}_parent ON {table} (parent_id);
"""

UPSERT_SQL = """
INSERT INTO {table} (id, state, agent, requester, parent_id, created_at, updated_at, payload)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
ON CONFLICT (id) DO UPDATE SET
  state = EXCLUDED.state, agent = EXCLUDED.agent, parent_id = EXCLUDED.parent_id,
  updated_at = EXCLUDED.updated_at, payload = EXCLUDED.payload
"""

SELECT_ONE_SQL = "SELECT payload FROM {table} WHERE id = %s"
SELECT_RECENT_SQL = "SELECT payload FROM {table} ORDER BY updated_at DESC LIMIT %s"
SELECT_STATE_SQL = (
    "SELECT payload FROM {table} WHERE state = ANY(%s) ORDER BY created_at ASC LIMIT %s"
)
PRUNE_SQL = "DELETE FROM {table} WHERE updated_at < %s AND state = ANY(%s)"

DEFAULT_RETENTION_DAYS = 30


class TaskStore:
    """Postgres-backed task history, degrading to memory.

    The in-memory fallback is not a toy: `docker compose` and a bare
    `adhar-ai runtime` have no database, and a task that lives only for the
    process is still far more useful than an HTTP request that lives only for
    the connection. What is lost without a database is resumption across a
    restart — which is stated in `/healthz` rather than silently assumed.
    """

    def __init__(
        self,
        dsn: str,
        table: str = "agent_task",
        retention_days: int = DEFAULT_RETENTION_DAYS,
        max_memory: int = 500,
    ) -> None:
        self.dsn = dsn
        if not table.replace("_", "").isalnum():
            raise ValueError(f"invalid task table name {table!r}")
        self.table = table
        self.retention_days = retention_days
        self.durable = False
        self.status = "in-memory (no database)" if not dsn else "pending"
        self._memory: OrderedDict[str, Task] = OrderedDict()
        self._max_memory = max_memory

    async def _connect(self) -> Any:
        import psycopg

        return await psycopg.AsyncConnection.connect(self.dsn)

    async def prepare(self) -> bool:
        if not self.dsn:
            return False
        try:
            async with await self._connect() as conn, conn.cursor() as cur:
                await cur.execute(SCHEMA_SQL.format(table=self.table))
                cutoff = time.time() - self.retention_days * 86400
                await cur.execute(
                    PRUNE_SQL.format(table=self.table), (cutoff, sorted(TERMINAL))
                )
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            self.durable = False
            self.status = f"in-memory ({type(exc).__name__}: {exc})"
            log.warning("task store unavailable, tasks will not survive a restart: %s", exc)
            return False
        self.durable = True
        self.status = f"durable ({self.table})"
        return True

    async def save(self, task: Task) -> None:
        task.updated_at = time.time()
        self._memory[task.id] = task
        self._memory.move_to_end(task.id)
        while len(self._memory) > self._max_memory:
            self._memory.popitem(last=False)
        if not self.durable:
            return
        try:
            async with await self._connect() as conn, conn.cursor() as cur:
                await cur.execute(
                    UPSERT_SQL.format(table=self.table),
                    (
                        task.id,
                        task.state,
                        task.agent,
                        task.requester,
                        task.parent_id,
                        task.created_at,
                        task.updated_at,
                        json.dumps(task.as_dict()),
                    ),
                )
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            # The task is still in memory and still running. Losing the durable
            # copy degrades resumption, it must not fail the work.
            log.warning("could not persist task %s: %s", task.id, exc)

    async def get(self, task_id: str) -> Task | None:
        if task_id in self._memory:
            return self._memory[task_id]
        if not self.durable:
            return None
        try:
            async with await self._connect() as conn, conn.cursor() as cur:
                await cur.execute(SELECT_ONE_SQL.format(table=self.table), (task_id,))
                row = await cur.fetchone()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not load task %s: %s", task_id, exc)
            return None
        return _decode(row[0]) if row else None

    async def recent(self, limit: int = 50) -> list[Task]:
        if not self.durable:
            return list(reversed(list(self._memory.values())))[:limit]
        try:
            async with await self._connect() as conn, conn.cursor() as cur:
                await cur.execute(SELECT_RECENT_SQL.format(table=self.table), (limit,))
                rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not list tasks: %s", exc)
            return list(reversed(list(self._memory.values())))[:limit]
        return [t for t in (_decode(r[0]) for r in rows) if t is not None]

    async def resumable(self, limit: int = 100) -> list[Task]:
        """Tasks a restart should pick back up.

        `running` is included deliberately: a task in that state when the
        process died was interrupted mid-flight, and leaving it there forever is
        the silent-failure mode this whole primitive exists to avoid.
        """
        states = ["queued", "planning", "running"]
        if not self.durable:
            return [t for t in self._memory.values() if t.state in states][:limit]
        try:
            async with await self._connect() as conn, conn.cursor() as cur:
                await cur.execute(SELECT_STATE_SQL.format(table=self.table), (states, limit))
                rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not find resumable tasks: %s", exc)
            return []
        return [t for t in (_decode(r[0]) for r in rows) if t is not None]

    def snapshot(self) -> dict[str, Any]:
        by_state: dict[str, int] = {}
        for task in self._memory.values():
            by_state[task.state] = by_state.get(task.state, 0) + 1
        return {"storage": self.status, "durable": self.durable, "held": by_state}


def _decode(payload: Any) -> Task | None:
    try:
        raw = payload if isinstance(payload, dict) else json.loads(payload)
        return Task.from_dict(raw)
    except Exception:  # noqa: BLE001 - one bad row must not lose the rest
        return None


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


class TaskQueue:
    """Runs tasks behind the caller, with a bounded worker pool.

    Bounded because an agent run is expensive: an unbounded pool turns a burst
    of queued work into a burst of concurrent LLM spend, and the first thing an
    operator would do is cap it anyway.

    Workers own the execution; the HTTP layer only enqueues and reads. That
    separation is what lets a task outlive the request that created it.
    """

    def __init__(
        self,
        store: TaskStore,
        runner: Any,
        workers: int = 3,
        poll_seconds: float = 0.25,
    ) -> None:
        self.store = store
        #: `async (task) -> None`, expected to drive the task to a terminal
        #: state and save it. Injected so the queue knows nothing about agents.
        self.runner = runner
        self.worker_count = workers
        self.poll_seconds = poll_seconds
        self._pending: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._running: set[str] = set()
        self.closing = False

    async def start(self) -> None:
        self._workers = [
            asyncio.create_task(self._worker(i)) for i in range(self.worker_count)
        ]
        log.info("task queue started with %d worker(s)", self.worker_count)

    async def resume(self) -> int:
        """Re-enqueue work interrupted by a restart."""
        tasks = await self.store.resumable()
        for task in tasks:
            if task.state == "running":
                # It was interrupted mid-flight. Say so rather than pretending
                # the restart never happened — a resumed run starts over, and
                # anything it already did is in the transcript.
                task.error = "interrupted by a restart and resumed"
            await self._pending.put(task.id)
        if tasks:
            log.info("resumed %d interrupted task(s)", len(tasks))
        return len(tasks)

    async def submit(self, task: Task) -> Task:
        if self.closing:
            raise TaskError("the runtime is shutting down and is not accepting new tasks")
        await self.store.save(task)
        await self._pending.put(task.id)
        return task

    async def _worker(self, index: int) -> None:
        while True:
            task_id = await self._pending.get()
            try:
                task = await self.store.get(task_id)
                if task is None or task.terminal:
                    continue
                self._running.add(task_id)
                try:
                    await self.runner(task)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.exception("task %s failed in worker %d", task_id, index)
                    task.error = f"{type(exc).__name__}: {exc}"
                    if not task.terminal:
                        with _suppress_transition():
                            task.transition("failed", reason=task.error)
                    await self.store.save(task)
                finally:
                    self._running.discard(task_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("task worker %d recovered", index)
            finally:
                self._pending.task_done()

    async def close(self, grace: float = 30.0) -> None:
        """Stop accepting work, let what is running finish, then stop workers."""
        self.closing = True
        deadline = time.monotonic() + grace
        while self._running and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        if self._running:
            log.warning("%d task(s) still running at shutdown", len(self._running))
        for worker in self._workers:
            worker.cancel()
        for worker in self._workers:
            try:
                await worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._workers.clear()

    def snapshot(self) -> dict[str, Any]:
        return {
            "workers": self.worker_count,
            "queued": self._pending.qsize(),
            "running": len(self._running),
            "accepting": not self.closing,
        }


class _suppress_transition:
    """Swallow an illegal transition when already failing.

    A task that errored in a state with no path to `failed` should not have its
    original error replaced by a `TaskError` about the transition — the first
    error is the one worth keeping.
    """

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, *_: Any) -> bool:
        return bool(exc_type is not None and issubclass(exc_type, TaskError))
