"""OpenTelemetry spans, using the GenAI semantic conventions.

Metrics say the agent is slow. Traces say *why*: which tool it called four times,
which backend took ninety seconds, where in a plan-act-observe loop the time
went. For an agent that is the difference between a number and a diagnosis.

The attribute names follow the OTel **GenAI semantic conventions**
(`gen_ai.request.model`, `gen_ai.usage.input_tokens`, …) on purpose. ADR-0025
has agentgateway emitting exactly those for the LLM and MCP hops, and the
platform's Grafana dashboard queries them. Inventing a private naming scheme
here would produce two vocabularies for one request path and a dashboard that
only ever shows half of it.

**Entirely optional, and deliberately so.** The SDK is not a dependency. With it
absent, or with no endpoint configured, every function here is a no-op that
costs a branch. Telemetry must never be on the critical path of an agent that is
trying to tell somebody why production is down.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from ..config import env, env_bool

log = logging.getLogger("adhar_ai.tracing")

#: Set once by `setup_tracing`. `None` means tracing is off and `span()` is a
#: no-op; it is never re-checked on the hot path.
_TRACER: Any = None

SERVICE_NAME = "adhar-ai"


def setup_tracing(component: str) -> bool:
    """Install an OTLP exporter if one is configured and the SDK is present.

    Returns whether tracing is live, so `/healthz` can say so rather than
    leaving an operator guessing why their Tempo is empty.
    """
    global _TRACER

    endpoint = env(
        "ADHAR_AI_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    )
    if not endpoint or env_bool("ADHAR_AI_TRACING_DISABLED"):
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ModuleNotFoundError as exc:
        # The API ships with other dependencies; the SDK and exporter are the
        # optional `tracing` extra. Absent means off, not broken.
        log.info("tracing not installed (%s); set the `tracing` extra to enable it", exc)
        return False

    try:
        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": SERVICE_NAME,
                    "service.namespace": "adhar-system",
                    "adhar.io/component": component,
                    "adhar.io/origin": "adhar-ai",
                }
            )
        )
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
        )
        trace.set_tracer_provider(provider)
        _TRACER = trace.get_tracer(SERVICE_NAME)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not start tracing (%s); continuing without it", exc)
        return False

    log.info("tracing to %s as %s/%s", endpoint, SERVICE_NAME, component)
    return True


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Open a span, or do nothing if tracing is off.

    Records an exception on the span and re-raises it, so a failed run is
    visible as a failed trace rather than a trace that simply stops.
    """
    if _TRACER is None:
        yield None
        return
    with _TRACER.start_as_current_span(name) as current:
        try:
            for key, value in attributes.items():
                if value is not None:
                    current.set_attribute(key, value)
        except Exception:  # noqa: BLE001
            pass
        try:
            yield current
        except Exception as exc:
            try:
                current.record_exception(exc)
                from opentelemetry.trace import Status, StatusCode

                current.set_status(Status(StatusCode.ERROR, str(exc)))
            except Exception:  # noqa: BLE001
                pass
            raise


def set_usage(current: Any, usage: dict | None) -> None:
    """Attach GenAI usage attributes, matching what agentgateway emits."""
    if current is None or not usage:
        return
    try:
        for key, attribute in (
            ("prompt_tokens", "gen_ai.usage.input_tokens"),
            ("input_tokens", "gen_ai.usage.input_tokens"),
            ("completion_tokens", "gen_ai.usage.output_tokens"),
            ("output_tokens", "gen_ai.usage.output_tokens"),
            ("cost_usd", "gen_ai.usage.cost_usd"),
        ):
            if (value := usage.get(key)) is not None:
                current.set_attribute(attribute, value)
    except Exception:  # noqa: BLE001
        pass
