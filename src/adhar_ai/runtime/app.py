"""FastAPI surface for the agent runtime.

    GET  /healthz                   liveness/readiness (the manifest probes this)
    GET  /config                    the effective autonomy policy (read-back)
    POST /chat                      one agent run -> answer or proposed PR
    POST /operators/{name}/event    operator webhook (Alertmanager, ArgoCD, cron)
    GET  /findings                  recent findings the operators produced
    GET  /knowledge                 what the knowledge base holds, by origin
    POST /knowledge                 add a note, runbook or incident write-up
    POST /knowledge/search          retrieve grounding directly, without an LLM
    POST /knowledge/refresh         re-derive knowledge from the platform now
    POST /feedback                  say whether an answer's grounding helped

Background workers reconnect dead MCP sessions, refresh the knowledge base, and
poll for drift (ArgoCD OutOfSync) and cost (OpenCost) — all through the MCP
tools rather than a second set of credentials.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from ..config import RuntimeEnv, env, env_int
from ..observability import METRICS_CONTENT_TYPE, metrics, setup_tracing
from ..provenance import ORIGIN_LABEL_KEY, ORIGIN_LABEL_VALUE
from .agents import AgentRegistry
from .auth import AuthPolicy, Principal
from .autonomy import RuntimeConfig, lower_of
from .chores import ChoreRegistry, ChoreRun
from .discovery import ONBOARDING_QUESTIONS, CoverageLog, capability_catalogue
from .findings import Finding
from .intake import (
    Drain,
    IdempotencyCache,
    RateLimited,
    RateLimiter,
    ShuttingDown,
    event_key,
)
from .journeys import parse as parse_journey
from .journeys import render as render_journey
from .loop import GatewayClient, Session, run
from .operators import REGISTRY, OperatorContext
from .orchestrator import Orchestrator, plan_first
from .sessions import ConversationStore
from .store import FindingStore
from .tasks import PlanStep, Task, TaskQueue, TaskStore
from .toolbox import MCPToolbox

log = logging.getLogger("adhar_ai.runtime")

#: How often to retry MCP servers with no live session. MCP Deployments roll on
#: any image update, node drain or OOM kill, and without this the runtime kept a
#: dead session for that domain until it was itself restarted.
MCP_RECONNECT_SECONDS = env_int("ADHAR_AI_MCP_RECONNECT_SECONDS", default=30)
DRIFT_POLL_SECONDS = env_int("ADHAR_AI_DRIFT_POLL_SECONDS", default=300)
COST_POLL_SECONDS = env_int("ADHAR_AI_COST_POLL_SECONDS", default=86400)
#: How often the knowledge base re-derives itself from the platform. The live
#: cluster inventory sets the floor; docs and packages are cheap to re-check
#: because unchanged content is never re-embedded.
KNOWLEDGE_REFRESH_SECONDS = env_int("ADHAR_AI_KNOWLEDGE_REFRESH_SECONDS", default=1800)
#: Where the package contracts are mounted, if they are. Optional: without it
#: the catalogue simply is not one of the knowledge sources.
PACKAGES_PATH = env("ADHAR_AI_PACKAGES_PATH", default="")
FINDINGS_KEPT = 200
#: Agent runs one caller may START per window. Not a token budget — that is
#: agentgateway's, per Keycloak group — but a cap on concurrent expensive work,
#: which token budgets do not constrain until the tokens are already spent.
RATE_LIMIT = env_int("ADHAR_AI_RATE_LIMIT", default=20)
RATE_WINDOW = env_int("ADHAR_AI_RATE_WINDOW_SECONDS", default=60)
#: How long a repeated operator event returns the first run's finding instead of
#: running again. Alertmanager and ArgoCD notifications both retry.
IDEMPOTENCY_TTL = env_int("ADHAR_AI_IDEMPOTENCY_TTL_SECONDS", default=600)
#: How long SIGTERM waits for in-flight runs before giving up on them.
SHUTDOWN_GRACE = env_int("ADHAR_AI_SHUTDOWN_GRACE_SECONDS", default=30)
#: Concurrent agent runs. An unbounded pool turns a burst of queued work into a
#: burst of concurrent LLM spend.
TASK_WORKERS = env_int("ADHAR_AI_TASK_WORKERS", default=3)
#: How often due chores are checked. The interval each chore declares is what
#: actually paces it; this is only the resolution of the check.
CHORE_TICK_SECONDS = env_int("ADHAR_AI_CHORE_TICK_SECONDS", default=300)


class NoteRequest(BaseModel):
    """A human-written piece of platform knowledge."""

    title: str
    body: str
    #: `note` for meeting notes and decisions, `runbook` for a procedure,
    #: `incident` for a write-up of something that broke. The kind is used at
    #: retrieval time: a runbook outranks a two-year-old aside.
    kind: str = "note"
    tags: list[str] = []


class SearchRequest(BaseModel):
    query: str
    k: int = 5
    kinds: list[str] = []


class FeedbackRequest(BaseModel):
    """Whether the grounding behind an answer actually helped."""

    chunk_ids: list[int]
    helpful: bool = True
    #: The question, so an unhelpful answer becomes a coverage gap rather than
    #: only a down-vote on some chunks.
    question: str = ""


class TaskRequest(BaseModel):
    """Work to be done behind the caller, rather than during their request."""

    prompt: str
    agent: str = ""
    autonomy: str | None = None
    session: str | None = None
    #: Write a plan and wait for approval before acting, whatever the stage.
    plan_first: bool = False


class ApprovalRequest(BaseModel):
    approve: bool = True
    note: str = ""


class ChatRequest(BaseModel):
    prompt: str
    session: str | None = None
    user: str | None = None
    autonomy: str | None = None
    model: str | None = None


def create_app(
    cfg: RuntimeConfig | None = None,
    envcfg: RuntimeEnv | None = None,
    toolbox: MCPToolbox | None = None,
    gateway: GatewayClient | None = None,
    auth: AuthPolicy | None = None,
    store: FindingStore | None = None,
    knowledge: Any = None,
) -> FastAPI:
    config = cfg or RuntimeConfig.load(os.environ.get("ADHAR_AI_CONFIG"))
    environment = envcfg or RuntimeEnv.from_env()
    policy = auth or AuthPolicy.from_env()
    findings: deque[Finding] = deque(maxlen=FINDINGS_KEPT)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Module-level pollers mint the runtime's own token through this;
        # they cannot reach the `policy` closure any other way.
        app.state.policy = policy
        app.state.toolbox = toolbox or MCPToolbox(config.mcp_servers)
        if toolbox is None:
            await app.state.toolbox.connect()
        app.state.gateway = gateway or GatewayClient(
            environment.llm_gateway_url, default_model=environment.llm_model
        )
        from ..rag import KnowledgeBase

        app.state.knowledge = knowledge or KnowledgeBase.build(
            dsn=environment.rag_dsn,
            docs_path=environment.docs_path,
            packages_path=PACKAGES_PATH,
            table=config.rag_table,
            toolbox=app.state.toolbox,
            findings=None,  # wired below, once the finding store exists
        )
        app.state.rag_status = "starting"

        # Restore findings written by a previous lifetime before serving, so a
        # rollout does not look to an on-call engineer like "nothing happened".
        app.state.store = store or FindingStore(environment.rag_dsn, table=config.findings_table)
        await app.state.store.prepare()

        # Tasks: the primitive everything else rests on. Durable where a
        # database exists, in-memory where one does not — and either way the
        # work outlives the request that asked for it.
        app.state.tasks = TaskStore(environment.rag_dsn)
        await app.state.tasks.prepare()
        app.state.orchestrator = Orchestrator(
            config=config,
            registry=app.state.agents,
            toolbox=app.state.toolbox,
            gateway=app.state.gateway,
            knowledge=None,  # attached once the knowledge base is up
            store=app.state.tasks,
            coverage=app.state.coverage,
            auth=policy,
            conversations=app.state.conversations,
        )
        app.state.queue = TaskQueue(
            app.state.tasks, app.state.orchestrator.execute, workers=TASK_WORKERS
        )
        await app.state.queue.start()
        await app.state.queue.resume()
        for previous in reversed(await app.state.store.recent(FINDINGS_KEPT)):
            findings.appendleft(previous)

        # Findings are a knowledge source: what the platform concluded last time
        # is exactly what the next similar investigation should retrieve.
        from ..rag.sources import FindingsSource

        if app.state.store.enabled:
            app.state.knowledge.sources.append(FindingsSource(app.state.store))

        if config.rag_enabled:
            # No DSN gate: the lexical index needs neither a database nor a key,
            # so "unkeyed" now means degraded grounding, not none.
            app.state.rag_task = asyncio.create_task(
                _bootstrap_knowledge(app, config, environment)
            )
        else:
            app.state.rag_task = None
            app.state.rag_status = "disabled"

        app.state.pollers = [
            asyncio.create_task(_reconnect_loop(app)),
            asyncio.create_task(_chore_loop(app)),
            asyncio.create_task(_knowledge_refresh_loop(app)),
            asyncio.create_task(_drift_poller(app, config)),
            asyncio.create_task(_cost_poller(app, config)),
        ]
        try:
            yield
        finally:
            # Refuse new work and let what is in flight finish. A run has
            # already spent its tokens and may have opened a branch; dropping it
            # on SIGTERM wastes the spend and can leave a half-made proposal.
            await app.state.drain.close()
            await app.state.queue.close(grace=SHUTDOWN_GRACE)
            for task in [*app.state.pollers, app.state.rag_task]:
                if task is not None:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            if toolbox is None:
                await app.state.toolbox.aclose()
            if gateway is None:
                await app.state.gateway.aclose()

    app = FastAPI(title="Adhar AI Runtime", version="0.1.0", lifespan=lifespan)
    app.state.tracing = setup_tracing("runtime")
    app.state.config = config
    app.state.env = environment
    app.state.findings = findings
    app.state.auth = policy
    app.state.limiter = RateLimiter(limit=RATE_LIMIT, window=RATE_WINDOW)
    app.state.agents = AgentRegistry.from_mapping(config.agents)
    app.state.chores = ChoreRegistry.from_mapping(config.chores)
    app.state.conversations = ConversationStore()
    app.state.coverage = CoverageLog()
    app.state.idempotency = IdempotencyCache(ttl=IDEMPOTENCY_TTL)
    app.state.drain = Drain(grace=SHUTDOWN_GRACE)

    if not policy.oidc_enabled and not policy.webhook_token and not policy.require_auth:
        log.warning(
            "no credential configured (OIDC_ISSUER_URL / ADHAR_AI_WEBHOOK_TOKEN): "
            "callers are answered but pinned to read-only, so nothing can open a PR"
        )

    async def ctx(principal: Principal | None = None) -> OperatorContext:
        return OperatorContext(
            cfg=config,
            toolbox=app.state.toolbox,
            gateway=app.state.gateway,
            retriever=app.state.knowledge,
            # A webhook-authenticated caller has no JWT to forward, so the
            # runtime presents its own Keycloak service-account token. Without
            # one, an operator cannot reach a Strict-mode LLM gateway at all.
            bearer=await policy.bearer_for(principal or Principal()),
        )

    @app.get("/metrics")
    async def prometheus_metrics() -> Response:
        """Prometheus exposition. Ungated like `/healthz`: a scrape presents no
        credential, and these are aggregate counters rather than cluster detail
        — no tenant label, no prompt, no finding."""
        toolbox_: MCPToolbox = app.state.toolbox
        metrics.mcp_servers_connected(len(toolbox_.servers) - len(toolbox_.unhealthy))
        return Response(content=metrics.render_metrics(), media_type=METRICS_CONTENT_TYPE)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        toolbox_: MCPToolbox = app.state.toolbox
        return JSONResponse(
            {
                "status": "ok",
                "autonomy_default": config.default_autonomy,
                "operators": sorted(config.operators),
                # Derived from LIVE sessions. This used to be the set of
                # domains that had tools at start-up, so a server that died
                # afterwards still showed as connected while every call to it
                # failed — the one state in which health matters most.
                "mcp_servers_connected": sorted(
                    d for d in toolbox_.servers if d not in toolbox_.unhealthy
                ),
                "mcp_servers_unreachable": {
                    d: toolbox_.errors.get(d, "no session") for d in toolbox_.unhealthy
                },
                "tools": sorted(toolbox_.tools),
                # Derived live from the knowledge base rather than a string
                # cached at start-up, which went stale the moment pgvector came
                # up behind it and reported "pending" over a working store.
                "rag": (
                    app.state.knowledge.mode
                    if getattr(app.state, "knowledge", None) is not None
                    else app.state.rag_status
                ),
                "auth": policy.describe(),
                "findings_held": len(findings),
                "findings_store": app.state.store.status,
                "tracing": "on" if app.state.tracing else "off",
                # Dependencies the breaker has given up on, and credential
                # kinds masked on the way out. Both empty is the healthy case.
                "circuits": app.state.gateway.breakers.snapshot()
                if hasattr(app.state.gateway, "breakers")
                else {},
                "credentials_masked": getattr(app.state.gateway, "masked", {}),
                "tasks": {
                    **app.state.tasks.snapshot(),
                    **app.state.queue.snapshot(),
                },
                "agents": app.state.agents.names,
                "chores": app.state.chores.snapshot(),
                "conversations": app.state.conversations.snapshot(),
                "coverage": app.state.coverage.snapshot(),
                "intake": {
                    **app.state.drain.snapshot(),
                    "rate_limit": app.state.limiter.snapshot(),
                    "idempotency": app.state.idempotency.snapshot(),
                },
                ORIGIN_LABEL_KEY: ORIGIN_LABEL_VALUE,
            }
        )

    @app.get("/config")
    async def read_config(request: Request) -> dict[str, Any]:
        policy.principal(request)
        return {
            "autonomy": {"default": config.default_autonomy},
            "limits": {
                "maxSteps": config.max_steps,
                "maxToolCallsPerOp": config.max_tool_calls_per_op,
            },
            "writePolicy": {
                "allowedRepos": list(config.write_policy.allowed_repos),
                "allowedPathPrefixes": list(config.write_policy.allowed_path_prefixes),
            },
            "auth": policy.describe(),
            "operators": {
                name: {
                    "trigger": op.trigger,
                    "autonomy": op.autonomy,
                    "allowedTools": list(op.allowed_tools),
                }
                for name, op in config.operators.items()
            },
            "mcpServers": config.mcp_servers,
        }

    @app.post("/chat")
    async def chat(request: Request, body: ChatRequest = Body(...)) -> dict[str, Any]:
        principal: Principal = policy.principal(request)
        _admit(app, principal.subject)
        conversation = (
            app.state.conversations.open(body.session, principal.subject)
            if body.session
            else None
        )
        grounding: list[str] = []
        chunk_ids: list[int] = []
        if app.state.knowledge is not None:
            grounding, chunk_ids = await app.state.knowledge.grounding_with_ids(
                body.prompt, k=5
            )
        # The caller may ask for a LOWER stage than the ConfigMap's, never a
        # higher one, and `ceiling` then pins an unauthenticated or
        # non-write-group caller to read-only whatever either of them said.
        requested = body.autonomy or config.default_autonomy
        session = Session(
            autonomy=principal.ceiling(lower_of(requested, config.default_autonomy)),
            tenant=principal.subject if principal.authenticated else (
                body.user or body.session or "anonymous"
            ),
            user=principal.subject if principal.authenticated else body.user,
            model=body.model,
            max_steps=config.max_steps,
            max_tool_calls=config.max_tool_calls_per_op,
            grounding=grounding,
            write_policy=config.write_policy,
            # The caller's own token, so agentgateway meters this run's spend
            # against the caller's Keycloak group rather than the runtime's.
            bearer=await policy.bearer_for(principal),
            # Prior turns, so "and what should I do about it?" does not
            # re-investigate from scratch.
            history=conversation.history() if conversation else [],
            history_note=conversation.context_note() if conversation else "",
        )
        with app.state.drain:
            result = await run(
                app.state.gateway, app.state.toolbox, session, body.prompt, config
            )
        payload = result.as_dict()
        payload["grounded_on"] = [g.split("\n", 1)[0].lstrip("# ") for g in grounding]
        payload["principal"] = principal.as_dict()
        payload["autonomy"] = session.autonomy
        # Returned so a caller can say whether this grounding helped; POST them
        # back to /feedback and the store learns which chunks are worth ranking.
        payload["grounding_chunk_ids"] = chunk_ids
        if conversation is not None:
            conversation.record(
                body.prompt,
                result.text,
                [c["tool"] for c in result.tool_calls],
            )
            payload["session"] = conversation.as_dict()
        app.state.coverage.observe_run(
            body.prompt, result, agent="chat", grounded=bool(grounding)
        )
        return payload

    @app.post("/operators/{name}/event")
    async def operator_event(
        request: Request,
        name: str = PathParam(...),
        event: dict[str, Any] = Body(default_factory=dict),
    ) -> dict[str, Any]:
        # Alertmanager and ArgoCD notifications hold no OIDC client, so this is
        # the one route that also accepts the shared webhook bearer. Without a
        # credential the run still happens — triage is useful — but `ceiling`
        # pins it to read-only, so an unauthenticated POST cannot open a PR.
        principal: Principal = policy.principal(request, allow_webhook_token=True)
        operator_cls = REGISTRY.get(name)
        if operator_cls is None:
            raise HTTPException(
                status_code=404,
                detail={"error": f"unknown operator {name!r}", "available": sorted(REGISTRY)},
            )

        # Alertmanager and ArgoCD notifications both retry, on any non-2xx and
        # on their own restart, and a retried alert is indistinguishable from a
        # new one. Without this, one flapping alert becomes N agent runs and — at
        # `suggest` or above — N near-identical pull requests for one problem.
        key = event_key(name, event)
        if (cached := app.state.idempotency.get(key)) is not None:
            log.info("replaying the finding for a repeated %s event", name)
            return {**cached, "replayed": True}

        _admit(app, principal.subject)
        with app.state.drain:
            finding = await operator_cls(await ctx(principal)).handle(
                event, principal=principal
            )
        findings.appendleft(finding)
        await _persist(app, finding)
        payload = finding.model_dump()
        app.state.idempotency.put(key, payload)
        return payload

    # ---------------------------------------------------------------- tasks --

    @app.post("/tasks")
    async def create_task(
        request: Request, body: TaskRequest = Body(...)
    ) -> dict[str, Any]:
        """Queue work and return immediately.

        The difference from `/chat` is not the reasoning — it is the same agent
        loop — but the lifetime. A task outlives this request, so it can take
        twenty minutes, pause for an approval, be handed between agents, and
        survive the rollout that interrupts it.
        """
        principal: Principal = policy.principal(request)
        _admit(app, principal.subject)

        task = Task(
            prompt=body.prompt,
            agent=body.agent,
            requester=principal.subject,
            autonomy=principal.ceiling(
                lower_of(body.autonomy or config.default_autonomy, config.default_autonomy)
            ),
            trigger="chat",
            session=body.session or "",
        )
        waiting = False
        if body.plan_first or task.autonomy in ("approve-to-apply", "scoped"):
            waiting = await plan_first(app.state.orchestrator, task)
        if waiting:
            # It wrote a plan and is holding for a human. Enqueueing it here
            # would have a worker pick it up and run the plan nobody approved,
            # which makes the whole gate decorative. `/tasks/{id}/approve`
            # submits it.
            await app.state.tasks.save(task)
        else:
            await app.state.queue.submit(task)

        if body.session:
            conversation = app.state.conversations.open(body.session, principal.subject)
            conversation.task_ids.append(task.id)
        return task.as_dict()

    @app.get("/tasks")
    async def list_tasks(request: Request, limit: int = 50) -> dict[str, Any]:
        policy.principal(request)
        tasks = await app.state.tasks.recent(limit)
        return {"count": len(tasks), "tasks": [t.as_dict() for t in tasks]}

    @app.get("/tasks/{task_id}")
    async def read_task(request: Request, task_id: str = PathParam(...)) -> dict[str, Any]:
        policy.principal(request)
        task = await app.state.tasks.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail={"error": f"no task {task_id!r}"})
        return task.as_dict()

    @app.post("/tasks/{task_id}/approve")
    async def approve_task(
        request: Request,
        task_id: str = PathParam(...),
        body: ApprovalRequest = Body(default_factory=ApprovalRequest),
    ) -> dict[str, Any]:
        """Approve or reject a plan a task is waiting on.

        Approval requires a write-capable credential. A task that reached
        `awaiting_approval` is one whose stage can change the platform, so
        letting an unauthenticated caller release it would make the whole
        approval gate decorative.
        """
        principal: Principal = policy.principal(request)
        task = await app.state.tasks.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail={"error": f"no task {task_id!r}"})
        if task.state != "awaiting_approval":
            raise HTTPException(
                status_code=409,
                detail={"error": f"task {task_id} is {task.state}, not awaiting approval"},
            )
        if not principal.write_allowed:
            metrics.record_denial("approval-unauthorised", "tasks")
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "approving a plan requires a write-capable credential",
                    "groups": list(policy.write_groups),
                },
            )

        if not body.approve:
            note = body.note.strip() or "no reason given"
            # On the plan, not only in `waiting_on`: a cancelled task clears
            # what it was waiting on, so a rejection recorded only there is a
            # rejection nobody can read afterwards.
            task.plan.append(
                PlanStep(description=f"rejected by {principal.subject}: {note}", done=True)
            )
            task.transition("cancelled", reason=f"rejected by {principal.subject}")
            await app.state.tasks.save(task)
            return task.as_dict()

        task.plan.append(
            PlanStep(description=f"approved by {principal.subject}", done=True)
        )
        await app.state.queue.submit(task)
        return task.as_dict()

    # --------------------------------------------------------------- agents --

    @app.get("/agents")
    async def list_agents(request: Request) -> dict[str, Any]:
        policy.principal(request)
        return {"agents": app.state.agents.describe()}

    @app.post("/agents/route")
    async def route_preview(
        request: Request, body: ChatRequest = Body(...)
    ) -> dict[str, Any]:
        """Which agent would take this, without spending a completion to find out."""
        policy.principal(request)
        agent, confidence = app.state.agents.route(body.prompt)
        spec = app.state.agents.get(agent)
        return {
            "agent": agent,
            "confidence": confidence,
            "ceiling": spec.ceiling,
            "tools": list(spec.tools) or "all permitted by ceiling",
        }

    # ------------------------------------------------------------- journeys --

    @app.post("/journeys/{surface}")
    async def journey_event(
        request: Request,
        surface: str = PathParam(...),
        payload: dict[str, Any] = Body(default_factory=dict),
    ) -> dict[str, Any]:
        """Slack, a pull request, or a failed pipeline.

        Every one of these payloads is attacker-influenced — anybody who can
        open a pull request can write anything in its body. They arrive as data
        in a prompt that says so, and the structural guarantee holds regardless:
        a review cannot change anything, whatever it is told.
        """
        principal: Principal = policy.principal(request, allow_webhook_token=True)
        try:
            journey = parse_journey(surface, payload)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail={"error": str(exc)}) from exc
        if not journey.prompt.strip():
            return {"skipped": "the payload carried no question"}

        _admit(app, principal.subject)
        task = Task(
            prompt=journey.prompt,
            requester=principal.subject,
            autonomy=principal.ceiling(config.default_autonomy),
            trigger=surface,
            # A Slack thread and a pull request are both long-lived contexts
            # where a follow-up refers to what came before. Dropping this makes
            # every message in a thread start from nothing.
            session=journey.session,
        )
        with app.state.drain:
            task = await app.state.orchestrator.execute(task)

        conversation = app.state.conversations.get(journey.session, principal.subject)
        checked = (
            [{"tool": tool} for turn in conversation.turns for tool in turn.tools]
            if conversation
            else []
        )
        result = SimpleNamespace(
            text=task.result,
            kind="error" if task.error else "answer",
            error=task.error,
            pull_requests=task.artifacts,
            # What the run actually called. The renderer prints these under
            # "what I checked", so a plan step here would claim a tool that
            # does not exist.
            tool_calls=checked,
        )
        return {
            "task": task.id,
            "agent": task.agent,
            "reply": render_journey(result, journey),
        }

    # --------------------------------------------------------------- chores --

    @app.get("/chores")
    async def list_chores(request: Request) -> dict[str, Any]:
        policy.principal(request)
        return {"chores": app.state.chores.describe()}

    @app.post("/chores/{name}/run")
    async def run_chore(request: Request, name: str = PathParam(...)) -> dict[str, Any]:
        """Run one chore now, regardless of its schedule."""
        principal: Principal = policy.principal(request, allow_webhook_token=True)
        chore = app.state.chores.get(name)
        if chore is None:
            raise HTTPException(
                status_code=404,
                detail={"error": f"no chore {name!r}", "available": [
                    c["name"] for c in app.state.chores.describe()
                ]},
            )
        _admit(app, principal.subject)
        run = await _run_chore(app, chore, principal)
        return run.as_dict()

    # ------------------------------------------------------------ discovery --

    @app.get("/capabilities")
    async def capabilities(request: Request) -> dict[str, Any]:
        """What the platform can do right now, derived rather than declared."""
        policy.principal(request)
        catalogue = await capability_catalogue(
            app.state.toolbox, app.state.agents, app.state.knowledge, app.state.chores
        )
        catalogue["tryAsking"] = [
            {"question": q, "demonstrates": why} for q, why in ONBOARDING_QUESTIONS
        ]
        return catalogue

    @app.get("/coverage")
    async def coverage(request: Request, limit: int = 50) -> dict[str, Any]:
        """Questions the platform answered badly — the queue of runbooks to write."""
        policy.principal(request)
        return app.state.coverage.report(limit)

    # ------------------------------------------------------------ knowledge --

    @app.get("/knowledge")
    async def knowledge_stats(request: Request) -> dict[str, Any]:
        policy.principal(request)
        return await app.state.knowledge.stats()

    @app.post("/knowledge")
    async def add_knowledge(
        request: Request, body: NoteRequest = Body(...)
    ) -> dict[str, Any]:
        """Take in something a human learned, and make it retrievable at once.

        This is the feedback path the platform's knowledge would otherwise have
        no home for: meeting notes, a troubleshooting write-up, what an incident
        turned out to be. Indexed immediately rather than at the next refresh —
        somebody writing up an outage at 02:00 should be able to ask about it at
        02:01.

        Writing knowledge is NOT a platform write: it touches the agent's own
        database, never a cluster and never a repository, so it does not go
        through the pull-request path and does not need `platform-admin`. The
        four MCP write tools remain the only things that change the platform,
        and they remain PR-only.
        """
        principal: Principal = policy.principal(request)
        result = await app.state.knowledge.add_note(
            title=body.title,
            body=body.body,
            kind=body.kind,
            author=principal.subject,
            tags=body.tags,
        )
        return result

    @app.post("/knowledge/search")
    async def search_knowledge(
        request: Request, body: SearchRequest = Body(...)
    ) -> dict[str, Any]:
        """Retrieve grounding without running the agent.

        Useful on its own — it answers "what does the platform know about X"
        for a fraction of the cost and latency of a full agent run — and it is
        how you check what the agent WOULD have been given when an answer
        disappoints.
        """
        policy.principal(request)
        hits = await app.state.knowledge.search(
            body.query, k=body.k, kinds=tuple(body.kinds)
        )
        return {
            "query": body.query,
            "mode": app.state.knowledge.mode,
            "hits": [
                {
                    "chunk_id": h.chunk_id,
                    "source": h.source,
                    "kind": h.kind,
                    "origin": h.origin,
                    "retrieval": h.retrieval,
                    "score": round(h.score, 6),
                    "text": h.text,
                }
                for h in hits
            ],
        }

    @app.post("/knowledge/refresh")
    async def refresh_knowledge(
        request: Request, origins: list[str] = Body(default_factory=list)
    ) -> dict[str, Any]:
        """Re-derive knowledge now, instead of waiting for the next sweep."""
        policy.principal(request)
        reports = await app.state.knowledge.refresh(only=tuple(origins))
        app.state.rag_status = app.state.knowledge.mode
        return {"mode": app.state.knowledge.mode, "refreshed": [r.as_dict() for r in reports]}

    @app.post("/feedback")
    async def record_feedback(
        request: Request, body: FeedbackRequest = Body(...)
    ) -> dict[str, Any]:
        """Say whether an answer's grounding actually helped.

        The chunk ids come back on every `/chat` response. Repeatedly helpful
        chunks rank slightly higher and repeatedly misleading ones slightly
        lower — bounded deliberately, so one downvote cannot bury the only
        document that answers a question.
        """
        policy.principal(request)
        updated = await app.state.knowledge.record_feedback(body.chunk_ids, body.helpful)
        if not body.helpful and body.question:
            # A human saying an answer did not help is the strongest coverage
            # signal there is — stronger than any heuristic over the run.
            app.state.coverage.record_unhelpful(body.question)
        return {"updated": updated, "helpful": body.helpful}

    @app.get("/findings")
    async def list_findings(
        request: Request, limit: int = 50, operator: str | None = None
    ) -> dict[str, Any]:
        # Gated like /chat. A finding carries the evidence that produced it —
        # tool arguments, pod names, log excerpts, cost figures — so an ungated
        # listing hands out exactly the cluster detail the read tools are
        # RBAC-scoped to protect. `/healthz` stays open: it is the probe.
        policy.principal(request)
        rows = [f for f in findings if operator is None or f.operator == operator]
        return {"count": len(rows), "findings": [f.model_dump() for f in rows[:limit]]}

    return app


def _admit(app: FastAPI, principal: str) -> None:
    """Decide whether to start one expensive unit of work.

    Two refusals, two status codes an HTTP client already knows how to handle:
    `429` with `Retry-After` for a rate limit, `503` with `Retry-After` while
    draining. Alertmanager and ArgoCD both honour a retry on each.
    """
    if app.state.drain.closing:
        raise HTTPException(
            status_code=503,
            detail={"error": str(ShuttingDown())},
            headers={"Retry-After": "5"},
        )
    try:
        app.state.limiter.check(principal)
    except RateLimited as exc:
        metrics.record_denial("rate-limit", "chat")
        raise HTTPException(
            status_code=429,
            detail={
                "error": str(exc),
                "limit": exc.limit,
                "window_seconds": exc.window,
            },
            headers={"Retry-After": str(max(1, int(exc.retry_after)))},
        ) from exc


# ---------------------------------------------------------------- background --


async def _persist(app: FastAPI, finding: Finding) -> None:
    """Store a finding durably, and feed it back into what the agent knows.

    The second half is the learning loop that matters most: a finding is the
    platform's own conclusion about itself, so the next investigation of a
    similar alert retrieves what the last one worked out — including the pull
    request that fixed it.
    """
    store: FindingStore = app.state.store
    await store.save(finding)
    knowledge = getattr(app.state, "knowledge", None)
    if knowledge is not None:
        await knowledge.learn_from_finding(finding)


async def _bootstrap_knowledge(
    app: FastAPI, config: RuntimeConfig, environment: RuntimeEnv
) -> None:
    """Build the knowledge base, then keep it current.

    The in-process lexical index is built and published FIRST, because it needs
    neither a key nor a database: the runtime is grounded from its first request
    rather than only after a possibly slow embedding pass. pgvector then upgrades
    that same knowledge base in place if it is available.
    """
    from ..rag import KnowledgeBase, LexicalIndex, load_embeddings

    knowledge: KnowledgeBase = app.state.knowledge
    if getattr(app.state, "orchestrator", None) is not None:
        app.state.orchestrator.knowledge = knowledge

    lexical = await asyncio.to_thread(LexicalIndex.from_path, environment.docs_path)
    knowledge.lexical = lexical
    app.state.rag_status = knowledge.mode

    if not environment.rag_dsn:
        log.info("no knowledge database configured; serving in-process lexical grounding")
        return

    try:
        knowledge.embedder = await load_embeddings(
            environment.llm_gateway_url,
            # The gateway enforces JWT auth on /v1/embeddings like every other
            # route; an unauthenticated probe 401s and the knowledge base
            # silently drops to lexical retrieval on every install.
            token_provider=app.state.policy.service_token,
        )
        await knowledge.prepare()
        reports = await knowledge.refresh()
        app.state.rag_status = knowledge.mode
        for report in reports:
            log.info("knowledge refreshed: %s", report.as_dict())
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        # The lexical index published above stays in place, so this is a
        # downgrade rather than a loss of grounding. Say which it is.
        app.state.rag_status = f"{knowledge.mode} — pgvector unavailable ({exc})"
        log.warning("knowledge base degraded to lexical only: %s", exc)


async def _knowledge_refresh_loop(app: FastAPI) -> None:
    """Re-derive knowledge on a schedule, so it tracks the platform.

    This is what makes the base dynamic: the cluster source is rebuilt from live
    state every pass, and the findings source picks up whatever the operators
    have concluded since. Unchanged content is never re-embedded, so a pass over
    a corpus that did not move costs one database round trip per origin.
    """
    while True:
        await asyncio.sleep(KNOWLEDGE_REFRESH_SECONDS)
        knowledge = app.state.knowledge
        try:
            reports = await knowledge.refresh()
            app.state.rag_status = knowledge.mode
            changed = sum(r.chunks_written + r.chunks_deleted for r in reports)
            if changed:
                log.info("knowledge refresh updated %d chunks", changed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("knowledge refresh failed: %s", exc)


async def _run_chore(app: FastAPI, chore: Any, principal: Principal) -> ChoreRun:
    """Run one chore as a task, under its own agent and caps.

    A chore in dry-run is pinned to `read-only` regardless of the ConfigMap, so
    "report what you would do" cannot quietly become "do it" because somebody
    raised the global stage for an unrelated reason.
    """
    run = ChoreRun(chore=chore.name)
    stage = "read-only" if chore.dry_run else principal.ceiling(config_of(app).default_autonomy)

    task = Task(
        prompt=chore.prompt,
        agent=chore.agent,
        requester=f"chore:{chore.name}",
        autonomy=stage,
        trigger=f"chore:{chore.name}",
    )
    run.task_id = task.id
    try:
        with app.state.drain:
            task = await app.state.orchestrator.execute(task)
    except Exception as exc:  # noqa: BLE001
        run.error = f"{type(exc).__name__}: {exc}"
        app.state.chores.record(run)
        return run

    proposals = [a for a in task.artifacts if a.get("url")]
    if len(proposals) > chore.max_proposals:
        # The cap is what stands between a helpful morning and an unreviewable
        # flood. It is reported rather than silently trimmed, because a chore
        # that wanted to open twelve pull requests is a chore to look at.
        run.skipped = (
            f"produced {len(proposals)} proposals, above its cap of {chore.max_proposals}"
        )
        log.warning("chore %s exceeded its proposal cap", chore.name)
    run.proposals = len(proposals)
    app.state.chores.record(run)
    return run


def config_of(app: FastAPI) -> Any:
    return app.state.config


async def _chore_loop(app: FastAPI) -> None:
    """Run due chores. Enabled ones only, one at a time."""
    while True:
        await asyncio.sleep(CHORE_TICK_SECONDS)
        try:
            due = app.state.chores.due()
            for chore in due:
                log.info("running chore %s (dry_run=%s)", chore.name, chore.dry_run)
                await _run_chore(app, chore, Principal())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("chore sweep failed: %s", exc)


async def _reconnect_loop(app: FastAPI) -> None:
    """Rebuild MCP sessions for domains that have none.

    Runs in the lifespan task on purpose: anyio ties a session's cancel scope to
    the task that opened it, so the rebuild has to happen where the original
    connect did, not inside whichever request noticed the failure.

    A domain becomes eligible only after a call through it has actually failed
    (or it never connected). There is deliberately no liveness probe of healthy
    sessions — see `MCPToolbox.reconnect` for why one cannot be issued safely
    from here — so a server that dies is noticed by the first call that tries to
    use it, and recovered on the next sweep.
    """
    while True:
        await asyncio.sleep(MCP_RECONNECT_SECONDS)
        toolbox: MCPToolbox = app.state.toolbox
        try:
            for domain in toolbox.unhealthy:
                if await toolbox.reconnect(domain):
                    log.info("reconnected to the %s MCP server", domain)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("mcp reconnect sweep failed: %s", exc)


async def _drift_poller(app: FastAPI, config: RuntimeConfig) -> None:
    """Poll ArgoCD (through the gitops MCP tool) for OutOfSync applications and
    hand any drift to the drift-explain operator."""
    await asyncio.sleep(15)
    seen: set[str] = set()
    while True:
        try:
            status = await app.state.toolbox.call("sync_status", {"only_unhealthy": True})
            drifted = [
                a
                for a in (status.get("applications") or [])
                if a.get("sync_status") == "OutOfSync"
            ]
            names = {str(a.get("name")) for a in drifted}
            fresh = names - seen
            seen = names
            if fresh:
                operator = REGISTRY["drift-explain"](
                    OperatorContext(
                        config,
                        app.state.toolbox,
                        app.state.gateway,
                        retriever=app.state.knowledge,
                        # A poller has no user token to forward, so the run
                        # presents the runtime's own service-account token —
                        # exactly what `ctx()` does for a webhook. Without it
                        # every scheduled run met the Strict-JWT gateway with no
                        # bearer and 401'd, and the finding it recorded was the
                        # 401 rather than the drift.
                        bearer=await app.state.policy.bearer_for(Principal()),
                    )
                )
                finding = await operator.handle(
                    {"apps": [a for a in drifted if a.get("name") in fresh]}
                )
                app.state.findings.appendleft(finding)
                await _persist(app, finding)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("drift poll skipped: %s", exc)
        await asyncio.sleep(DRIFT_POLL_SECONDS)


async def _cost_poller(app: FastAPI, config: RuntimeConfig) -> None:
    """Daily OpenCost review via the cost MCP tools."""
    await asyncio.sleep(60)
    while True:
        try:
            snapshot = await app.state.toolbox.call(
                "cost_by", {"dimension": "namespace", "window": "7d", "top": 10}
            )
            if not snapshot.get("error"):
                operator = REGISTRY["cost-advisor"](
                    OperatorContext(
                        config,
                        app.state.toolbox,
                        app.state.gateway,
                        retriever=app.state.knowledge,
                        bearer=await app.state.policy.bearer_for(Principal()),
                    )
                )
                finding = await operator.handle({"window": "7d", "snapshot": snapshot})
                app.state.findings.appendleft(finding)
                await _persist(app, finding)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("cost poll skipped: %s", exc)
        await asyncio.sleep(COST_POLL_SECONDS)
