"""Central token and rate budgets, per tenant.

In-process and therefore per-replica: the gateway Deployment runs a single
replica, which is what the platform manifest declares. Scaling it out means
moving this state to Redis — the interface here is deliberately narrow so that
swap is a one-file change.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from ..config import Budgets


class BudgetExceeded(Exception):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind


@dataclass
class _TenantState:
    day: int = 0
    tokens_today: int = 0
    requests: deque[float] = field(default_factory=deque)


class BudgetLedger:
    def __init__(self, budgets: Budgets) -> None:
        self.budgets = budgets
        self._tenants: dict[str, _TenantState] = defaultdict(_TenantState)

    @staticmethod
    def _today() -> int:
        return int(time.time() // 86400)

    def check(self, tenant: str, requested_max_tokens: int) -> None:
        """Pre-flight gate. Raises BudgetExceeded before any provider call."""
        state = self._tenants[tenant]
        today = self._today()
        if state.day != today:
            state.day, state.tokens_today = today, 0

        if requested_max_tokens > self.budgets.per_op_max_tokens:
            raise BudgetExceeded(
                "per_op_max_tokens",
                f"max_tokens {requested_max_tokens} exceeds the per-operation cap "
                f"{self.budgets.per_op_max_tokens}",
            )

        now = time.monotonic()
        window = self.budgets.rate_limit_requests_per_minute
        while state.requests and now - state.requests[0] > 60:
            state.requests.popleft()
        if len(state.requests) >= window:
            raise BudgetExceeded(
                "rate_limit",
                f"tenant {tenant!r} exceeded {window} requests/minute",
            )

        if state.tokens_today >= self.budgets.per_user_daily_tokens:
            raise BudgetExceeded(
                "per_user_daily_tokens",
                f"tenant {tenant!r} exhausted its daily budget of "
                f"{self.budgets.per_user_daily_tokens} tokens",
            )
        state.requests.append(now)

    def record(self, tenant: str, total_tokens: int) -> None:
        self._tenants[tenant].tokens_today += max(0, total_tokens)

    def snapshot(self, tenant: str) -> dict[str, int]:
        state = self._tenants[tenant]
        return {
            "tokens_today": state.tokens_today,
            "daily_token_budget": self.budgets.per_user_daily_tokens,
            "requests_last_minute": len(state.requests),
            "rate_limit_per_minute": self.budgets.rate_limit_requests_per_minute,
        }
