"""A small response cache for identical, deterministic completions.

ADR-0024 lists caching among the mitigations for LLM cost. `ADHAR_AI_RESPONSE_CACHE`
was read from the environment and then consulted by nothing, so the setting was
a claim rather than a control. This is the control.

It caches only what is safe to cache, which is a narrow slice on purpose:

* **`temperature == 0` only.** Anything else is asking the provider for
  variation, and serving a stored answer would silently take it away.
* **Keyed on the whole request** — model, every message, the tool specs and the
  tool choice. Two requests share a cache entry only when the provider would
  have been asked exactly the same question.
* **Per tenant.** A completion is derived from one caller's prompt, which may
  contain platform state that caller could read and another could not. Sharing
  across tenants would turn a cache into a disclosure channel.
* **Short TTL.** The agent asks about a cluster that changes underneath it; a
  stale answer about live state is worse than a slow one.

The win is real in the loop that motivates it: a retry after a tool error, a
duplicated operator event, or two engineers asking the same question minutes
apart all replay one prompt verbatim.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from typing import Any

#: Entries expire quickly — the subject matter is live cluster state.
DEFAULT_TTL_SECONDS = 300
DEFAULT_MAX_ENTRIES = 256


def cache_key(tenant: str, body: Any) -> str:
    """A stable digest of everything that would change the provider's answer."""
    material = json.dumps(
        {
            "tenant": tenant,
            "model": getattr(body, "model", None),
            "max_tokens": getattr(body, "max_tokens", None),
            "temperature": getattr(body, "temperature", None),
            "tool_choice": getattr(body, "tool_choice", None),
            "messages": [
                m.model_dump(exclude_none=True) for m in (getattr(body, "messages", None) or [])
            ],
            "tools": [t.model_dump() for t in (getattr(body, "tools", None) or [])],
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ResponseCache:
    """Bounded, TTL'd, in-process LRU. Disabled instances are a no-op."""

    def __init__(
        self,
        enabled: bool = True,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self.enabled = enabled
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def cacheable(self, body: Any) -> bool:
        """Only deterministic, non-streaming requests."""
        if not self.enabled:
            return False
        if getattr(body, "stream", False):
            return False
        # `None` means "provider default", which is not necessarily zero.
        return getattr(body, "temperature", None) == 0

    def get(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        stored_at, value = entry
        if time.time() - stored_at > self.ttl_seconds:
            del self._entries[key]
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return value

    def put(self, key: str, value: Any) -> None:
        self._entries[key] = (time.time(), value)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "entries": len(self._entries),
            "hits": self.hits,
            "misses": self.misses,
            "ttl_seconds": self.ttl_seconds,
            "cacheable": "temperature=0, non-streaming, per tenant",
        }
