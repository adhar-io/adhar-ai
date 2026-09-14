"""Operational signal for the agentic layer.

Every other Adhar package ships a ServiceMonitor and a Grafana dashboard. This
one shipped an audit log on stdout and nothing else, which means an operator
could reconstruct what the agent did yesterday but could not see what it is
doing now, what it costs, or whether it is failing.

Two surfaces, deliberately separate:

* **Metrics** (`metrics.py`) — aggregate, cheap, scraped. "Is the agent healthy,
  what is it spending, how often is policy refusing it."
* **Tracing** (`tracing.py`) — per-run, detailed, sampled. "Why did *this*
  run take ninety seconds and call the same tool four times."

Both are optional at runtime and neither is on the critical path: a broken
collector must never be able to stop the agent from answering.
"""

from .metrics import (
    METRICS_CONTENT_TYPE,
    circuit_state,
    knowledge_chunks,
    mcp_servers_connected,
    observe_agent_run,
    observe_knowledge_search,
    observe_llm_call,
    observe_tool_call,
    record_denial,
    record_pull_request,
    record_tool_call,
    record_usage,
    render_metrics,
)
from .tracing import setup_tracing, span

__all__ = [
    "METRICS_CONTENT_TYPE",
    "circuit_state",
    "knowledge_chunks",
    "mcp_servers_connected",
    "observe_agent_run",
    "observe_knowledge_search",
    "observe_llm_call",
    "observe_tool_call",
    "record_denial",
    "record_pull_request",
    "record_tool_call",
    "record_usage",
    "render_metrics",
    "setup_tracing",
    "span",
]
