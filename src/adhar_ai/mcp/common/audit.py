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

from mcp.server.mcpserver.exceptions import ToolError

from ...clients.errors import BackendNotConfigured, WriteNotPermitted
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


#: Failures Adhar ANTICIPATES, whose message the model is meant to read.
#:
#: The MCP SDK splits tool failures in two: a `ToolError` is an expected
#: outcome and its message is returned to the client, while any other exception
#: is a crash whose text is withheld — the model sees only "Error executing tool
#: <name>". `BackendNotConfigured` was landing in the second bucket, so the one
#: property `clients/errors.py` exists to provide was lost at the boundary: the
#: agent could not distinguish "Prometheus is not configured here" from "the
#: tool crashed", and the system prompt's instruction to say so plainly had
#: nothing to say it from.
EXPECTED_FAILURES: tuple[type[Exception], ...] = (
    BackendNotConfigured,
    WriteNotPermitted,
    ValueError,
    KeyError,
)


def _anticipated(exc: BaseException) -> bool:
    """Is this a failure the model should be told about in full?

    The two families below carry the most useful text the agent ever sees from a
    backend, and both were being masked as crashes:

    * `ApiException` — "namespaces is forbidden: User cannot list resource" is an
      RBAC answer, and "the server could not find the requested resource" means
      a CRD is not installed. Masked, both become "Error executing tool", and the
      agent reports a broken tool instead of a missing permission.
    * `HTTPStatusError` — a 404 from ArgoCD means the application does not exist,
      which is the answer to the question, not a fault.
    """
    if isinstance(exc, EXPECTED_FAILURES):
        return True
    return isinstance(exc, _backend_failures())


@functools.cache
def _backend_failures() -> tuple[type[BaseException], ...]:
    """Backend error BASE classes, resolved once.

    By type rather than by class name: the Kubernetes client raises
    `NotFoundException`, `ForbiddenException` and friends, all subclasses of
    `ApiException`. A name check catches the base and misses every subclass —
    which is to say, it misses every error that is actually raised.
    """
    found: list[type[BaseException]] = []
    try:
        from kubernetes.client.exceptions import ApiException

        found.append(ApiException)
    except Exception:  # noqa: BLE001 - optional at import time
        pass
    try:
        import httpx

        found.append(httpx.HTTPError)
    except Exception:  # noqa: BLE001
        pass
    return tuple(found)


def _expected(exc: Exception) -> Exception:
    """Re-raise an anticipated failure as a `ToolError` so its text survives."""
    if isinstance(exc, ToolError):
        return exc
    if _anticipated(exc):
        return ToolError(f"{type(exc).__name__}: {str(exc)[:600]}")
    return exc


def _record(tool: str, access: str, domain: str, decision: str, started: float) -> None:
    """Count this call in the SERVER's own metrics.

    Not only in the runtime's loop: an external agent — Claude Code, an IDE,
    ChatOps — reaches these tools through agentgateway and never touches the
    runtime, so runtime-side instrumentation alone would leave the entire
    outward MCP surface invisible.
    """
    from ...observability import record_tool_call

    record_tool_call(tool, domain, access, decision, time.monotonic() - started)


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
                _record(fn.__name__, access, domain, "error", started)
                raise _expected(exc) from exc
            _record(fn.__name__, access, domain, "ok", started)
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
