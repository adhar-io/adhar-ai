"""The structured record every operator produces.

A finding is emitted whether or not a PR follows: `read-only` operators produce
findings and stop, which is the point of the bottom rung of the ladder.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..provenance import ORIGIN_LABEL_KEY, ORIGIN_LABEL_VALUE

Severity = Literal["info", "warning", "critical"]


class Citation(BaseModel):
    source: str
    kind: str = "tool"
    detail: str = ""


class Finding(BaseModel):
    id: str
    operator: str
    title: str
    severity: Severity = "info"
    summary: str = ""
    autonomy: str = "suggest"
    subject: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    recommendation: str = ""
    #: Set when the finding produced a Gitea PR. `None` means no change was
    #: proposed — either the autonomy level forbids writes or none was needed.
    pull_request: dict[str, Any] | None = None
    created_at: float = Field(default_factory=time.time)
    origin: str = Field(default=ORIGIN_LABEL_VALUE)

    def as_labels(self) -> dict[str, str]:
        return {ORIGIN_LABEL_KEY: ORIGIN_LABEL_VALUE, "adhar.io/operator": self.operator}
