"""FastAPI surface for the agent runtime.

    GET  /healthz                   liveness/readiness (the manifest probes this)
    GET  /config                    the effective autonomy policy (read-back)
    POST /chat                      one agent run -> answer or proposed PR
    POST /operators/{name}/event    operator webhook (Alertmanager, ArgoCD, cron)
    GET  /findings                  recent findings the operators produced

Background workers poll for drift (ArgoCD OutOfSync) and cost (OpenCost), both
through the MCP tools rather than a second set of credentials.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..config import RuntimeEnv, env_int
from ..provenance import ORIGIN_LABEL_KEY, ORIGIN_LABEL_VALUE
from .auth import AuthPolicy, Principal
from .autonomy import RuntimeConfig, lower_of
from .findings import Finding
from .loop import GatewayClient, Session, run
from .operators import REGISTRY, OperatorContext
from .store import FindingStore
from .toolbox import MCPToolbox

log = logging.getLogger("adhar_ai.runtime")

DRIFT_POLL_SECONDS = env_int("ADHAR_AI_DRIFT_POLL_SECONDS", default=300)
COST_POLL_SECONDS = env_int("ADHAR_AI_COST_POLL_SECONDS", default=86400)
FINDINGS_KEPT = 200


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
) -> FastAPI:
    config = cfg or RuntimeConfig.load(os.environ.get("ADHAR_AI_CONFIG"))
    environment = envcfg or RuntimeEnv.from_env()
    policy = auth or AuthPolicy.from_env()
    findings: deque[Finding] = deque(maxlen=FINDINGS_KEPT)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.toolbox = toolbox or MCPToolbox(config.mcp_servers)
        if toolbox is None:
            await app.state.toolbox.connect()
        app.state.gateway = gateway or GatewayClient(
            environment.llm_gateway_url, default_model=environment.llm_model
        )
        app.state.retriever = None
        app.state.rag_status = "disabled"

        # Restore findings written by a previous lifetime before serving, so a
        # rollout does not look to an on-call engineer like "nothing happened".
        app.state.store = store or FindingStore(environment.rag_dsn, table=config.findings_table)
        await app.state.store.prepare()
        for previous in reversed(await app.state.store.recent(FINDINGS_KEPT)):
            findings.appendleft(previous)

        if config.rag_enabled:
            # No DSN gate any more: the lexical index needs neither a database
            # nor a key, so "unkeyed" now means degraded grounding, not none.
            app.state.rag_task = asyncio.create_task(_bootstrap_rag(app, config, environment))
        else:
            app.state.rag_task = None
            app.state.rag_status = "disabled"

        app.state.pollers = [
            asyncio.create_task(_drift_poller(app, config)),
            asyncio.create_task(_cost_poller(app, config)),
        ]
        try:
            yield
        finally:
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
    app.state.config = config
    app.state.env = environment
    app.state.findings = findings
    app.state.auth = policy

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
            retriever=app.state.retriever,
            # A webhook-authenticated caller has no JWT to forward, so the
            # runtime presents its own Keycloak service-account token. Without
            # one, an operator cannot reach a Strict-mode LLM gateway at all.
            bearer=await policy.bearer_for(principal or Principal()),
        )

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        toolbox_: MCPToolbox = app.state.toolbox
        return JSONResponse(
            {
                "status": "ok",
                "autonomy_default": config.default_autonomy,
                "operators": sorted(config.operators),
                "mcp_servers_connected": sorted(
                    {t.domain for t in toolbox_.tools.values()}
                ),
                "mcp_servers_unreachable": toolbox_.errors,
                "tools": sorted(toolbox_.tools),
                "rag": app.state.rag_status,
                "auth": policy.describe(),
                "findings_held": len(findings),
                "findings_store": app.state.store.status,
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
        grounding: list[str] = []
        if app.state.retriever is not None:
            grounding = await app.state.retriever.grounding(body.prompt, k=5)
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
        )
        result = await run(app.state.gateway, app.state.toolbox, session, body.prompt, config)
        payload = result.as_dict()
        payload["grounded_on"] = [g.split("\n", 1)[0].lstrip("# ") for g in grounding]
        payload["principal"] = principal.as_dict()
        payload["autonomy"] = session.autonomy
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
        finding = await operator_cls(await ctx(principal)).handle(event, principal=principal)
        findings.appendleft(finding)
        await _persist(app, finding)
        return finding.model_dump()

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


# ---------------------------------------------------------------- background --


async def _persist(app: FastAPI, finding: Finding) -> None:
    """Write a finding to durable storage if one is configured."""
    store: FindingStore = app.state.store
    await store.save(finding)


async def _bootstrap_rag(app: FastAPI, config: RuntimeConfig, environment: RuntimeEnv) -> None:
    """Build the grounding index at start-up, then expose the retriever.

    Runs as a background task so a slow or missing database never blocks
    readiness. The LEXICAL index is built first and published immediately: it
    needs no key and no database, so the runtime is grounded from the first
    request rather than only after a possibly slow embedding pass. pgvector then
    upgrades that retriever in place if it is available.
    """
    from ..rag import LexicalIndex, Retriever, ingest_path, load_embeddings

    lexical = await asyncio.to_thread(LexicalIndex.from_path, environment.docs_path)
    app.state.retriever = Retriever(dsn="", embeddings=None, lexical=lexical)
    app.state.rag_status = app.state.retriever.mode

    if not environment.rag_dsn:
        return

    try:
        embeddings = await load_embeddings(environment.llm_gateway_url)
        count = await ingest_path(
            environment.rag_dsn, environment.docs_path, embeddings, table=config.rag_table
        )
        app.state.retriever = Retriever(
            environment.rag_dsn, embeddings, table=config.rag_table, lexical=lexical
        )
        app.state.rag_status = (
            f"{app.state.retriever.mode} — {count} chunks indexed from "
            f"{environment.docs_path} with {embeddings.name} embeddings"
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # The lexical retriever published above stays in place, so this is a
        # downgrade rather than a loss of grounding. Say which it is.
        app.state.rag_status = (
            f"{app.state.retriever.mode} — pgvector unavailable "
            f"({type(exc).__name__}: {exc})"
        )
        log.warning("pgvector unavailable, serving lexical grounding only: %s", exc)


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
                        retriever=app.state.retriever,
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
                        retriever=app.state.retriever,
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
