"""Ollama backend — keeps air-gapped installs viable (no API key required).

Uses Ollama's native `/api/chat` (which supports tool calling) and
`/api/embeddings`, so it works against a stock `ollama serve`.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ...config import LLMConfig
from ..types import Message, ToolCall, ToolCallFunction, ToolSpec, Usage
from .base import ProviderResult

DEFAULT_ENDPOINT = "http://ollama.adhar-system.svc.cluster.local:11434"


class OllamaProvider:
    name = "ollama"

    def __init__(self, cfg: LLMConfig, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
        self.model = cfg.model or "llama3.1"
        self.base_url = (cfg.endpoint or DEFAULT_ENDPOINT).rstrip("/")
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=300.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

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
            "messages": [_to_ollama(m) for m in messages],
            "stream": False,
            "options": {"num_predict": max_tokens},
        }
        if temperature is not None:
            body["options"]["temperature"] = temperature
        if tools:
            body["tools"] = [t.model_dump() for t in tools]

        resp = await self._http().post(f"{self.base_url}/api/chat", json=body)
        resp.raise_for_status()
        payload = resp.json()
        raw = payload.get("message") or {}

        tool_calls = [
            ToolCall(
                id=f"call_{i}",
                function=ToolCallFunction(
                    name=str((tc.get("function") or {}).get("name", "")),
                    arguments=_as_json((tc.get("function") or {}).get("arguments")),
                ),
            )
            for i, tc in enumerate(raw.get("tool_calls") or [])
        ]
        usage = Usage(
            prompt_tokens=int(payload.get("prompt_eval_count") or 0),
            completion_tokens=int(payload.get("eval_count") or 0),
        )
        usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
        return ProviderResult(
            message=Message(
                role="assistant", content=raw.get("content") or None, tool_calls=tool_calls or None
            ),
            usage=usage,
            finish_reason="tool_calls" if tool_calls else "stop",
            model=chosen,
        )

    async def models(self) -> list[str]:
        try:
            resp = await self._http().get(f"{self.base_url}/api/tags")
            resp.raise_for_status()
            return [str(m["name"]) for m in (resp.json().get("models") or [])]
        except Exception:
            return [self.model]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            resp = await self._http().post(
                f"{self.base_url}/api/embeddings", json={"model": self.model, "prompt": text}
            )
            resp.raise_for_status()
            out.append([float(v) for v in resp.json().get("embedding") or []])
        return out


def _as_json(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value or {})


def _to_ollama(msg: Message) -> dict[str, Any]:
    out: dict[str, Any] = {"role": msg.role}
    content = msg.content
    out["content"] = content if isinstance(content, str) else ""
    if msg.tool_calls:
        out["tool_calls"] = [
            {
                "function": {
                    "name": c.function.name,
                    "arguments": json.loads(c.function.arguments or "{}"),
                }
            }
            for c in msg.tool_calls
        ]
    return out
