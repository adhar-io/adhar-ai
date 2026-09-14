"""Metrics, and the dashboard that reads them.

The drift test at the bottom is the one that earns its place. A Grafana panel
querying a metric nobody exports renders as an empty graph, which looks exactly
like "this is not happening" — so a dashboard can be wrong for months and the
only symptom is false reassurance. Here the panel list is checked against the
live registry.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest
import yaml

from adhar_ai.observability import metrics

#: The dashboard lives in the sibling platform repo, which is not always checked
#: out beside this one (it is not in CI). Found relative to this file rather than
#: by an absolute path, and the tests that need it skip when it is absent.
_RELATIVE = pathlib.Path(
    "platform/stack/packages/ai/adhar-ai/manifests/dashboard.yaml"
)


def _find_dashboard() -> pathlib.Path | None:
    import os

    if override := os.environ.get("ADHAR_PLATFORM_REPO"):
        candidate = pathlib.Path(override) / _RELATIVE
        return candidate if candidate.exists() else None
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        for sibling in ("adhar", "platform"):
            candidate = parent / sibling / _RELATIVE
            if candidate.exists():
                return candidate
    return None


DASHBOARD = _find_dashboard()


def exported_series() -> set[str]:
    """Every series name the registry can emit, suffixes included."""
    names: set[str] = set()
    for metric in metrics.REGISTRY.collect():
        names.add(metric.name)
        for suffix in ("_total", "_bucket", "_count", "_sum"):
            names.add(metric.name + suffix)
    return names


# --------------------------------------------------------------------------- #
# Exposition
# --------------------------------------------------------------------------- #


def test_the_registry_renders_valid_exposition() -> None:
    body = metrics.render_metrics().decode()
    assert "# HELP adhar_ai_agent_runs_total" in body
    assert "# TYPE adhar_ai_agent_runs_total counter" in body


def test_a_private_registry_is_used() -> None:
    """The default registry picks up process and GC collectors from anything
    that imports prometheus_client, which would make what this service exports
    depend on its import graph."""
    body = metrics.render_metrics().decode()
    assert "python_gc_objects" not in body
    assert "process_cpu_seconds" not in body


def test_every_metric_is_namespaced() -> None:
    for metric in metrics.REGISTRY.collect():
        assert metric.name.startswith("adhar_ai_"), metric.name


def test_every_metric_documents_itself() -> None:
    """An undocumented metric on a dashboard is a number nobody can act on."""
    for metric in metrics.REGISTRY.collect():
        assert metric.documentation, f"{metric.name} has no HELP text"
        assert len(metric.documentation) > 20, f"{metric.name}: {metric.documentation}"


def test_the_tenant_is_not_a_label() -> None:
    """Unbounded cardinality. An agentic platform with a label per user is a
    Prometheus outage waiting for a busy afternoon; per-tenant spend belongs in
    the audit stream, which is built for it."""
    for metric in metrics.REGISTRY.collect():
        for sample in metric.samples:
            assert "tenant" not in sample.labels, f"{metric.name} labels by tenant"
            assert "user" not in sample.labels, f"{metric.name} labels by user"


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #


def test_an_agent_run_is_counted_with_its_outcome() -> None:
    with metrics.observe_agent_run("chat", "read-only") as state:
        state["outcome"] = "answer"
        state["steps"] = 3
    body = metrics.render_metrics().decode()
    assert 'adhar_ai_agent_runs_total{autonomy="read-only",outcome="answer",trigger="chat"}' in body


def test_a_raised_exception_is_recorded_as_an_error_and_re_raised() -> None:
    with pytest.raises(RuntimeError):
        with metrics.observe_agent_run("chat", "suggest"):
            raise RuntimeError("boom")
    assert 'outcome="error"' in metrics.render_metrics().decode()


def test_usage_records_tokens_under_either_naming() -> None:
    """Providers disagree: OpenAI says prompt/completion, the OTel GenAI
    convention says input/output. Both arrive."""
    metrics.record_usage("m1", {"prompt_tokens": 10, "completion_tokens": 5})
    metrics.record_usage("m2", {"input_tokens": 7, "output_tokens": 3})
    body = metrics.render_metrics().decode()
    assert 'adhar_ai_tokens_total{direction="input",model="m1"} 10.0' in body
    assert 'adhar_ai_tokens_total{direction="output",model="m2"} 3.0' in body


def test_cost_is_recorded_only_when_reported() -> None:
    """Deriving it from a hardcoded price table would put a number on a
    dashboard that silently goes wrong the next time a provider reprices."""
    metrics.record_usage("priced", {"prompt_tokens": 1, "cost_usd": 0.25})
    metrics.record_usage("unpriced", {"prompt_tokens": 1})
    body = metrics.render_metrics().decode()
    assert 'adhar_ai_cost_usd_total{model="priced"} 0.25' in body
    assert 'adhar_ai_cost_usd_total{model="unpriced"}' not in body


def test_instrumentation_never_raises() -> None:
    """A metric must never be the reason an agent run fails."""
    metrics.record_usage("m", None)
    metrics.record_usage("m", {"prompt_tokens": "not a number"})
    metrics.record_denial("reason")
    metrics.knowledge_chunks({})
    metrics.circuit_state("x", "nonsense-state")
    metrics.mcp_servers_connected(3)


def test_knowledge_gauges_are_replaced_not_accumulated() -> None:
    """A gauge set from a snapshot must not keep a stale origin alive after that
    origin stops being indexed."""
    metrics.knowledge_chunks({("docs", "adr"): 500, ("notes", "note"): 3})
    metrics.knowledge_chunks({("docs", "adr"): 600})
    body = metrics.render_metrics().decode()
    assert 'adhar_ai_knowledge_chunks{kind="adr",origin="docs"} 600.0' in body
    assert 'origin="notes"' not in body


def test_circuit_states_map_to_numbers_a_dashboard_can_alert_on() -> None:
    metrics.circuit_state("llm-gateway", "open")
    assert 'adhar_ai_circuit_state{target="llm-gateway"} 2.0' in metrics.render_metrics().decode()


# --------------------------------------------------------------------------- #
# The dashboard must not drift from the code
# --------------------------------------------------------------------------- #


needs_platform = pytest.mark.skipif(
    DASHBOARD is None, reason="the platform repo is not checked out beside this one"
)


@needs_platform
def test_every_dashboard_query_names_a_metric_that_exists() -> None:
    """A panel querying a metric nobody exports renders as an empty graph, which
    looks exactly like "this is not happening". That is false reassurance, and
    it is invisible until someone needs the panel."""
    config = yaml.safe_load(DASHBOARD.read_text())
    dashboard = json.loads(config["data"]["adhar-ai.json"])

    referenced: set[str] = set()
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            referenced.update(re.findall(r"\badhar_ai_[a-z_]+\b", target["expr"]))

    assert referenced, "the dashboard queries nothing"
    missing = sorted(referenced - exported_series())
    assert not missing, f"dashboard panels query metrics this repo does not export: {missing}"


@needs_platform
def test_every_dashboard_panel_explains_itself() -> None:
    config = yaml.safe_load(DASHBOARD.read_text())
    dashboard = json.loads(config["data"]["adhar-ai.json"])
    undocumented = [p["title"] for p in dashboard["panels"] if not p.get("description")]
    assert not undocumented, f"panels with no description: {undocumented}"
