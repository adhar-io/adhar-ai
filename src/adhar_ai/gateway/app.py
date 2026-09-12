"""FastAPI surface for the LLM gateway.

    GET  /healthz                 liveness + readiness (the manifest probes this)
    GET  /v1/models               models the configured backend reports
    POST /v1/chat/completions     OpenAI-compatible chat, tool-calling aware
    POST /v1/embeddings           used by the RAG indexer
    GET  /v1/budget               the caller's remaining budget

The API key stays in this process: callers never see it and it is never echoed
into a model context or an audit record.
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from ..config import LLMConfig
from ..provenance import ORIGIN_LABEL_KEY, ORIGIN_LABEL_VALUE
from .budget import BudgetExceeded, BudgetLedger
from .providers import load_provider
from .providers.base import EmbeddingsUnsupported
from .types import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    EmbeddingRequest,
    EmbeddingResponse,
    ModelCard,
    ModelList,
    Usage,
)

DEFAULT_TENANT = "anonymous"


def create_app(cfg: LLMConfig | None = None) -> FastAPI:
    config = cfg or LLMConfig.from_env()
    ledger = BudgetLedger(config.budgets)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.provider = load_provider(config) if config.keyed else None
        yield
        if app.state.provider is not None:
            await app.state.provider.aclose()

    app = FastAPI(title="Adhar AI LLM Gateway", version="0.1.0", lifespan=lifespan)
    app.state.config = config
    app.state.ledger = ledger

    def provider() -> Any:
        """Unkeyed is a *reported* state, not a crash: the platform must run
        unaffected until an operator sets the Vault key (ADR-0024 §9)."""
        if app.state.provider is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Adhar AI is unkeyed: provider={config.provider} has no API key. "
                    "Set secret/adhar-ai/llm in Vault (PROVIDER + API_KEY) to enable agency."
                ),
            )
        return app.state.provider

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "provider": config.provider,
                "model": config.model,
                "keyed": config.keyed,
                "budgets": {
                    "per_user_daily_tokens": config.budgets.per_user_daily_tokens,
                    "per_op_max_tokens": config.budgets.per_op_max_tokens,
                    "per_op_max_tool_calls": config.budgets.per_op_max_tool_calls,
                    "rate_limit_requests_per_minute": (
                        config.budgets.rate_limit_requests_per_minute
                    ),
                },
                ORIGIN_LABEL_KEY: ORIGIN_LABEL_VALUE,
            }
        )

    @app.get("/v1/models", response_model=ModelList)
    async def models() -> ModelList:
        names = await provider().models()
        return ModelList(data=[ModelCard(id=n, owned_by=config.provider) for n in names])

    @app.get("/v1/budget")
    async def budget(x_adhar_tenant: str | None = Header(default=None)) -> dict[str, Any]:
        return ledger.snapshot(x_adhar_tenant or DEFAULT_TENANT)

    @app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
    async def chat_completions(
        request: Request,
        body: ChatCompletionRequest = Body(...),
        x_adhar_tenant: str | None = Header(default=None),
    ) -> ChatCompletionResponse:
        tenant = body.tenant or x_adhar_tenant or DEFAULT_TENANT
        if body.stream:
            raise HTTPException(
                status_code=400,
                detail="streaming is not implemented by the Adhar AI gateway; set stream=false",
            )
        try:
            ledger.check(tenant, body.max_tokens)
        except BudgetExceeded as exc:
            raise HTTPException(
                status_code=429, detail={"budget": exc.kind, "message": str(exc)}
            ) from exc

        try:
            result = await provider().chat(
                messages=body.messages,
                tools=body.tools,
                max_tokens=body.max_tokens,
                model=body.model,
                temperature=body.temperature,
                tool_choice=body.tool_choice,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"{config.provider} backend error: {type(exc).__name__}: {exc}",
            ) from exc

        ledger.record(tenant, result.usage.total_tokens)
        return ChatCompletionResponse(
            id=f"chatcmpl-{uuid.uuid4().hex[:24]}",
            created=int(time.time()),
            model=result.model or config.model,
            choices=[Choice(index=0, message=result.message, finish_reason=result.finish_reason)],
            usage=result.usage,
        )

    @app.post("/v1/embeddings", response_model=EmbeddingResponse)
    async def embeddings(
        body: EmbeddingRequest = Body(...),
        x_adhar_tenant: str | None = Header(default=None),
    ) -> EmbeddingResponse:
        texts = [body.input] if isinstance(body.input, str) else list(body.input)
        try:
            vectors = await provider().embed(texts)
        except EmbeddingsUnsupported as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"embedding failed: {exc}") from exc
        return EmbeddingResponse(
            data=[
                {"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)
            ],
            model=body.model or config.model,
            usage=Usage(prompt_tokens=sum(len(t.split()) for t in texts)),
        )

    return app
