"""Retry, timeout and circuit breaking for the agent's outbound calls.

An agent run is a chain of network calls where any single failure throws away
everything done so far. A completion that 503s on step four of six costs the
five tool calls before it, the tokens they consumed, and the user's patience —
and a 503 from a provider is an ordinary event, not an exception.

Three controls, and the interaction between them is the point:

**Retry with exponential backoff and full jitter.** Only on failures that a
retry can actually fix — a timeout, a connection error, a 429, a 5xx. A 400 or a
401 is retried zero times, because the second attempt is guaranteed to fail the
same way and the only thing it adds is latency.

Full jitter (`random(0, base * 2**n)`) rather than a fixed schedule, because
every replica backs off from the same provider outage at the same instant. A
fixed schedule turns one outage into a synchronised thundering herd at 1s, 2s
and 4s; jitter spreads the recovery.

**Timeouts.** A hung call is worse than a failed one: it holds a slot, blocks
the step, and eventually fails anyway with nothing learned. Everything outbound
gets a deadline.

**A circuit breaker.** Retries make a *transient* failure survivable and a
*sustained* one worse — every step politely retries a backend that has been down
for ten minutes, turning one dead dependency into a run that takes three minutes
to fail. After enough consecutive failures the breaker opens and calls fail
immediately with a message naming the dependency, which is both faster and more
useful. One probe is let through after a cooldown to find out whether it is back.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("adhar_ai.resilience")

#: HTTP statuses worth retrying. 429 is the provider asking us to slow down,
#: which is precisely what backoff does; 5xx is the provider having a moment.
#: 408 and 409 are transient by definition.
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class CircuitOpen(RuntimeError):
    """Raised instead of calling a dependency that is known to be failing."""

    def __init__(self, target: str, until: float) -> None:
        self.target = target
        self.retry_after = max(0.0, until - time.monotonic())
        super().__init__(
            f"{target} is failing and its circuit is open; "
            f"retrying in {self.retry_after:.0f}s"
        )


@dataclass(slots=True)
class RetryPolicy:
    attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    timeout: float | None = None

    def delay(self, attempt: int) -> float:
        """Full jitter: uniform in [0, min(max, base * 2**attempt)].

        The randomness is the feature. Without it every replica retries in
        lockstep and the recovering backend is hit by the whole fleet at once.
        """
        ceiling = min(self.max_delay, self.base_delay * (2**attempt))
        return random.uniform(0, ceiling)


def is_retryable(exc: BaseException) -> bool:
    """Would trying again plausibly succeed?

    Deliberately conservative. Retrying something that cannot succeed wastes a
    deadline and hides the real error behind a slower version of it.
    """
    import httpx

    if isinstance(exc, asyncio.TimeoutError | TimeoutError):
        return True
    if isinstance(exc, httpx.TimeoutException | httpx.ConnectError | httpx.ReadError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS
    if isinstance(exc, ConnectionError | OSError):
        return True
    return False


@dataclass
class CircuitBreaker:
    """One breaker per dependency.

    States: closed (calls pass), open (calls fail immediately), half-open (one
    probe passes; success closes, failure re-opens). Deliberately simple —
    consecutive failures, not a rolling error rate — because a breaker whose
    behaviour an operator cannot predict from the logs is worse than no breaker.
    """

    target: str
    threshold: int = 5
    cooldown: float = 30.0
    failures: int = 0
    opened_until: float = 0.0
    _probing: bool = field(default=False, repr=False)

    @property
    def state(self) -> str:
        if self.opened_until and time.monotonic() < self.opened_until:
            return "open"
        if self._probing or self.opened_until:
            return "half-open"
        return "closed"

    def before(self) -> None:
        """Raise `CircuitOpen` if this dependency should not be called."""
        now = time.monotonic()
        if self.opened_until and now < self.opened_until:
            raise CircuitOpen(self.target, self.opened_until)
        if self.opened_until:
            # Cooldown elapsed: let exactly one call through to find out.
            self._probing = True

    def succeeded(self) -> None:
        if self.failures or self.opened_until:
            log.info("circuit for %s closed after a successful call", self.target)
        self.failures = 0
        self.opened_until = 0.0
        self._probing = False
        _report(self.target, "closed")

    def failed(self) -> None:
        self.failures += 1
        if self._probing or self.failures >= self.threshold:
            self.opened_until = time.monotonic() + self.cooldown
            self._probing = False
            log.warning(
                "circuit for %s opened after %d consecutive failures; "
                "failing fast for %.0fs",
                self.target,
                self.failures,
                self.cooldown,
            )
            _report(self.target, "open")


def _report(target: str, state: str) -> None:
    try:
        from .observability import circuit_state

        circuit_state(target, state)
    except Exception:  # noqa: BLE001 - instrumentation is never load-bearing
        pass


async def call_with_resilience[T](
    operation: Callable[[], Awaitable[T]],
    *,
    target: str,
    policy: RetryPolicy | None = None,
    breaker: CircuitBreaker | None = None,
) -> T:
    """Run `operation`, retrying what is worth retrying.

    Raises the LAST exception when every attempt fails, not the first: the last
    one is the state the dependency was actually in when we gave up, which is
    what an operator reading the error needs.
    """
    policy = policy or RetryPolicy()
    if breaker is not None:
        breaker.before()

    last: BaseException | None = None
    for attempt in range(policy.attempts):
        try:
            if policy.timeout:
                result = await asyncio.wait_for(operation(), timeout=policy.timeout)
            else:
                result = await operation()
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            # A cancelled run is a decision, not a failure. Retrying one would
            # keep working after the caller gave up, and would count against the
            # breaker for a dependency that never misbehaved.
            raise
        except BaseException as exc:  # noqa: BLE001
            last = exc
            if not is_retryable(exc) or attempt == policy.attempts - 1:
                if breaker is not None:
                    breaker.failed()
                raise
            delay = policy.delay(attempt)
            log.info(
                "%s failed (%s: %s); retrying in %.2fs [%d/%d]",
                target,
                type(exc).__name__,
                str(exc)[:120],
                delay,
                attempt + 1,
                policy.attempts,
            )
            await asyncio.sleep(delay)
        else:
            if breaker is not None:
                breaker.succeeded()
            return result

    if breaker is not None:
        breaker.failed()
    raise last if last is not None else RuntimeError(f"{target} failed with no exception")


class BreakerRegistry:
    """One breaker per named dependency, created on first use."""

    def __init__(self, threshold: int = 5, cooldown: float = 30.0) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, target: str) -> CircuitBreaker:
        if target not in self._breakers:
            self._breakers[target] = CircuitBreaker(
                target=target, threshold=self.threshold, cooldown=self.cooldown
            )
        return self._breakers[target]

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """What `/healthz` reports about failing dependencies."""
        return {
            name: {"state": breaker.state, "consecutive_failures": breaker.failures}
            for name, breaker in self._breakers.items()
            if breaker.state != "closed" or breaker.failures
        }
