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

from fastapi import Body, FastAPI, HTTPException
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..config import RuntimeEnv, env_int
from ..provenance import ORIGIN_LABEL_KEY, ORIGIN_LABEL_VALUE
from .autonomy import RuntimeConfig
from .findings import Finding
from .loop import GatewayClient, Session, run
from .operators import REGISTRY, OperatorContext
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
) -> FastAPI:
    config = cfg or RuntimeConfig.load(os.environ.get("ADHAR_AI_CONFIG"))
    environment = envcfg or RuntimeEnv.from_env()
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

        if config.rag_enabled and environment.rag_dsn:
            app.state.rag_task = asyncio.create_task(_bootstrap_rag(app, config, environment))
        else:
            app.state.rag_task = None
            app.state.rag_status = (
                "disabled (no ADHAR_AI_RAG_DSN)" if config.rag_enabled else "disabled"
            )

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

    def ctx() -> OperatorContext:
        return OperatorContext(cfg=config, toolbox=app.state.toolbox, gateway=app.state.gateway)

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
                "findings_held": len(findings),
                ORIGIN_LABEL_KEY: ORIGIN_LABEL_VALUE,
            }
        )

    @app.get("/config")
    async def read_config() -> dict[str, Any]:
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
    async def chat(body: ChatRequest = Body(...)) -> dict[str, Any]:
        grounding: list[str] = []
        if app.state.retriever is not None:
            grounding = await app.state.retriever.grounding(body.prompt, k=5)
        session = Session(
            autonomy=body.autonomy or config.default_autonomy,
            tenant=body.user or body.session or "anonymous",
            user=body.user,
            model=body.model,
            max_steps=config.max_steps,
            max_tool_calls=config.max_tool_calls_per_op,
            grounding=grounding,
        )
        result = await run(app.state.gateway, app.state.toolbox, session, body.prompt, config)
        payload = result.as_dict()
        payload["grounded_on"] = [g.split("\n", 1)[0].lstrip("# ") for g in grounding]
        return payload

    @app.post("/operators/{name}/event")
    async def operator_event(
        name: str = PathParam(...), event: dict[str, Any] = Body(default_factory=dict)
    ) -> dict[str, Any]:
        operator_cls = REGISTRY.get(name)
        if operator_cls is None:
            raise HTTPException(
                status_code=404,
                detail={"error": f"unknown operator {name!r}", "available": sorted(REGISTRY)},
            )
        finding = await operator_cls(ctx()).handle(event)
        findings.appendleft(finding)
        return finding.model_dump()

    @app.get("/findings")
    async def list_findings(limit: int = 50, operator: str | None = None) -> dict[str, Any]:
        rows = [f for f in findings if operator is None or f.operator == operator]
        return {"count": len(rows), "findings": [f.model_dump() for f in rows[:limit]]}

    return app


# ---------------------------------------------------------------- background --


async def _bootstrap_rag(app: FastAPI, config: RuntimeConfig, environment: RuntimeEnv) -> None:
    """Ingest the platform docs at start-up, then expose the retriever.

    Runs as a background task so a slow or missing database never blocks
    readiness — the runtime answers (ungrounded) while indexing proceeds.
    """
    from ..rag import Retriever, ingest_path, load_embeddings

    try:
        embeddings = await load_embeddings(environment.llm_gateway_url)
        count = await ingest_path(
            environment.rag_dsn, environment.docs_path, embeddings, table=config.rag_table
        )
        app.state.retriever = Retriever(environment.rag_dsn, embeddings, table=config.rag_table)
        app.state.rag_status = (
            f"ready ({count} chunks from {environment.docs_path}, {embeddings.name} embeddings)"
            if count
            else f"ready (no documents at {environment.docs_path}; retrieval will return nothing)"
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        app.state.rag_status = f"unavailable: {type(exc).__name__}: {exc}"
        log.warning("RAG bootstrap failed: %s", exc)


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
                    OperatorContext(config, app.state.toolbox, app.state.gateway)
                )
                finding = await operator.handle(
                    {"apps": [a for a in drifted if a.get("name") in fresh]}
                )
                app.state.findings.appendleft(finding)
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
                    OperatorContext(config, app.state.toolbox, app.state.gateway)
                )
                finding = await operator.handle({"window": "7d", "snapshot": snapshot})
                app.state.findings.appendleft(finding)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("cost poll skipped: %s", exc)
        await asyncio.sleep(COST_POLL_SECONDS)
