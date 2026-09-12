"""Structured audit events.

Every tool call emits one JSON line on stdout. Alloy scrapes container stdout
into Loki, so the platform's "Adhar AI" dashboard gets the audit trail for free
without this process holding a Loki *write* credential.
"""

from __future__ import annotations

import functools
import json
import logging
import secrets
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from ...provenance import ORIGIN_LABEL_KEY, ORIGIN_LABEL_VALUE

_logger = logging.getLogger("adhar_ai.audit")

#: Never let a credential-shaped value reach the audit stream or a prompt.
_REDACT_KEYS = {"token", "password", "apikey", "api_key", "secret", "authorization", "credential"}


def new_audit_id() -> str:
    return f"aud-{secrets.token_hex(8)}"


def redact(value: Any, _depth: int = 0) -> Any:
    if _depth > 6:
        return "…"
    if isinstance(value, dict):
        return {
            k: ("***" if k.lower().replace("-", "_") in _REDACT_KEYS else redact(v, _depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v, _depth + 1) for v in value[:20]]
    if isinstance(value, str) and len(value) > 200:
        return value[:200] + "…"
    return value


def emit(**fields: Any) -> None:
    record = {
        "ts": time.time(),
        ORIGIN_LABEL_KEY: ORIGIN_LABEL_VALUE,
        **redact(fields),
    }
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)


F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def audited(access: str, domain: str) -> Callable[[F], F]:
    """Decorator emitting `{audit_id, tool, access, domain, args, decision}`.

    Failures are audited too — a denied or errored tool call is exactly the
    event an operator most wants to see.
    """

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            audit_id = new_audit_id()
            started = time.monotonic()
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:
                emit(
                    audit_id=audit_id,
                    tool=fn.__name__,
                    access=access,
                    domain=domain,
                    args=kwargs,
                    decision="error",
                    error=f"{type(exc).__name__}: {exc}",
                    duration_ms=round((time.monotonic() - started) * 1000, 1),
                )
                raise
            emit(
                audit_id=audit_id,
                tool=fn.__name__,
                access=access,
                domain=domain,
                args=kwargs,
                decision="ok",
                duration_ms=round((time.monotonic() - started) * 1000, 1),
            )
            return result

        return wrapper  # type: ignore[return-value]

    return decorator
