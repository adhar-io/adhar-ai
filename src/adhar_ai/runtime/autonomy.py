"""Staged autonomy, read from the `adhar-ai-config` ConfigMap.

The ladder (conservative by default — the ConfigMap ships `suggest`):

  read-only         investigate and answer; write tools are not even offered
  suggest           write tools allowed, every write yields a PR and pauses
  approve-to-apply  write yields a PR; CI may auto-merge under policy
  scoped            narrow, policy-gated auto-merge on allowlisted paths

Every rung above `read-only` still produces a Git pull request. None of them
mutates a cluster — that property is structural (the MCP servers expose no
apply tool at all), not a setting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

LADDER = ("read-only", "suggest", "approve-to-apply", "scoped")

DEFAULT_MCP_SERVERS = {
    domain: f"http://adhar-ai-mcp-{domain}.adhar-system.svc.cluster.local:8080"
    for domain in ("cluster", "gitops", "provision", "observability", "security", "cost", "catalog")
}


class AutonomyError(ValueError):
    pass


def rank(level: str) -> int:
    try:
        return LADDER.index(level)
    except ValueError as exc:
        raise AutonomyError(f"unknown autonomy level {level!r}; expected one of {LADDER}") from exc


@dataclass(slots=True)
class OperatorPolicy:
    name: str
    trigger: str = "manual"
    autonomy: str = "suggest"
    allowed_tools: tuple[str, ...] = ()

    @property
    def may_write(self) -> bool:
        return rank(self.autonomy) > rank("read-only")


@dataclass(slots=True)
class WritePolicy:
    allowed_repos: tuple[str, ...] = ("packages", "environments")
    allowed_path_prefixes: tuple[str, ...] = ("packages/", "environments/")


@dataclass(slots=True)
class RuntimeConfig:
    default_autonomy: str = "suggest"
    max_steps: int = 12
    max_tool_calls_per_op: int = 40
    write_policy: WritePolicy = field(default_factory=WritePolicy)
    operators: dict[str, OperatorPolicy] = field(default_factory=dict)
    mcp_servers: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MCP_SERVERS))
    rag_enabled: bool = True
    rag_database: str = "adhar_ai_rag"
    rag_table: str = "kb_chunk"

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> RuntimeConfig:
        autonomy = (data.get("autonomy") or {}).get("default", "suggest")
        rank(autonomy)  # validate early — a typo must not silently widen authority
        limits = data.get("limits") or {}
        wp = data.get("writePolicy") or {}
        rag = data.get("rag") or {}
        operators = {}
        for name, raw in (data.get("operators") or {}).items():
            raw = raw or {}
            level = raw.get("autonomy", autonomy)
            rank(level)
            operators[name] = OperatorPolicy(
                name=name,
                trigger=str(raw.get("trigger", "manual")),
                autonomy=level,
                allowed_tools=tuple(raw.get("allowedTools") or ()),
            )
        return cls(
            default_autonomy=autonomy,
            max_steps=int(limits.get("maxSteps", 12)),
            max_tool_calls_per_op=int(limits.get("maxToolCallsPerOp", 40)),
            write_policy=WritePolicy(
                allowed_repos=tuple(wp.get("allowedRepos") or ("packages", "environments")),
                allowed_path_prefixes=tuple(
                    wp.get("allowedPathPrefixes") or ("packages/", "environments/")
                ),
            ),
            operators=operators,
            mcp_servers={**DEFAULT_MCP_SERVERS, **(data.get("mcpServers") or {})},
            rag_enabled=bool(rag.get("enabled", True)),
            rag_database=str(rag.get("database", "adhar_ai_rag")),
            rag_table=str(rag.get("table", "kb_chunk")),
        )

    @classmethod
    def load(cls, path: str | Path | None) -> RuntimeConfig:
        """Load the ConfigMap-mounted `config.yaml`; fall back to the shipped
        defaults (which are the ConfigMap's own values) when absent."""
        if not path:
            return cls()
        p = Path(path)
        if not p.exists():
            return cls()
        return cls.from_mapping(yaml.safe_load(p.read_text()) or {})

    def operator(self, name: str) -> OperatorPolicy:
        if name in self.operators:
            return self.operators[name]
        return OperatorPolicy(name=name, autonomy=self.default_autonomy)
