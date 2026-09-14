"""Deciding whether to start a unit of work, and finishing the ones in flight.

An agent run is expensive and slow in a way an ordinary HTTP handler is not: it
spends tokens, holds a connection for tens of seconds, and can open a pull
request. That makes three ordinary web concerns unusually sharp here.

**Duplicate delivery.** Alertmanager retries. So does ArgoCD notifications. Both
re-send on any non-2xx and on their own restart, and a retried alert is
indistinguishable from a new one — same labels, same annotations. Without
deduplication one flapping alert becomes N agent runs and, above `read-only`, N
near-identical pull requests for one problem. Idempotency turns a retry into the
same answer rather than more work.

**Rate.** agentgateway holds per-group token budgets, which is the right place
for spend. It does not stop one caller from starting fifty concurrent agent
runs, each of which is cheap at the first step and expensive by the sixth. The
limit here is on *starting work*, and it is per principal.

**Shutdown.** A rollout sends SIGTERM. An agent run mid-flight has already spent
its tokens and may have opened a branch; dropping it wastes the spend and can
leave a half-made proposal. `Drain` lets in-flight runs finish while refusing
new ones, which is what makes a rollout uneventful.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("adhar_ai.intake")


class RateLimited(Exception):
    """Too many runs started by one caller."""

    def __init__(self, retry_after: float, limit: int, window: int) -> None:
        self.retry_after = retry_after
        self.limit = limit
        self.window = window
        super().__init__(
            f"rate limit: at most {limit} agent runs per {window}s for one caller; "
            f"retry in {retry_after:.0f}s"
        )


@dataclass
class RateLimiter:
    """A sliding window per principal.

    A sliding window rather than a token bucket because the thing being limited
    is starting an expensive job, and the question an operator asks is "how many
    runs can one person start per minute" — which a window answers directly and
    a bucket only approximately.
    """

    limit: int = 20
    window: int = 60
    #: Bounded so an unbounded stream of distinct callers cannot grow this
    #: forever. Anonymous callers all share one key, which is deliberate: they
    #: are not distinguishable, so they should not get a limit each.
    max_principals: int = 4096
    _hits: OrderedDict[str, deque[float]] = field(default_factory=OrderedDict)

    def check(self, principal: str) -> None:
        if self.limit <= 0:
            return
        now = time.monotonic()
        hits = self._hits.get(principal)
        if hits is None:
            hits = deque()
            self._hits[principal] = hits
            while len(self._hits) > self.max_principals:
                self._hits.popitem(last=False)
        self._hits.move_to_end(principal)

        cutoff = now - self.window
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= self.limit:
            raise RateLimited(hits[0] + self.window - now, self.limit, self.window)
        hits.append(now)

    def snapshot(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "window_seconds": self.window,
            "principals_tracked": len(self._hits),
        }


def event_key(operator: str, event: dict[str, Any]) -> str:
    """A stable identity for one logical event.

    Hashes the operator plus the event body with sorted keys, so a retry that
    re-serialises the same JSON in a different order still matches. Volatile
    fields are dropped first — Alertmanager stamps every delivery with fresh
    timestamps, and including them would make every retry look new, which is
    exactly the failure this exists to prevent.
    """
    payload = _stable(event)
    material = json.dumps({"operator": operator, "event": payload}, sort_keys=True, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


#: Fields that change on every delivery of the SAME alert.
_VOLATILE = frozenset(
    {
        "startsAt",
        "endsAt",
        "updatedAt",
        "timestamp",
        "receivedAt",
        "fingerprint",
        "groupKey",
        "truncatedAlerts",
    }
)


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in sorted(value.items()) if k not in _VOLATILE}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


@dataclass
class IdempotencyCache:
    """Recent event keys and what they produced.

    In-process and bounded. A second replica would not see the first's entries,
    which is a real limitation and the honest reason the manifests declare one
    replica; a shared cache belongs in the same place a shared rate limit would,
    and neither exists yet.
    """

    ttl: float = 600.0
    max_entries: int = 512
    _entries: OrderedDict[str, tuple[float, Any]] = field(default_factory=OrderedDict)
    hits: int = 0

    def get(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if time.monotonic() - stored_at > self.ttl:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return value

    def put(self, key: str, value: Any) -> None:
        self._entries[key] = (time.monotonic(), value)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def snapshot(self) -> dict[str, Any]:
        return {"entries": len(self._entries), "replays": self.hits, "ttl_seconds": self.ttl}


class Drain:
    """Tracks in-flight runs so shutdown can wait for them.

    Used as an async context manager around each run. After `close()` new runs
    are refused, which is what lets a rollout finish the work it has already
    paid for instead of throwing it away.
    """

    def __init__(self, grace: float = 30.0) -> None:
        self.grace = grace
        self.closing = False
        self._in_flight = 0
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def __enter__(self) -> Drain:
        if self.closing:
            raise ShuttingDown()
        self._in_flight += 1
        self._idle.clear()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._in_flight = max(0, self._in_flight - 1)
        if self._in_flight == 0:
            self._idle.set()

    async def close(self) -> None:
        """Refuse new runs, then wait up to `grace` for the current ones."""
        self.closing = True
        if self._in_flight:
            log.info("draining %d in-flight agent run(s)", self._in_flight)
            try:
                await asyncio.wait_for(self._idle.wait(), timeout=self.grace)
                log.info("all in-flight runs finished")
            except TimeoutError:
                log.warning(
                    "%d run(s) still in flight after %.0fs; shutting down anyway",
                    self._in_flight,
                    self.grace,
                )

    def snapshot(self) -> dict[str, Any]:
        return {"in_flight": self._in_flight, "draining": self.closing}


class ShuttingDown(Exception):
    """A new run was refused because the process is draining."""

    def __str__(self) -> str:
        return "this runtime is shutting down and is not accepting new work"
