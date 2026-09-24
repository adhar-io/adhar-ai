"""Driving a task through the agents that handle it.

This is where the pieces meet: the router picks an agent, the agent's ceiling
narrows the autonomy, the knowledge base grounds it, the loop runs it, and — if
the agent decides the work belongs to somebody else — the task is handed on and
runs again under the next agent's constraints.

Three properties worth being explicit about.

**The agent's ceiling is applied before the caller's.** An agent declared
`read-only` stays read-only for an administrator at `scoped`, because the
narrowing is a property of the role rather than of the request. Authority only
ever flows downward: ConfigMap, then agent, then principal, then request.

**Handoff is a new run, not a continuation.** The receiving agent gets the task,
the question and what the previous agent concluded — but its own tools, its own
ceiling and its own knowledge scope. Continuing the same message history would
carry the previous agent's tool results into a session that was never allowed to
call those tools.

**A plan is produced before acting only when the stage warrants it.** At
`read-only` and `suggest` the loop is already reviewable, because nothing
happens without a pull request. Above that, the plan is the artifact a human
approves, so it is written first and the task waits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..observability import metrics
from .agents import GENERALIST, AgentRegistry, HandoffError, check_handoff
from .autonomy import RuntimeConfig, lower_of
from .loop import AgentResult, Session, run
from .tasks import PlanStep, Task, TaskError

log = logging.getLogger("adhar_ai.orchestrator")

#: Stages at which the agent writes a plan and waits for approval before acting.
#: Below these the run is already reviewable — nothing lands without a pull
#: request a human merges — so a plan step would be ceremony.
PLAN_FIRST_STAGES = frozenset({"approve-to-apply", "scoped"})

#: Appended when an agent may pass the task on. Kept short: a long menu of
#: colleagues invites the model to route rather than answer.
HANDOFF_PROMPT = (
    "\n\nIf this task belongs to another specialist, say so on the FIRST line "
    "exactly as `HANDOFF: <agent> — <reason>` and then stop. Available: {agents}. "
    "Only hand over when the work genuinely needs their tools; answering is "
    "almost always better than forwarding."
)


@dataclass(slots=True)
class _Attempt:
    """One agent turn that actually reached a model."""

    result: AgentResult
    grounding: list[str] = field(default_factory=list)
    chunk_ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class Answer:
    """What one orchestrated run produced.

    A `Task` is the durable row: state, lineage, the final text. It does not
    carry the things an interactive caller needs to render — which tools were
    called, how many steps it took, which chunks were retrieved. `/journeys`
    used to reconstruct those by reading the conversation store back, which is
    a guess. This carries them directly.
    """

    task: Task
    #: `None` only if the run never reached a model — every agent forwarded.
    result: AgentResult | None = None
    agent: str = ""
    confidence: float = 0.0
    #: Grounding behind the FINAL answer. After a handoff the receiving agent
    #: re-grounds under its own knowledge scope, so this is that agent's.
    grounding: list[str] = field(default_factory=list)
    chunk_ids: list[int] = field(default_factory=list)


class Orchestrator:
    """Runs tasks through the agent roster."""

    def __init__(
        self,
        config: RuntimeConfig,
        registry: AgentRegistry,
        toolbox: Any,
        gateway: Any,
        knowledge: Any = None,
        store: Any = None,
        coverage: Any = None,
        auth: Any = None,
        conversations: Any = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.toolbox = toolbox
        self.gateway = gateway
        self.knowledge = knowledge
        self.store = store
        self.coverage = coverage
        self.auth = auth
        self.conversations = conversations

    # ------------------------------------------------------------- routing --

    def route(self, task: Task) -> str:
        """Decide which agent holds this task, if it does not already say."""
        return self.route_with_confidence(task)[0]

    def route_with_confidence(self, task: Task) -> tuple[str, float]:
        """As `route`, but says how sure it was.

        An agent named on the task is certain by construction: somebody chose
        it, so there is nothing for the router to be unsure about.
        """
        if task.agent and task.agent in self.registry:
            return task.agent, 1.0
        agent, confidence = self.registry.route(task.prompt)
        log.info("task %s routed to %s (confidence %.2f)", task.id, agent, confidence)
        return agent, confidence

    def ceiling_for(self, task: Task, agent_name: str) -> str:
        """The stage this run may actually reach.

        Four narrowings, applied in order, none of which can widen: the
        ConfigMap default, the agent's declared ceiling, whatever the caller
        asked for, and whatever their credential permits.
        """
        agent = self.registry.get(agent_name)
        stage = lower_of(
            task.autonomy or self.config.default_autonomy,
            self.config.default_autonomy,
        )
        return agent.ceiling_for(stage)

    # ------------------------------------------------------------ the work --

    async def execute(self, task: Task) -> Task:
        """Drive a task to a terminal state, following handoffs.

        This is the queue's entry point, so it returns the durable row. An
        interactive caller wants the run itself — use `answer`.
        """
        return (await self.answer(task)).task

    async def answer(
        self,
        task: Task,
        *,
        bearer: str = "",
        tenant: str = "",
        model: str = "",
        persist: bool = True,
    ) -> Answer:
        """Drive a task to a terminal state and report what the run produced.

        `bearer`, `tenant` and `model` exist for the interactive path. A person
        asking a question must reach the gateway as THEMSELVES: their token
        meters their spend against their own Keycloak group, and their tenant
        is what a budget is kept against. Unattended work has no caller, so it
        falls back to the runtime's service account and an `agent:` tenant.

        `persist=False` runs the task WITHOUT writing it to the task store. An
        interactive answer is not a task: it does not outlive its request, so
        a row for it is pure cost — chat traffic would fill the table and bury
        the long-running work `/tasks` exists to show. The run is still in the
        audit stream, which is the record.
        """
        outcome = Answer(task=task)
        for _ in range(len(self.registry.names) + 1):
            agent_name, confidence = self.route_with_confidence(task)
            if task.agent != agent_name:
                task.hand_to(agent_name, "routed")

            handed, attempt = await self._run_once(
                task, bearer=bearer, tenant=tenant, model=model, persist=persist
            )
            if attempt is not None:
                outcome.result = attempt.result
                outcome.grounding = attempt.grounding
                outcome.chunk_ids = attempt.chunk_ids
            outcome.agent = task.agent or agent_name
            outcome.confidence = confidence
            if handed is None:
                outcome.task = task
                return outcome
            # A handoff: loop and run again under the new agent's constraints.
            task = handed
        # Only reachable if every agent forwards, which `check_handoff` already
        # caps — belt to its braces.
        task.transition("failed", reason="the task was forwarded without ever being answered")
        outcome.task = task
        return outcome

    async def _run_once(
        self,
        task: Task,
        *,
        bearer: str = "",
        tenant: str = "",
        model: str = "",
        persist: bool = True,
    ) -> tuple[Task | None, _Attempt | None]:
        """One agent's attempt.

        Returns `(task, attempt)` where the task is non-`None` only if the
        agent handed the work on, and the attempt is `None` only in that case —
        a forwarded turn produced no answer to report.
        """
        agent = self.registry.get(task.agent or GENERALIST)
        stage = self.ceiling_for(task, agent.name)

        grounding: list[str] = []
        chunk_ids: list[int] = []
        if self.knowledge is not None:
            try:
                # Scoped to what this agent should read. The security agent
                # grounds on ADRs, runbooks and incidents; the guide on
                # documentation. An agent given the whole corpus is a
                # generalist wearing a label.
                grounding, chunk_ids = await self.knowledge.grounding_with_ids(
                    task.prompt, k=5, kinds=agent.knowledge_kinds
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("grounding unavailable for task %s: %s", task.id, exc)

        if task.state == "queued":
            task.transition("running", reason=f"{agent.name} picked it up")
        elif task.state == "awaiting_approval":
            task.transition("running", reason="approved")
        await self._save(task, persist)

        system_extra = agent.instructions
        if agent.escalates_to:
            system_extra += HANDOFF_PROMPT.format(agents=", ".join(agent.escalates_to))

        conversation = (
            self.conversations.open(task.session, task.requester)
            if self.conversations is not None and task.session
            else None
        )

        session = Session(
            autonomy=stage,
            allowed_tools=agent.tools,
            # A caller's own tenant where there is one, so a budget is kept
            # against the person rather than against the role they happened to
            # be routed to.
            tenant=tenant or f"agent:{agent.name}",
            model=model or None,
            user=task.requester if task.requester != "anonymous" else None,
            trigger=task.trigger,
            grounding=grounding,
            write_policy=self.config.write_policy,
            max_steps=self.config.max_steps,
            max_tool_calls=self.config.max_tool_calls_per_op,
            bearer=bearer or await self._bearer(),
            # A task raised from a Slack thread or a pull request continues the
            # conversation it came from; one raised by a chore has none.
            history=conversation.history() if conversation else [],
            history_note=conversation.context_note() if conversation else "",
        )

        prompt = task.prompt
        if system_extra:
            # Appended to the question rather than the system prompt: the shared
            # system prompt carries the guarantees, and mixing a role's
            # instructions into it makes those guarantees look negotiable.
            prompt = f"{system_extra}\n\n---\n\n{prompt}"

        result = await run(self.gateway, self.toolbox, session, prompt, self.config)
        task.audit_id = result.audit_id

        attempt = _Attempt(result=result, grounding=grounding, chunk_ids=chunk_ids)

        handoff = _parse_handoff(result.text)
        if handoff is not None:
            return await self._hand_over(task, handoff, persist), None

        task.result = result.text
        task.artifacts.extend(result.pull_requests)
        for pull in result.pull_requests:
            if pull.get("url"):
                task.plan.append(
                    PlanStep(description=f"proposed {pull['url']}", done=True)
                )

        if conversation is not None:
            conversation.record(
                task.prompt, result.text, [c["tool"] for c in result.tool_calls]
            )
            # Only a task that was actually stored can be looked up later, so
            # an unpersisted interactive run leaves no dangling id behind.
            if persist and task.id not in conversation.task_ids:
                conversation.task_ids.append(task.id)

        if self.coverage is not None:
            self.coverage.observe_run(
                task.prompt,
                result,
                agent=agent.name,
                task_id=task.id,
                grounded=bool(grounding),
            )

        if result.kind == "error":
            task.error = result.error
            task.transition("failed", reason=result.error[:120])
        else:
            task.transition("done", reason=result.kind)
        await self._save(task, persist)
        return None, attempt

    async def _hand_over(
        self, task: Task, handoff: tuple[str, str], persist: bool = True
    ) -> Task:
        """Transfer the task, or refuse and make the agent answer instead."""
        target, reason = handoff
        try:
            checked = check_handoff(self.registry, task, target, reason)
        except HandoffError as exc:
            log.warning("refused handoff on task %s: %s", task.id, exc)
            metrics.record_denial("handoff-refused", task.agent or "unknown")
            # Refusing is not failing. The task goes back to the generalist,
            # which can answer anything, rather than dying because one agent
            # wanted to forward it somewhere it was not allowed to.
            task.error = f"handoff refused: {exc}"
            task.hand_to(GENERALIST, "handoff refused")
            return task

        task.plan.append(
            PlanStep(
                description=f"handed from {checked.from_agent} to {checked.to}: {checked.reason}",
                done=True,
            )
        )
        task.hand_to(checked.to, checked.reason)
        await self._save(task, persist)
        return task

    async def _bearer(self) -> str:
        if self.auth is None:
            return ""
        try:
            from .auth import Principal

            return await self.auth.bearer_for(Principal())
        except Exception:  # noqa: BLE001
            return ""

    async def _save(self, task: Task, persist: bool = True) -> None:
        if persist and self.store is not None:
            await self.store.save(task)


def _parse_handoff(text: str) -> tuple[str, str] | None:
    """Read a `HANDOFF: agent — reason` line, if the answer opens with one.

    Only the first line is considered. A model that mentions handing over
    halfway through an otherwise complete answer has answered; treating that as
    a transfer would throw away the answer it just gave.
    """
    if not text:
        return None
    first = text.strip().splitlines()[0].strip()
    if not first.upper().startswith("HANDOFF:"):
        return None
    body = first.split(":", 1)[1].strip()
    for separator in ("—", "--", " - ", ":"):
        if separator in body:
            agent, reason = body.split(separator, 1)
            return agent.strip().strip("`"), reason.strip()
    return (body.strip().strip("`"), "no reason given") if body else None


async def plan_first(orchestrator: Orchestrator, task: Task) -> bool:
    """Write a plan and wait, when the stage is high enough to warrant it.

    Returns whether the task is now waiting. At `read-only` and `suggest` this
    does nothing: those runs are already reviewable because nothing happens
    without a pull request somebody merges.
    """
    agent_name = orchestrator.route(task)
    stage = orchestrator.ceiling_for(task, agent_name)
    if stage not in PLAN_FIRST_STAGES or task.plan:
        return False

    task.hand_to(agent_name, "planning")
    task.transition("planning", reason="writing a plan for approval")
    await orchestrator._save(task)

    session = Session(
        autonomy="read-only",  # planning never acts
        allowed_tools=orchestrator.registry.get(agent_name).tools,
        tenant=f"agent:{agent_name}",
        trigger=task.trigger,
        max_steps=4,
        write_policy=orchestrator.config.write_policy,
    )
    instruction = (
        "Write a short numbered plan for the task below. One line per step, each "
        "naming the tool you would use. Do not carry any step out. If the task "
        "needs no changes, say so in one line instead of inventing steps.\n\n"
        f"---\n\n{task.prompt}"
    )
    result = await run(
        orchestrator.gateway, orchestrator.toolbox, session, instruction, orchestrator.config
    )
    task.plan = [
        PlanStep(description=line.strip())
        for line in (result.text or "").splitlines()
        if line.strip() and line.strip()[0].isdigit()
    ] or [PlanStep(description=(result.text or "no plan produced").strip()[:400])]

    try:
        task.transition(
            "awaiting_approval",
            reason=f"{len(task.plan)} step(s) proposed by {agent_name}; approve to run",
        )
    except TaskError:
        return False
    await orchestrator._save(task)
    return True
