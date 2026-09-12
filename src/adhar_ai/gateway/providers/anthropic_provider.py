"""Anthropic backend — the platform default.

Uses the official `anthropic` Python SDK. Translates the gateway's
OpenAI-compatible wire format to the Messages API and back, including tool use.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from ...config import LLMConfig
from ..types import Message, ToolCall, ToolCallFunction, ToolSpec, Usage
from .base import EmbeddingsUnsupported, ProviderResult

#: The gateway's default. Overridden by MODEL in the `adhar-ai-llm` secret.
DEFAULT_MODEL = "claude-sonnet-5"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, cfg: LLMConfig) -> None:
        import anthropic

        self.cfg = cfg
        self.model = cfg.model or DEFAULT_MODEL
        kwargs: dict[str, Any] = {}
        if cfg.api_key:
            kwargs["api_key"] = cfg.api_key
        if cfg.endpoint:
            kwargs["base_url"] = cfg.endpoint
        self._client = anthropic.AsyncAnthropic(**kwargs)

    async def aclose(self) -> None:
        await self._client.close()

    # ------------------------------------------------------------------ chat --

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        max_tokens: int,
        model: str | None,
        temperature: float | None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> ProviderResult:
        system, converted = _to_anthropic_messages(messages)
        kwargs: dict[str, Any] = {
            "model": model or self.model,
            "max_tokens": max_tokens,
            "messages": converted,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [
                {
                    "name": t.function.name,
                    "description": t.function.description,
                    "input_schema": t.function.parameters,
                }
                for t in tools
            ]
            if tool_choice == "required":
                kwargs["tool_choice"] = {"type": "any"}
            elif tool_choice == "none":
                kwargs["tool_choice"] = {"type": "none"}
        # Sampling params are rejected by current Claude models; only forward a
        # temperature when the caller explicitly asked for one.
        if temperature is not None:
            kwargs["temperature"] = temperature

        response = await self._client.messages.create(**kwargs)

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        function=ToolCallFunction(
                            name=block.name, arguments=json.dumps(block.input or {})
                        ),
                    )
                )

        usage = Usage(
            prompt_tokens=getattr(response.usage, "input_tokens", 0) or 0,
            completion_tokens=getattr(response.usage, "output_tokens", 0) or 0,
        )
        usage.total_tokens = usage.prompt_tokens + usage.completion_tokens

        return ProviderResult(
            message=Message(
                role="assistant",
                content="\n".join(text_parts) or None,
                tool_calls=tool_calls or None,
            ),
            usage=usage,
            finish_reason="tool_calls" if tool_calls else _finish(response.stop_reason),
            model=str(response.model),
        )

    async def models(self) -> list[str]:
        try:
            page = await self._client.models.list(limit=50)
            return [m.id for m in page.data]
        except Exception:
            # Unkeyed or offline: report the configured model rather than fail
            # /v1/models, which is used as a config-readback endpoint.
            return [self.model]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingsUnsupported(
            "Anthropic exposes no embeddings endpoint; Adhar AI RAG falls back to "
            "local sentence-transformers when the `local-embeddings` extra is installed"
        )


def _finish(stop_reason: str | None) -> str:
    return {"end_turn": "stop", "max_tokens": "length", "tool_use": "tool_calls"}.get(
        stop_reason or "", "stop"
    )


def _to_anthropic_messages(
    messages: list[Message],
) -> tuple[str, list[dict[str, Any]]]:
    """Split out the system prompt and convert OpenAI-shaped turns.

    OpenAI `tool` messages become Anthropic `tool_result` blocks, and assistant
    `tool_calls` become `tool_use` blocks — so a full tool-use loop round-trips.
    """
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []

    for msg in messages:
        if msg.role == "system":
            system_parts.append(_text_of(msg))
            continue

        if msg.role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": msg.tool_call_id or "",
                "content": _text_of(msg) or "(empty)",
            }
            # Consecutive tool results must land in ONE user message, or the
            # model learns to stop issuing parallel tool calls.
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue

        if msg.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if text := _text_of(msg):
                blocks.append({"type": "text", "text": text})
            for call in msg.tool_calls or []:
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.id or f"toolu_{uuid.uuid4().hex[:16]}",
                        "name": call.function.name,
                        "input": args,
                    }
                )
            if blocks:
                out.append({"role": "assistant", "content": blocks})
            continue

        out.append({"role": "user", "content": _text_of(msg) or "(empty)"})

    return "\n\n".join(p for p in system_parts if p), out


def _text_of(msg: Message) -> str:
    if isinstance(msg.content, str):
        return msg.content
    if isinstance(msg.content, list):
        return "\n".join(
            str(part.get("text", "")) for part in msg.content if isinstance(part, dict)
        )
    return ""
