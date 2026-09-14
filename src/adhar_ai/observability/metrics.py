"""Prometheus metrics for the agent, its tools, its spend and its refusals.

Chosen for the questions an operator actually has to answer about an agent, in
roughly the order they get asked:

| Question | Metric |
|---|---|
| Is it working? | `adhar_ai_agent_runs_total{outcome}` |
| Is it slow? | `adhar_ai_agent_run_duration_seconds` |
| What is it costing me? | `adhar_ai_tokens_total`, `adhar_ai_cost_usd_total` |
| Is it trying things it should not? | `adhar_ai_denied_total{reason}` |
| Is it actually changing anything? | `adhar_ai_pull_requests_total` |
| Which backend is broken? | `adhar_ai_tool_calls_total{decision}`, `adhar_ai_circuit_state` |

`adhar_ai_denied_total` deserves its place. A steady trickle of policy refusals
is the system working. A spike is either a misconfiguration or something trying
to make the agent exceed its authority, and it is the one signal that
distinguishes them from "the agent is quiet today".

**Label cardinality is bounded on purpose.** Tool names, domains, outcomes and
autonomy stages are closed sets. The tenant is NOT a label: it is unbounded, and
an agentic platform with a label per user is a Prometheus outage waiting for a
busy afternoon. Per-tenant spend belongs in the audit stream, which already
carries it and is built for high cardinality.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

METRICS_CONTENT_TYPE = CONTENT_TYPE_LATEST

#: A private registry rather than the global default. The default registry picks
#: up process and GC collectors from anything that imports prometheus_client,
#: which makes what this service exports depend on its import graph. An explicit
#: registry means the metric list is exactly what is written below.
REGISTRY = CollectorRegistry()

#: An agent run is seconds to minutes, not milliseconds. The default histogram
#: buckets top out at 10s and would put almost every real run in `+Inf`.
RUN_BUCKETS = (1, 2.5, 5, 10, 20, 30, 60, 120, 300, 600)
#: A tool call is a single backend round trip.
CALL_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)

# --------------------------------------------------------------- agent runs --

AGENT_RUNS = Counter(
    "adhar_ai_agent_runs_total",
    "Agent runs, by how they ended.",
    ["outcome", "autonomy", "trigger"],
    registry=REGISTRY,
)
AGENT_RUN_SECONDS = Histogram(
    "adhar_ai_agent_run_duration_seconds",
    "Wall-clock time of one agent run.",
    ["trigger"],
    buckets=RUN_BUCKETS,
    registry=REGISTRY,
)
AGENT_STEPS = Histogram(
    "adhar_ai_agent_steps",
    "Plan-act-observe steps taken in one run. A run at the step limit is a run "
    "that did not converge.",
    ["trigger"],
    buckets=(1, 2, 3, 4, 6, 8, 12, 16, 24),
    registry=REGISTRY,
)

# -------------------------------------------------------------- tool calls --

TOOL_CALLS = Counter(
    "adhar_ai_tool_calls_total",
    "MCP tool calls, by outcome.",
    ["tool", "domain", "access", "decision"],
    registry=REGISTRY,
)
TOOL_SECONDS = Histogram(
    "adhar_ai_tool_call_duration_seconds",
    "Time for one MCP tool call, including the backend it fronts.",
    ["domain"],
    buckets=CALL_BUCKETS,
    registry=REGISTRY,
)

# ------------------------------------------------------------------ writes --

PULL_REQUESTS = Counter(
    "adhar_ai_pull_requests_total",
    "Pull requests the agent opened. The ONLY way it changes the platform, so "
    "this is the complete record of its effect on the world.",
    ["repo", "tool"],
    registry=REGISTRY,
)
DENIALS = Counter(
    "adhar_ai_denied_total",
    "Actions refused by policy, by reason. A spike is either a "
    "misconfiguration or an attempt to exceed the agent's authority.",
    ["reason", "tool"],
    registry=REGISTRY,
)

# -------------------------------------------------------------------- cost --

LLM_REQUESTS = Counter(
    "adhar_ai_llm_requests_total",
    "Completion requests to the LLM gateway.",
    ["model", "outcome"],
    registry=REGISTRY,
)
LLM_SECONDS = Histogram(
    "adhar_ai_llm_duration_seconds",
    "Time for one completion request.",
    ["model"],
    buckets=RUN_BUCKETS,
    registry=REGISTRY,
)
TOKENS = Counter(
    "adhar_ai_tokens_total",
    "Tokens billed, by direction. Matches the OTel GenAI convention "
    "(gen_ai.usage.input_tokens / output_tokens).",
    ["model", "direction"],
    registry=REGISTRY,
)
COST_USD = Counter(
    "adhar_ai_cost_usd_total",
    "Realized spend, where the gateway reports it. Absent rather than "
    "estimated when it does not: a guessed cost figure on a dashboard is worse "
    "than an empty panel, because somebody will budget against it.",
    ["model"],
    registry=REGISTRY,
)

# --------------------------------------------------------------- knowledge --

KNOWLEDGE_CHUNKS = Gauge(
    "adhar_ai_knowledge_chunks",
    "Indexed chunks in the knowledge base, by origin and kind.",
    ["origin", "kind"],
    registry=REGISTRY,
)
KNOWLEDGE_SEARCH_SECONDS = Histogram(
    "adhar_ai_knowledge_search_duration_seconds",
    "Grounding retrieval time, by which path answered.",
    ["mode"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
    registry=REGISTRY,
)

# ------------------------------------------------------------ dependencies --

MCP_CONNECTED = Gauge(
    "adhar_ai_mcp_servers_connected",
    "MCP domain servers with a live session. Below the configured count means "
    "the agent has silently lost a domain's tools.",
    registry=REGISTRY,
)
CIRCUIT_STATE = Gauge(
    "adhar_ai_circuit_state",
    "Circuit breaker per dependency: 0 closed (healthy), 1 half-open "
    "(probing), 2 open (failing fast).",
    ["target"],
    registry=REGISTRY,
)


def render_metrics() -> bytes:
    return generate_latest(REGISTRY)


# --------------------------------------------------------------------------- #
# Recording helpers. Every one is total-failure-tolerant: instrumentation must
# never be the reason an agent run fails.
# --------------------------------------------------------------------------- #


@contextmanager
def observe_agent_run(trigger: str, autonomy: str) -> Iterator[dict[str, Any]]:
    """Time one agent run and record how it ended.

    Yields a mutable dict; set `outcome` in it before the block exits. On an
    exception the outcome is recorded as `error` and the exception re-raised.
    """
    state: dict[str, Any] = {"outcome": "error"}
    started = time.monotonic()
    try:
        yield state
    finally:
        elapsed = time.monotonic() - started
        try:
            AGENT_RUNS.labels(state.get("outcome", "error"), autonomy, trigger).inc()
            AGENT_RUN_SECONDS.labels(trigger).observe(elapsed)
            if steps := state.get("steps"):
                AGENT_STEPS.labels(trigger).observe(float(steps))
        except Exception:  # noqa: BLE001 - never fail a run over a metric
            pass


@contextmanager
def observe_tool_call(tool: str, domain: str, access: str) -> Iterator[dict[str, str]]:
    """Time one tool call. Set `decision` in the yielded dict."""
    state = {"decision": "error"}
    started = time.monotonic()
    try:
        yield state
    finally:
        try:
            TOOL_CALLS.labels(tool, domain, access, state.get("decision", "error")).inc()
            TOOL_SECONDS.labels(domain).observe(time.monotonic() - started)
        except Exception:  # noqa: BLE001
            pass


def record_tool_call(
    tool: str, domain: str, access: str, decision: str, seconds: float
) -> None:
    """Record a tool call whose timing the caller already has.

    The context-manager form suits a `with` block; this suits a decorator that
    already measures its own duration and has to report a decision from two
    different exit paths.
    """
    try:
        TOOL_CALLS.labels(tool, domain, access, decision).inc()
        TOOL_SECONDS.labels(domain).observe(seconds)
    except Exception:  # noqa: BLE001
        pass


@contextmanager
def observe_llm_call(model: str) -> Iterator[dict[str, str]]:
    """Time one completion. Set `outcome` in the yielded dict."""
    state = {"outcome": "error"}
    started = time.monotonic()
    try:
        yield state
    finally:
        try:
            LLM_REQUESTS.labels(model or "unset", state.get("outcome", "error")).inc()
            LLM_SECONDS.labels(model or "unset").observe(time.monotonic() - started)
        except Exception:  # noqa: BLE001
            pass


@contextmanager
def observe_knowledge_search(mode: str) -> Iterator[None]:
    started = time.monotonic()
    try:
        yield
    finally:
        try:
            KNOWLEDGE_SEARCH_SECONDS.labels(mode).observe(time.monotonic() - started)
        except Exception:  # noqa: BLE001
            pass


def record_usage(model: str, usage: dict | None) -> None:
    """Record token counts and, where reported, realized cost.

    Cost is only recorded when the gateway actually returns it. Deriving it from
    a hardcoded price table would put a number on a dashboard that silently goes
    wrong the next time a provider changes its pricing, and somebody will budget
    against that number.
    """
    if not usage:
        return
    try:
        label = model or "unset"
        for key, direction in (
            ("prompt_tokens", "input"),
            ("input_tokens", "input"),
            ("completion_tokens", "output"),
            ("output_tokens", "output"),
        ):
            if value := usage.get(key):
                TOKENS.labels(label, direction).inc(float(value))
        for key in ("cost_usd", "cost", "total_cost"):
            if value := usage.get(key):
                COST_USD.labels(label).inc(float(value))
                break
    except Exception:  # noqa: BLE001
        pass


def record_pull_request(repo: str, tool: str) -> None:
    try:
        PULL_REQUESTS.labels(repo or "unknown", tool).inc()
    except Exception:  # noqa: BLE001
        pass


def record_denial(reason: str, tool: str = "") -> None:
    try:
        DENIALS.labels(reason, tool or "none").inc()
    except Exception:  # noqa: BLE001
        pass


def mcp_servers_connected(count: int) -> None:
    try:
        MCP_CONNECTED.set(count)
    except Exception:  # noqa: BLE001
        pass


def knowledge_chunks(counts: dict[tuple[str, str], int]) -> None:
    """Replace the knowledge gauges from a `{(origin, kind): chunks}` snapshot."""
    try:
        KNOWLEDGE_CHUNKS.clear()
        for (origin, kind), value in counts.items():
            KNOWLEDGE_CHUNKS.labels(origin, kind).set(value)
    except Exception:  # noqa: BLE001
        pass


def circuit_state(target: str, state: str) -> None:
    try:
        CIRCUIT_STATE.labels(target).set({"closed": 0, "half-open": 1, "open": 2}.get(state, 0))
    except Exception:  # noqa: BLE001
        pass
