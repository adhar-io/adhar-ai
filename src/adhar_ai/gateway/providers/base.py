from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..types import Message, ToolSpec, Usage


@dataclass(slots=True)
class ProviderResult:
    message: Message
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"
    model: str = ""


class LLMProvider(Protocol):
    name: str

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        max_tokens: int,
        model: str | None,
        temperature: float | None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> ProviderResult: ...

    async def models(self) -> list[str]: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    async def aclose(self) -> None: ...


class EmbeddingsUnsupported(RuntimeError):
    """Raised when a backend offers no embedding endpoint.

    The RAG indexer catches this and falls back to local sentence-transformers
    (when installed) rather than silently indexing garbage vectors.
    """
