"""Durable storage for operator findings.

Findings were an in-process `deque`: a restart, a rollout or a second replica
lost every one of them. That is the wrong property for the output of an
alert-triage run — the finding is often the only artifact a `read-only` operator
produces, and it is what an on-call engineer comes back to read.

The store reuses the CNPG database the RAG index already runs on
(`adhar-ai-rag`), so nothing new is provisioned. When no DSN is configured — the
default for `docker compose` and a bare `adhar-ai runtime` — every method is a
no-op and the deque is the whole story, exactly as before. Persistence is an
upgrade when the database is there, never a dependency.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .findings import Finding

log = logging.getLogger("adhar_ai.store")

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS {table} (
  id          TEXT PRIMARY KEY,
  operator    TEXT NOT NULL,
  severity    TEXT NOT NULL,
  created_at  DOUBLE PRECISION NOT NULL,
  payload     JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS {table}_recent ON {table} (created_at DESC);
CREATE INDEX IF NOT EXISTS {table}_operator ON {table} (operator, created_at DESC);
"""

INSERT_SQL = """
INSERT INTO {table} (id, operator, severity, created_at, payload)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload
"""

SELECT_SQL = "SELECT payload FROM {table} ORDER BY created_at DESC LIMIT %s"

#: Findings older than this are dropped on each start-up. A finding is a
#: point-in-time judgement about a cluster that has since moved on; keeping them
#: forever turns the table into a log nobody reads and the retention into
#: somebody's later problem.
DEFAULT_RETENTION_DAYS = 30

PRUNE_SQL = "DELETE FROM {table} WHERE created_at < %s"


class FindingStore:
    """Postgres-backed finding history. Every failure degrades to in-memory."""

    def __init__(
        self,
        dsn: str,
        table: str = "finding",
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        self.dsn = dsn
        # Interpolated into SQL, so it must be an identifier and not a fragment
        # of one. The value comes from the ConfigMap, which is not user input,
        # but "not user input today" is not a reason to leave it uncheckable.
        if not table.replace("_", "").isalnum():
            raise ValueError(f"invalid findings table name {table!r}")
        self.table = table
        self.retention_days = retention_days
        self.enabled = bool(dsn)
        self.status = "disabled (no database)" if not dsn else "pending"

    async def _connect(self) -> Any:
        import psycopg

        return await psycopg.AsyncConnection.connect(self.dsn)

    async def prepare(self) -> None:
        """Create the table and prune expired rows. Never raises."""
        if not self.enabled:
            return
        import time

        cutoff = time.time() - self.retention_days * 86400
        try:
            async with await self._connect() as conn, conn.cursor() as cur:
                await cur.execute(CREATE_SQL.format(table=self.table))
                await cur.execute(PRUNE_SQL.format(table=self.table), (cutoff,))
                await conn.commit()
            self.status = f"ready ({self.table})"
        except Exception as exc:  # noqa: BLE001
            self.enabled = False
            self.status = f"unavailable: {type(exc).__name__}: {exc}"
            log.warning("finding store unavailable, keeping findings in memory only: %s", exc)

    async def save(self, finding: Finding) -> None:
        """Persist one finding. A failure here must never fail the run that
        produced it — the finding is already in the in-memory deque and in the
        HTTP response, so the durable copy is the only thing lost."""
        if not self.enabled:
            return
        try:
            async with await self._connect() as conn, conn.cursor() as cur:
                await cur.execute(
                    INSERT_SQL.format(table=self.table),
                    (
                        finding.id,
                        finding.operator,
                        finding.severity,
                        finding.created_at,
                        json.dumps(finding.model_dump()),
                    ),
                )
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not persist finding %s: %s", finding.id, exc)

    async def recent(self, limit: int = 200) -> list[Finding]:
        """Findings from previous lifetimes, newest first. `[]` on any failure."""
        if not self.enabled:
            return []
        try:
            async with await self._connect() as conn, conn.cursor() as cur:
                await cur.execute(SELECT_SQL.format(table=self.table), (limit,))
                rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not load findings: %s", exc)
            return []
        loaded = []
        for (payload,) in rows:
            try:
                loaded.append(
                    Finding.model_validate(
                        payload if isinstance(payload, dict) else json.loads(payload)
                    )
                )
            except Exception:  # noqa: BLE001 - one bad row must not lose the rest
                continue
        return loaded
