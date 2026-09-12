"""Gateway controls that were configured but not connected to anything.

`ADHAR_AI_RESPONSE_CACHE` and `BUDGET_MAX_CONCURRENT_SESSIONS` were both read
from the environment and then consulted by no code, and the cost tools declared
a list of valid dimensions they never checked. A setting nothing reads is worse
than a missing one: it reads like a control in review and is not one in
production.
"""

from __future__ import annotations

import pytest

from adhar_ai.gateway.cache import ResponseCache, cache_key
from adhar_ai.gateway.types import Message
from adhar_ai.mcp.cost import VALID_DIMENSIONS, validate_dimension


class Body:
    """Enough of a ChatCompletionRequest for the cache to key on."""

    def __init__(self, temperature=0, prompt="why is vault degraded", tenant_model="claude-x"):
        self.model = tenant_model
        self.max_tokens = 1024
        self.temperature = temperature
        self.tool_choice = None
        self.stream = False
        self.messages = [Message(role="user", content=prompt)]
        self.tools = []


# --------------------------------------------------------------------------- #
# Response cache
# --------------------------------------------------------------------------- #


def test_only_deterministic_requests_are_cached() -> None:
    """Above zero the caller is asking the provider for variation; serving a
    stored answer would silently take that away."""
    cache = ResponseCache()
    assert cache.cacheable(Body(temperature=0)) is True
    assert cache.cacheable(Body(temperature=0.7)) is False
    assert cache.cacheable(Body(temperature=None)) is False


def test_streaming_is_never_cached() -> None:
    body = Body(temperature=0)
    body.stream = True
    assert ResponseCache().cacheable(body) is False


def test_a_disabled_cache_caches_nothing() -> None:
    assert ResponseCache(enabled=False).cacheable(Body()) is False


def test_the_key_covers_the_prompt_the_model_and_the_tenant() -> None:
    """Two requests share an entry only when the provider would have been asked
    exactly the same question by the same caller."""
    same = cache_key("alice", Body())
    assert cache_key("alice", Body()) == same
    assert cache_key("bob", Body()) != same, "a cache must not cross tenants"
    assert cache_key("alice", Body(prompt="something else")) != same
    assert cache_key("alice", Body(tenant_model="gpt-5")) != same


def test_a_cached_answer_is_returned_then_expires() -> None:
    cache = ResponseCache(ttl_seconds=0)
    cache.put("k", "answer")
    assert cache.get("k") is None, "an expired entry must not be served"
    assert cache.misses == 1

    fresh = ResponseCache(ttl_seconds=300)
    fresh.put("k", "answer")
    assert fresh.get("k") == "answer"
    assert fresh.hits == 1


def test_the_cache_is_bounded() -> None:
    """It lives in a Pod with a memory limit; unbounded is not an option."""
    cache = ResponseCache(max_entries=3)
    for i in range(10):
        cache.put(f"k{i}", i)
    assert len(cache._entries) == 3
    assert cache.get("k0") is None  # evicted, least recently used first
    assert cache.get("k9") == 9


def test_the_snapshot_describes_what_is_cacheable() -> None:
    snapshot = ResponseCache().snapshot()
    assert snapshot["enabled"] is True
    assert "temperature=0" in snapshot["cacheable"]


# --------------------------------------------------------------------------- #
# Cost dimensions
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("dimension", VALID_DIMENSIONS)
def test_every_documented_dimension_is_accepted(dimension: str) -> None:
    validate_dimension(dimension)


def test_a_label_dimension_is_accepted_by_prefix() -> None:
    validate_dimension("label:app.kubernetes.io/part-of")


def test_a_bare_label_prefix_is_not_a_dimension() -> None:
    with pytest.raises(ValueError):
        validate_dimension("label:")


def test_an_unknown_dimension_is_named_rather_than_proxied() -> None:
    """Without this the typo reached OpenCost, whose 400 the model reads as
    "the cost backend is broken" rather than "I passed a bad argument"."""
    with pytest.raises(ValueError) as raised:
        validate_dimension("namspace")
    assert "namspace" in str(raised.value)
    assert "namespace" in str(raised.value)  # says what to use instead
