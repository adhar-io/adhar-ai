"""The LLM gateway: provider selection, translation, budgets, HTTP surface."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from adhar_ai.config import Budgets, LLMConfig
from adhar_ai.gateway.app import create_app
from adhar_ai.gateway.budget import BudgetExceeded, BudgetLedger
from adhar_ai.gateway.providers import load_provider
from adhar_ai.gateway.providers.anthropic_provider import (
    DEFAULT_MODEL,
    AnthropicProvider,
    _to_anthropic_messages,
)
from adhar_ai.gateway.providers.base import ProviderResult
from adhar_ai.gateway.providers.ollama import OllamaProvider
from adhar_ai.gateway.providers.openai_compatible import (
    AzureOpenAIProvider,
    OpenAICompatibleProvider,
)
from adhar_ai.gateway.types import (
    FunctionSpec,
    Message,
    ToolCall,
    ToolCallFunction,
    ToolSpec,
    Usage,
)

# ---------------------------------------------------------- provider select --


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("claude", "anthropic"),
        ("anthropic", "anthropic"),
        ("openai", "openai"),
        ("azure", "azure"),
        ("openai-compatible", "openai-compatible"),
        ("ollama", "ollama"),
    ],
)
def test_provider_aliases(monkeypatch, raw, expected):
    monkeypatch.setenv("PROVIDER", raw)
    monkeypatch.delenv("ADHAR_AI_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("MODEL", raising=False)
    assert LLMConfig.from_env().provider == expected


def test_adhar_ai_llm_provider_wins_over_the_secret_key(monkeypatch):
    monkeypatch.setenv("PROVIDER", "openai")
    monkeypatch.setenv("ADHAR_AI_LLM_PROVIDER", "ollama")
    assert LLMConfig.from_env().provider == "ollama"


def test_anthropic_is_the_default_with_a_claude_model(monkeypatch):
    for name in ("PROVIDER", "ADHAR_AI_LLM_PROVIDER", "ADHAR_AI_DEFAULT_PROVIDER", "MODEL"):
        monkeypatch.delenv(name, raising=False)
    cfg = LLMConfig.from_env()
    assert cfg.provider == "anthropic"
    assert cfg.model == DEFAULT_MODEL == "claude-sonnet-5"


def test_unknown_provider_is_rejected(monkeypatch):
    monkeypatch.setenv("PROVIDER", "definitely-not-a-provider")
    with pytest.raises(ValueError, match="unknown LLM provider"):
        LLMConfig.from_env()


def test_ollama_is_keyed_without_an_api_key():
    assert LLMConfig(provider="ollama", api_key="").keyed is True
    assert LLMConfig(provider="anthropic", api_key="").keyed is False


def test_load_provider_returns_the_right_class():
    assert isinstance(load_provider(LLMConfig(provider="ollama")), OllamaProvider)
    assert isinstance(load_provider(LLMConfig(provider="azure")), AzureOpenAIProvider)
    assert isinstance(
        load_provider(LLMConfig(provider="openai-compatible")), OpenAICompatibleProvider
    )


# -------------------------------------------------- anthropic translation ----


def test_openai_messages_translate_to_anthropic_blocks():
    system, msgs = _to_anthropic_messages(
        [
            Message(role="system", content="be terse"),
            Message(role="user", content="why is vault degraded?"),
            Message(
                role="assistant",
                content="checking",
                tool_calls=[
                    ToolCall(
                        id="toolu_1",
                        function=ToolCallFunction(
                            name="app_status", arguments='{"app": "vault"}'
                        ),
                    )
                ],
            ),
            Message(role="tool", tool_call_id="toolu_1", content='{"sync_status": "OutOfSync"}'),
        ]
    )
    assert system == "be terse"
    assert msgs[0] == {"role": "user", "content": "why is vault degraded?"}

    assistant = msgs[1]
    assert assistant["role"] == "assistant"
    assert assistant["content"][0] == {"type": "text", "text": "checking"}
    assert assistant["content"][1] == {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "app_status",
        "input": {"app": "vault"},
    }

    result = msgs[2]
    assert result["role"] == "user"
    assert result["content"][0]["type"] == "tool_result"
    assert result["content"][0]["tool_use_id"] == "toolu_1"


def test_parallel_tool_results_collapse_into_one_user_message():
    """Splitting tool results across messages trains the model out of parallel calls."""
    _, msgs = _to_anthropic_messages(
        [
            Message(role="user", content="go"),
            Message(role="tool", tool_call_id="a", content="1"),
            Message(role="tool", tool_call_id="b", content="2"),
        ]
    )
    assert len(msgs) == 2
    assert len(msgs[1]["content"]) == 2


# ------------------------------------------------------ other providers HTTP --


@respx.mock
async def test_openai_compatible_posts_the_expected_body():
    route = respx.post("https://vllm.internal/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "served",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "hi"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            },
        )
    )
    provider = OpenAICompatibleProvider(
        LLMConfig(provider="openai-compatible", endpoint="https://vllm.internal/v1", api_key="k")
    )
    result = await provider.chat(
        [Message(role="user", content="hi")],
        [ToolSpec(function=FunctionSpec(name="t"))],
        max_tokens=64,
        model=None,
        temperature=None,
    )
    assert result.message.content == "hi"
    assert result.usage.total_tokens == 4
    body = json.loads(route.calls[0].request.content)
    assert body["max_tokens"] == 64
    assert body["tools"][0]["function"]["name"] == "t"
    assert route.calls[0].request.headers["authorization"] == "Bearer k"
    await provider.aclose()


@respx.mock
async def test_azure_addresses_a_deployment_and_uses_the_api_key_header():
    route = respx.post(
        "https://res.openai.azure.com/openai/deployments/gpt4o/chat/completions"
    ).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}}
        )
    )
    provider = AzureOpenAIProvider(
        LLMConfig(
            provider="azure", endpoint="https://res.openai.azure.com", model="gpt4o", api_key="az"
        )
    )
    await provider.chat([Message(role="user", content="x")], None, 16, None, None)
    assert route.called
    assert route.calls[0].request.headers["api-key"] == "az"
    assert route.calls[0].request.url.params["api-version"]
    await provider.aclose()


@respx.mock
async def test_ollama_uses_its_native_chat_api():
    route = respx.post("http://ollama.test/api/chat").mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "list_pods",
                                "arguments": {"namespace": "demo"},
                            }
                        }
                    ],
                },
                "prompt_eval_count": 10,
                "eval_count": 5,
            },
        )
    )
    provider = OllamaProvider(LLMConfig(provider="ollama", endpoint="http://ollama.test"))
    result = await provider.chat([Message(role="user", content="pods?")], None, 32, None, None)
    assert result.finish_reason == "tool_calls"
    assert result.message.tool_calls[0].function.name == "list_pods"
    assert json.loads(result.message.tool_calls[0].function.arguments) == {"namespace": "demo"}
    assert result.usage.total_tokens == 15
    assert json.loads(route.calls[0].request.content)["stream"] is False
    await provider.aclose()


# ------------------------------------------------------------------ budgets --


def test_budget_rejects_oversized_requests():
    ledger = BudgetLedger(Budgets(per_op_max_tokens=100))
    with pytest.raises(BudgetExceeded) as exc:
        ledger.check("alice", 200)
    assert exc.value.kind == "per_op_max_tokens"


def test_budget_rate_limits_per_tenant():
    ledger = BudgetLedger(Budgets(rate_limit_requests_per_minute=2))
    ledger.check("alice", 10)
    ledger.check("alice", 10)
    with pytest.raises(BudgetExceeded) as exc:
        ledger.check("alice", 10)
    assert exc.value.kind == "rate_limit"
    ledger.check("bob", 10)  # budgets are per tenant


def test_budget_exhausts_the_daily_token_allowance():
    ledger = BudgetLedger(Budgets(per_user_daily_tokens=100))
    ledger.check("alice", 10)
    ledger.record("alice", 150)
    with pytest.raises(BudgetExceeded) as exc:
        ledger.check("alice", 10)
    assert exc.value.kind == "per_user_daily_tokens"


# ------------------------------------------------------------- HTTP surface --


class StubProvider:
    name = "stub"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat(self, messages, tools, max_tokens, model, temperature, tool_choice=None):
        self.calls.append({"messages": messages, "tools": tools, "max_tokens": max_tokens})
        return ProviderResult(
            message=Message(role="assistant", content="grounded answer"),
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            model="stub-model",
        )

    async def models(self):
        return ["stub-model"]

    async def embed(self, texts):
        return [[0.5] * 4 for _ in texts]

    async def aclose(self):
        return None


@pytest.fixture
def keyed_client(monkeypatch):
    stub = StubProvider()
    monkeypatch.setattr("adhar_ai.gateway.app.load_provider", lambda cfg: stub)
    cfg = LLMConfig(provider="anthropic", api_key="sk-test", model="claude-sonnet-5")
    with TestClient(create_app(cfg)) as client:
        yield client, stub


def test_healthz_reports_provider_and_budgets(keyed_client):
    client, _ = keyed_client
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["provider"] == "anthropic"
    assert body["keyed"] is True
    assert body["adhar.io/origin"] == "adhar-ai"
    assert body["budgets"]["per_op_max_tokens"] == 400_000


def test_healthz_never_leaks_the_api_key(keyed_client):
    client, _ = keyed_client
    assert "sk-test" not in client.get("/healthz").text


def test_chat_completions_round_trip(keyed_client):
    client, stub = keyed_client
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "why is vault degraded?"}],
            "tools": [
                {"type": "function", "function": {"name": "app_status", "description": "d"}}
            ],
            "max_tokens": 512,
        },
        headers={"X-Adhar-Tenant": "alice"},
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["choices"][0]["message"]["content"] == "grounded answer"
    assert body["usage"]["total_tokens"] == 15
    assert stub.calls[0]["tools"][0].function.name == "app_status"

    assert client.get("/v1/budget", headers={"X-Adhar-Tenant": "alice"}).json()[
        "tokens_today"
    ] == 15


def test_models_endpoint(keyed_client):
    client, _ = keyed_client
    assert client.get("/v1/models").json()["data"][0]["id"] == "stub-model"


def test_over_budget_returns_429(monkeypatch):
    stub = StubProvider()
    monkeypatch.setattr("adhar_ai.gateway.app.load_provider", lambda cfg: stub)
    cfg = LLMConfig(
        provider="anthropic", api_key="k", budgets=Budgets(rate_limit_requests_per_minute=1)
    )
    with TestClient(create_app(cfg)) as client:
        payload = {"messages": [{"role": "user", "content": "x"}]}
        assert client.post("/v1/chat/completions", json=payload).status_code == 200
        resp = client.post("/v1/chat/completions", json=payload)
        assert resp.status_code == 429
        assert resp.json()["detail"]["budget"] == "rate_limit"


def test_unkeyed_gateway_reports_503_and_stays_up():
    """No key configured => the platform runs unaffected, not crash-looping."""
    with TestClient(create_app(LLMConfig(provider="anthropic", api_key=""))) as client:
        health = client.get("/healthz").json()
        assert health["status"] == "ok"
        assert health["keyed"] is False
        resp = client.post(
            "/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]}
        )
        assert resp.status_code == 503
        assert "unkeyed" in resp.json()["detail"]


def test_streaming_is_rejected_explicitly(keyed_client):
    client, _ = keyed_client
    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "x"}], "stream": True},
    )
    assert resp.status_code == 400


def test_embeddings_endpoint(keyed_client):
    client, _ = keyed_client
    body = client.post("/v1/embeddings", json={"input": ["a", "b"]}).json()
    assert len(body["data"]) == 2
    assert body["data"][0]["embedding"] == [0.5] * 4


def test_anthropic_provider_reports_embeddings_unsupported():
    provider = AnthropicProvider(LLMConfig(provider="anthropic", api_key="k"))
    with pytest.raises(Exception, match="no embeddings endpoint"):
        import asyncio

        asyncio.run(provider.embed(["x"]))
