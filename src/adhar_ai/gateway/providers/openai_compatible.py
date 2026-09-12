"""OpenAI, Azure OpenAI, and any OpenAI-compatible endpoint (vLLM, LiteLLM, …).

These speak the gateway's own wire format, so the translation is close to a
pass-through. Implemented over httpx rather than the `openai` SDK to keep the
image slim and to make the request shape directly assertable in tests.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ...config import LLMConfig
from ..types import Message, ToolCall, ToolSpec, Usage
from .base import ProviderResult

OPENAI_API = "https://api.openai.com/v1"
AZURE_API_VERSION = "2024-10-21"


class OpenAICompatibleProvider:
    name = "openai-compatible"

    def __init__(self, cfg: LLMConfig, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
        self.model = cfg.model or "gpt-4o"
        self.base_url = (cfg.endpoint or OPENAI_API).rstrip("/")
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=120.0, headers=self._headers())
        return self._client

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        return headers

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _chat_url(self, model: str) -> str:
        return f"{self.base_url}/chat/completions"

    def _params(self) -> dict[str, str]:
        return {}

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        max_tokens: int,
        model: str | None,
        temperature: float | None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> ProviderResult:
        chosen = model or self.model
        body: dict[str, Any] = {
            "model": chosen,
            "messages": [m.model_dump(exclude_none=True) for m in messages],
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = [t.model_dump() for t in tools]
            if tool_choice:
                body["tool_choice"] = tool_choice
        if temperature is not None:
            body["temperature"] = temperature

        resp = await self._http().post(self._chat_url(chosen), json=body, params=self._params())
        resp.raise_for_status()
        payload = resp.json()

        choice = (payload.get("choices") or [{}])[0]
        raw = choice.get("message") or {}
        usage_raw = payload.get("usage") or {}
        return ProviderResult(
            message=Message(
                role="assistant",
                content=raw.get("content"),
                tool_calls=[ToolCall(**tc) for tc in (raw.get("tool_calls") or [])] or None,
            ),
            usage=Usage(
                prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
                completion_tokens=int(usage_raw.get("completion_tokens") or 0),
                total_tokens=int(usage_raw.get("total_tokens") or 0),
            ),
            finish_reason=str(choice.get("finish_reason") or "stop"),
            model=str(payload.get("model") or chosen),
        )

    async def models(self) -> list[str]:
        try:
            resp = await self._http().get(f"{self.base_url}/models", params=self._params())
            resp.raise_for_status()
            return [str(m["id"]) for m in (resp.json().get("data") or [])]
        except Exception:
            return [self.model]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self._http().post(
            f"{self.base_url}/embeddings",
            json={"model": "text-embedding-3-small", "input": texts},
            params=self._params(),
        )
        resp.raise_for_status()
        rows = sorted(resp.json().get("data") or [], key=lambda d: int(d.get("index", 0)))
        return [list(map(float, row["embedding"])) for row in rows]


class OpenAIProvider(OpenAICompatibleProvider):
    name = "openai"


class AzureOpenAIProvider(OpenAICompatibleProvider):
    """Azure addresses a *deployment*, so the model name is part of the path."""

    name = "azure"

    def _chat_url(self, model: str) -> str:
        return f"{self.base_url}/openai/deployments/{model}/chat/completions"

    def _params(self) -> dict[str, str]:
        return {"api-version": AZURE_API_VERSION}

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["api-key"] = self.cfg.api_key
        return headers

    async def models(self) -> list[str]:
        return [self.model] if self.model else []


def _now() -> int:
    return int(time.time())
