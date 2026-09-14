"""Rate limiting, idempotency and draining.

An agent run is expensive and slow in a way an ordinary HTTP handler is not, so
three ordinary web concerns are unusually sharp: a duplicate delivery costs a
whole run and possibly a duplicate pull request, a burst costs real money, and a
run dropped by SIGTERM has already spent its tokens.
"""

from __future__ import annotations

import asyncio

import pytest
import yaml
from fastapi.testclient import TestClient

from adhar_ai.config import RuntimeEnv
from adhar_ai.runtime.app import create_app
from adhar_ai.runtime.auth import AuthPolicy
from adhar_ai.runtime.autonomy import RuntimeConfig
from adhar_ai.runtime.intake import (
    Drain,
    IdempotencyCache,
    RateLimited,
    RateLimiter,
    ShuttingDown,
    event_key,
)

from .test_runtime import CONFIGMAP_YAML, FakeGateway, FakeToolbox, _answer

ALERT = {
    "alerts": [
        {
            "status": "firing",
            "labels": {"alertname": "KubePodCrashLooping", "namespace": "demo"},
            "annotations": {"summary": "pod is restarting"},
        }
    ]
}


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #


def test_a_caller_may_start_up_to_the_limit() -> None:
    limiter = RateLimiter(limit=3, window=60)
    for _ in range(3):
        limiter.check("alice")
    with pytest.raises(RateLimited) as raised:
        limiter.check("alice")
    assert raised.value.limit == 3
    assert raised.value.retry_after > 0


def test_the_limit_is_per_caller() -> None:
    limiter = RateLimiter(limit=1, window=60)
    limiter.check("alice")
    limiter.check("bob")  # bob is unaffected by alice
    with pytest.raises(RateLimited):
        limiter.check("alice")


def test_the_window_slides() -> None:
    limiter = RateLimiter(limit=1, window=0)
    limiter.check("alice")
    limiter.check("alice"), "a zero-second window always has room"


def test_a_zero_limit_disables_the_control() -> None:
    limiter = RateLimiter(limit=0)
    for _ in range(100):
        limiter.check("alice")


def test_tracked_principals_are_bounded() -> None:
    """An unbounded stream of distinct callers must not grow this forever."""
    limiter = RateLimiter(limit=5, max_principals=10)
    for i in range(50):
        limiter.check(f"caller-{i}")
    assert limiter.snapshot()["principals_tracked"] <= 10


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


def test_the_same_alert_produces_the_same_key() -> None:
    assert event_key("alert-triage", ALERT) == event_key("alert-triage", ALERT)


def test_key_order_does_not_change_the_key() -> None:
    """A retry may re-serialise the same JSON in a different order."""
    reordered = {
        "alerts": [
            {
                "annotations": {"summary": "pod is restarting"},
                "labels": {"namespace": "demo", "alertname": "KubePodCrashLooping"},
                "status": "firing",
            }
        ]
    }
    assert event_key("alert-triage", reordered) == event_key("alert-triage", ALERT)


def test_volatile_timestamps_do_not_make_a_retry_look_new() -> None:
    """Alertmanager stamps every delivery with fresh timestamps. Including them
    would make every retry a new event, which is the failure this prevents."""
    first = {**ALERT, "startsAt": "2026-09-15T10:00:00Z", "fingerprint": "abc"}
    retry = {**ALERT, "startsAt": "2026-09-15T10:05:00Z", "fingerprint": "def"}
    assert event_key("alert-triage", first) == event_key("alert-triage", retry)


def test_a_genuinely_different_alert_gets_a_different_key() -> None:
    other = {"alerts": [{"status": "firing", "labels": {"alertname": "DiskFull"}}]}
    assert event_key("alert-triage", other) != event_key("alert-triage", ALERT)


def test_the_same_event_to_a_different_operator_is_a_different_key() -> None:
    assert event_key("cost-advisor", ALERT) != event_key("alert-triage", ALERT)


