from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from ..autonomy import OperatorPolicy, RuntimeConfig
from ..findings import Citation, Finding
from ..loop import AgentResult, GatewayClient, Session, run
from ..toolbox import MCPToolbox


@dataclass(slots=True)
class OperatorContext:
    cfg: RuntimeConfig
    toolbox: MCPToolbox
    gateway: GatewayClient
    model: str | None = None


class Operator:
    """Base class. Subclasses supply `name`, `trigger`, a prompt and a
    severity/title derivation from the event."""

    name: str = "operator"
    trigger: str = "manual"
    default_allowed_tools: tuple[str, ...] = ()
    default_autonomy: str = "suggest"

    def __init__(self, ctx: OperatorContext) -> None:
        self.ctx = ctx
        self.policy: OperatorPolicy = ctx.cfg.operator(self.name)

    # ------------------------------------------------------------- overrides --

    def prompt(self, event: dict[str, Any]) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def title(self, event: dict[str, Any]) -> str:
        return f"{self.name} finding"

    def severity(self, event: dict[str, Any]) -> str:
        return "info"

    def subject(self, event: dict[str, Any]) -> dict[str, Any]:
        return {}

    # ------------------------------------------------------------------ run --

    def session(self) -> Session:
        allowed = self.policy.allowed_tools or self.default_allowed_tools
        return Session(
            autonomy=self.policy.autonomy,
            allowed_tools=allowed,
            tenant=f"operator:{self.name}",
            user=None,
            model=self.ctx.model,
            max_steps=self.ctx.cfg.max_steps,
            max_tool_calls=self.ctx.cfg.max_tool_calls_per_op,
        )

    async def handle(self, event: dict[str, Any]) -> Finding:
        result: AgentResult = await run(
            self.ctx.gateway, self.ctx.toolbox, self.session(), self.prompt(event), self.ctx.cfg
        )
        return self.to_finding(event, result)

    def to_finding(self, event: dict[str, Any], result: AgentResult) -> Finding:
        return Finding(
            id=f"{self.name}-{secrets.token_hex(4)}",
            operator=self.name,
            title=self.title(event),
            severity=self.severity(event),  # type: ignore[arg-type]
            summary=result.text or result.error,
            autonomy=self.policy.autonomy,
            subject=self.subject(event),
            evidence=result.tool_calls,
            citations=[
                Citation(source=c["tool"], kind="tool", detail=str(c.get("args", "")))
                for c in result.tool_calls
            ],
            recommendation=result.text,
            pull_request=result.pull_requests[0] if result.pull_requests else None,
        )
