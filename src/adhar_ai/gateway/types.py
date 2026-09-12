"""OpenAI-compatible wire types (the gateway's public contract)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolCallFunction(BaseModel):
    name: str
    arguments: str = "{}"


class ToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: ToolCallFunction


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None


class FunctionSpec(BaseModel):
    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


class ToolSpec(BaseModel):
    type: Literal["function"] = "function"
    function: FunctionSpec


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[Message]
    tools: list[ToolSpec] | None = None
    tool_choice: str | dict[str, Any] | None = None
    max_tokens: int = 4096
    temperature: float | None = None
    stream: bool = False
    #: Adhar extension: the tenant the budget is charged to. Also accepted as
    #: the `X-Adhar-Tenant` header.
    tenant: str | None = None


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class Choice(BaseModel):
    index: int = 0
    message: Message
    finish_reason: str = "stop"


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage


class ModelCard(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = 0
    owned_by: str = "adhar-ai"


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelCard]


class EmbeddingRequest(BaseModel):
    input: str | list[str]
    model: str | None = None


class EmbeddingResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[dict[str, Any]]
    model: str
    usage: Usage