def test_the_cache_expires_and_is_bounded() -> None:
    cache = IdempotencyCache(ttl=0, max_entries=3)
    cache.put("k", {"finding": 1})
    assert cache.get("k") is None, "an expired entry must not be replayed"

    fresh = IdempotencyCache(ttl=600, max_entries=3)
    for i in range(10):
        fresh.put(f"k{i}", i)
    assert fresh.snapshot()["entries"] == 3
    assert fresh.get("k0") is None  # evicted
    assert fresh.get("k9") == 9


# --------------------------------------------------------------------------- #
# Draining
# --------------------------------------------------------------------------- #


async def test_in_flight_runs_finish_before_shutdown() -> None:
    """A run has already spent its tokens and may have opened a branch."""
    drain = Drain(grace=5)
    finished = []

    async def work() -> None:
        with drain:
            await asyncio.sleep(0.05)
            finished.append(True)

    task = asyncio.create_task(work())
    await asyncio.sleep(0.01)
    assert drain.in_flight == 1

    await drain.close()
    assert finished == [True]
    await task


async def test_new_work_is_refused_while_draining() -> None:
    drain = Drain(grace=1)
    await drain.close()
    with pytest.raises(ShuttingDown):
        with drain:
            pass


async def test_a_run_that_overruns_the_grace_does_not_block_shutdown_forever() -> None:
    drain = Drain(grace=0.05)

    async def slow() -> None:
        with drain:
            await asyncio.sleep(5)

    task = asyncio.create_task(slow())
    await asyncio.sleep(0.01)
    await drain.close()  # returns after the grace rather than waiting 5s
    assert drain.in_flight == 1
    task.cancel()


# --------------------------------------------------------------------------- #
# Through the real HTTP surface
# --------------------------------------------------------------------------- #


def build(**env):
    toolbox = FakeToolbox()
    gateway = FakeGateway([_answer("all healthy")] * 50)
    cfg = RuntimeConfig.from_mapping(yaml.safe_load(CONFIGMAP_YAML))
    app = create_app(
        cfg=cfg, envcfg=RuntimeEnv(), toolbox=toolbox, gateway=gateway, auth=AuthPolicy()
    )
    for key, value in env.items():
        setattr(app.state, key, value)
    return app, gateway


def test_a_burst_of_chat_requests_is_rate_limited() -> None:
    app, _ = build()
    with TestClient(app) as client:
        app.state.limiter = RateLimiter(limit=2, window=60)
        ok = [client.post("/chat", json={"prompt": "hello"}).status_code for _ in range(2)]
        limited = client.post("/chat", json={"prompt": "hello"})

    assert ok == [200, 200]
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers
    assert limited.json()["detail"]["limit"] == 2


def test_a_repeated_alert_replays_instead_of_running_again() -> None:
    """One flapping alert would otherwise become N agent runs and, above
    read-only, N near-identical pull requests for one problem."""
    app, gateway = build()
    with TestClient(app) as client:
        first = client.post("/operators/alert-triage/event", json=ALERT).json()
        turns_used = len(gateway.requests)
        second = client.post("/operators/alert-triage/event", json=ALERT).json()

    assert first["id"] == second["id"], "the same finding is returned"
    assert second.get("replayed") is True
    assert len(gateway.requests) == turns_used, "no second agent run was started"


def test_a_different_alert_still_runs() -> None:
    app, gateway = build()
    other = {"alerts": [{"status": "firing", "labels": {"alertname": "DiskFull"}}]}
    with TestClient(app) as client:
        client.post("/operators/alert-triage/event", json=ALERT)
        before = len(gateway.requests)
        client.post("/operators/alert-triage/event", json=other)

    assert len(gateway.requests) > before


def test_a_draining_runtime_refuses_new_work_with_503() -> None:
    app, _ = build()
    with TestClient(app) as client:
        app.state.drain.closing = True
        response = client.post("/chat", json={"prompt": "hello"})
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert "shutting down" in response.json()["detail"]["error"]


def test_healthz_reports_the_intake_posture() -> None:
    app, _ = build()
    with TestClient(app) as client:
        intake = client.get("/healthz").json()["intake"]
    assert intake["draining"] is False
    assert intake["in_flight"] == 0
    assert intake["rate_limit"]["limit"] > 0
    assert "idempotency" in intake
