"""Retry, circuit breaking, credential masking and the model allow-list.

These are the controls that decide whether a transient provider failure costs a
run, whether a sustained one costs three minutes per run, and whether a Secret
that reached a prompt through a crash log leaves the process.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from adhar_ai.resilience import (
    BreakerRegistry,
    CircuitBreaker,
    CircuitOpen,
    RetryPolicy,
    call_with_resilience,
    is_retryable,
)
from adhar_ai.safety import MASK, CredentialScanner, ModelPolicy


def http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://gateway/v1/chat/completions")
    return httpx.HTTPStatusError(
        f"{status}", request=request, response=httpx.Response(status, request=request)
    )


# --------------------------------------------------------------------------- #
# What is worth retrying
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_statuses_are_retried(status: int) -> None:
    assert is_retryable(http_error(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_not_retried(status: int) -> None:
    """The second attempt fails identically. Retrying adds latency and hides the
    real error behind a slower version of it."""
    assert is_retryable(http_error(status)) is False


def test_timeouts_and_connection_failures_are_retried() -> None:
    request = httpx.Request("POST", "https://gateway/")
    assert is_retryable(httpx.ConnectTimeout("slow", request=request)) is True
    assert is_retryable(httpx.ConnectError("refused", request=request)) is True
    assert is_retryable(TimeoutError()) is True


def test_a_programming_error_is_not_retried() -> None:
    assert is_retryable(ValueError("bad argument")) is False


# --------------------------------------------------------------------------- #
# Retrying
# --------------------------------------------------------------------------- #


async def test_a_transient_failure_is_survived() -> None:
    attempts = {"n": 0}

    async def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise http_error(503)
        return "ok"

    result = await call_with_resilience(
        flaky, target="llm", policy=RetryPolicy(attempts=3, base_delay=0.001)
    )
    assert result == "ok"
    assert attempts["n"] == 3


async def test_a_permanent_failure_fails_on_the_first_attempt() -> None:
    attempts = {"n": 0}

    async def refused() -> str:
        attempts["n"] += 1
        raise http_error(401)

    with pytest.raises(httpx.HTTPStatusError):
        await call_with_resilience(
            refused, target="llm", policy=RetryPolicy(attempts=5, base_delay=0.001)
        )
    assert attempts["n"] == 1, "a 401 must not be retried"


async def test_the_last_error_is_raised_not_the_first() -> None:
    """The last one is the state the dependency was actually in when we gave up,
    which is what an operator reading the error needs."""
    seen = []

    async def worsening() -> str:
        seen.append(len(seen))
        raise http_error(503 if len(seen) < 3 else 500)

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await call_with_resilience(
            worsening, target="llm", policy=RetryPolicy(attempts=3, base_delay=0.001)
        )
    assert raised.value.response.status_code == 500


async def test_a_timeout_policy_bounds_a_hung_call() -> None:
    async def hangs() -> str:
        await asyncio.sleep(10)
        return "never"

    with pytest.raises(TimeoutError):
        await call_with_resilience(
            hangs, target="llm", policy=RetryPolicy(attempts=1, timeout=0.05)
        )


async def test_cancellation_is_not_a_failure() -> None:
    """A cancelled run is a decision. Retrying one would keep working after the
    caller gave up, and would count against a dependency that never misbehaved."""
    breaker = CircuitBreaker(target="llm", threshold=1)

    async def cancelled() -> str:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await call_with_resilience(
            cancelled, target="llm", policy=RetryPolicy(attempts=3), breaker=breaker
        )
    assert breaker.failures == 0
    assert breaker.state == "closed"


def test_backoff_is_jittered_and_bounded() -> None:
    """Without jitter every replica retries in lockstep and the recovering
    backend is hit by the whole fleet at once."""
    policy = RetryPolicy(base_delay=1.0, max_delay=8.0)
    samples = [policy.delay(3) for _ in range(40)]
    assert all(0 <= d <= 8.0 for d in samples)
    assert len(set(samples)) > 1, "a fixed schedule would synchronise the fleet"


# --------------------------------------------------------------------------- #
# Circuit breaking
# --------------------------------------------------------------------------- #


async def test_a_sustained_outage_starts_failing_fast() -> None:
    """Retries make a transient failure survivable and a sustained one worse:
    every step politely retries a backend that has been down for ten minutes."""
    breaker = CircuitBreaker(target="argocd", threshold=3, cooldown=60)
    calls = {"n": 0}

    async def dead() -> str:
        calls["n"] += 1
        raise http_error(503)

    for _ in range(3):
        with pytest.raises(httpx.HTTPStatusError):
            await call_with_resilience(
                dead, target="argocd", policy=RetryPolicy(attempts=1), breaker=breaker
            )
    assert breaker.state == "open"

    before = calls["n"]
    with pytest.raises(CircuitOpen) as raised:
        await call_with_resilience(dead, target="argocd", breaker=breaker)
    assert calls["n"] == before, "an open circuit must not call the dependency"
    assert "argocd" in str(raised.value)


async def test_a_recovered_dependency_closes_the_circuit() -> None:
    breaker = CircuitBreaker(target="gitea", threshold=1, cooldown=0.01)

    async def dead() -> str:
        raise http_error(503)

    async def alive() -> str:
        return "ok"

    with pytest.raises(httpx.HTTPStatusError):
        await call_with_resilience(
            dead, target="gitea", policy=RetryPolicy(attempts=1), breaker=breaker
        )
    assert breaker.state == "open"

    await asyncio.sleep(0.02)  # cooldown elapses; one probe is let through
    assert await call_with_resilience(alive, target="gitea", breaker=breaker) == "ok"
    assert breaker.state == "closed"


async def test_a_failing_probe_re_opens_the_circuit() -> None:
    breaker = CircuitBreaker(target="gitea", threshold=1, cooldown=0.01)

    async def dead() -> str:
        raise http_error(503)

    with pytest.raises(httpx.HTTPStatusError):
        await call_with_resilience(
            dead, target="gitea", policy=RetryPolicy(attempts=1), breaker=breaker
        )
    await asyncio.sleep(0.02)
    with pytest.raises(httpx.HTTPStatusError):
        await call_with_resilience(
            dead, target="gitea", policy=RetryPolicy(attempts=1), breaker=breaker
        )
    assert breaker.state == "open"


def test_breakers_are_per_dependency() -> None:
    """A dead Prometheus must not stop the agent from reading Kubernetes."""
    registry = BreakerRegistry(threshold=1)
    registry.get("prometheus").failed()
    assert registry.get("prometheus").state == "open"
    assert registry.get("kubernetes").state == "closed"
    assert "prometheus" in registry.snapshot()
    assert "kubernetes" not in registry.snapshot()


# --------------------------------------------------------------------------- #
# Credential masking
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("aws-access-key", "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"),
        ("url-credentials", "DATABASE_URL=postgres://adhar:hunter2@db:5432/app"),
        ("jwt", "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"),
        ("openai-key", "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz012345"),
        ("github-token", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
        ("private-key", "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----"),
        ("labelled-secret", "client_secret: s3cr3t-value-here"),
    ],
)
def test_credential_shapes_are_masked(label: str, text: str) -> None:
    """The agent assembles prompts from tool output it did not write — pod env
    blocks, log lines, PR diffs — so a mounted Secret echoed into a crash log
    becomes part of a prompt with nobody deciding that it should."""
    result = CredentialScanner().scan(text)
    assert result.masked, f"{label} was not masked"
    assert label in result.findings
    assert MASK in result.text


@pytest.mark.parametrize(
    "text",
    [
        "the pod is in CrashLoopBackOff with exit code 20",
        "Application vault is OutOfSync, health Degraded",
        "kubectl get pods -n adhar-system",
        "the secret adhar-ai-bot is unset",  # names a secret, carries no value
    ],
)
def test_ordinary_platform_text_is_left_alone(text: str) -> None:
    """A scanner that masks half a diagnosis is worse than none: the model then
    reasons about redacted evidence."""
    assert CredentialScanner().scan(text).masked is False


def test_a_whole_conversation_is_scrubbed_and_reported_by_kind() -> None:
    from adhar_ai.gateway.types import Message

    messages = [
        Message(role="system", content="You are Adhar AI."),
        Message(role="user", content="why did it fail?"),
        Message(role="tool", content="env: AWS_SECRET_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE"),
    ]
    scrubbed, findings = CredentialScanner().scrub_messages(messages)

    assert findings == ["aws-access-key"], "the KIND is reported, never the value"
    assert "AKIAIOSFODNN7EXAMPLE" not in "".join(m.content or "" for m in scrubbed)
    assert scrubbed[0].content == "You are Adhar AI.", "untouched messages pass through"


def test_scrubbing_tolerates_messages_with_no_text() -> None:
    from adhar_ai.gateway.types import Message

    messages = [Message(role="assistant", content=None)]
    scrubbed, findings = CredentialScanner().scrub_messages(messages)
    assert scrubbed == messages
    assert findings == []


# --------------------------------------------------------------------------- #
# Model allow-list
# --------------------------------------------------------------------------- #


def test_no_allow_list_permits_everything() -> None:
    """Shipped default. An allow-list that rejects the platform's own default
    model on upgrade day is worse than not having one."""
    assert ModelPolicy().permits("anything-at-all") is True


def test_an_allow_list_admits_by_exact_name_and_prefix() -> None:
    policy = ModelPolicy(allowed=("claude-*", "gpt-4o-mini"))
    assert policy.permits("claude-sonnet-5") is True
    assert policy.permits("gpt-4o-mini") is True
    assert policy.permits("gpt-4o") is False


def test_the_refusal_says_what_is_permitted() -> None:
    policy = ModelPolicy(allowed=("claude-*",))
    refusal = policy.refusal("gpt-4o")
    assert "gpt-4o" in refusal
    assert "claude-*" in refusal
    assert "ADHAR_AI_ALLOWED_MODELS" in refusal
