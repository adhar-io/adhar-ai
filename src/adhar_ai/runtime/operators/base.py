from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from ..auth import Principal
from ..autonomy import OperatorPolicy, RuntimeConfig, lower_of
from ..findings import Citation, Finding
from ..loop import AgentResult, GatewayClient, Session, run
from ..toolbox import MCPToolbox


@dataclass(slots=True)
class OperatorContext:
    cfg: RuntimeConfig
    toolbox: MCPToolbox
    gateway: GatewayClient
    model: str | None = None
    #: Same pgvector-backed retriever `/chat` uses. Operators were the one path
    #: that ran ungrounded, which is backwards: a triage run needs the runbooks
    #: more than an interactive question does, because nobody is there to
    #: supply the missing context.
    retriever: Any = None
    #: Token presented to the LLM gateway for this operator's run. An operator
    #: usually has no user token to forward — an Alertmanager webhook carries a
    #: shared secret, and a poller carries nothing — so this is normally the
    #: runtime's own Keycloak service-account token.
    bearer: str = ""


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

    def session(
        self,
        grounding: list[str] | None = None,
        principal: Principal | None = None,
    ) -> Session:
        allowed = self.policy.allowed_tools or self.default_allowed_tools
        # The operator's own stage, the global default and the caller's ceiling
        # all narrow it; none of them may widen it.
        autonomy = lower_of(self.policy.autonomy, self.ctx.cfg.default_autonomy)
        if principal is not None:
            autonomy = principal.ceiling(autonomy)
        return Session(
            autonomy=autonomy,
            allowed_tools=allowed,
            tenant=f"operator:{self.name}",
            user=principal.subject if principal and principal.authenticated else None,
            model=self.ctx.model,
            max_steps=self.ctx.cfg.max_steps,
            max_tool_calls=self.ctx.cfg.max_tool_calls_per_op,
            grounding=grounding or [],
            write_policy=self.ctx.cfg.write_policy,
            bearer=principal.token if principal and principal.token else self.ctx.bearer,
        )

    async def grounding(self, event: dict[str, Any]) -> list[str]:
        if self.ctx.retriever is None:
            return []
        try:
            return await self.ctx.retriever.grounding(self.prompt(event)[:2000], k=5)
        except Exception:  # noqa: BLE001 - an ungrounded finding beats no finding
            return []

    async def handle(
        self, event: dict[str, Any], principal: Principal | None = None
    ) -> Finding:
        session = self.session(await self.grounding(event), principal)
        result: AgentResult = await run(
            self.ctx.gateway, self.ctx.toolbox, session, self.prompt(event), self.ctx.cfg
        )
        return self.to_finding(event, result, session)

    def to_finding(
        self, event: dict[str, Any], result: AgentResult, session: Session | None = None
    ) -> Finding:
        return Finding(
            id=f"{self.name}-{secrets.token_hex(4)}",
            operator=self.name,
            title=self.title(event),
            severity=self.severity(event),  # type: ignore[arg-type]
            summary=result.text or result.error,
            # The stage the run ACTUALLY executed at, which a caller's ceiling
            # may have lowered — not the one the ConfigMap asked for.
            autonomy=session.autonomy if session else self.policy.autonomy,
            subject=self.subject(event),
            evidence=result.tool_calls,
            citations=[
                Citation(source=c["tool"], kind="tool", detail=str(c.get("args", "")))
                for c in result.tool_calls
            ],
            recommendation=result.text,
            pull_request=result.pull_requests[0] if result.pull_requests else None,
        )
