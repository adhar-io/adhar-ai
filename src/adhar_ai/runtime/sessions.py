"""Multi-turn conversation memory.

`/chat` accepted a `session` field and used it as a tenant label. Nothing was
remembered, so every question started from nothing: "why is it degraded?"
followed by "and what should I do about it?" made the agent re-investigate from
scratch, spending a second full run to rediscover what it had just been told.

A `Conversation` is the fix: a bounded, expiring window of prior turns, replayed
into the next prompt.

Three decisions worth stating, because each is a trade:

**Bounded by turns, not tokens.** Token-accurate trimming needs a tokenizer per
provider, and the provider is chosen by a model name at request time. Turns are
a coarser bound that never disagrees with the gateway about what it counts.

**Summaries, not transcripts.** Replaying whole tool transcripts would blow the
window open after three questions — a single `list_pods` result is larger than
most answers. What is kept is the question and the answer, which is what a
follow-up actually refers to.

**Expiring.** A conversation is a working context, not a record. The audit
stream is the record. Thirty minutes of silence and the next question starts
clean, which is usually what the asker intended anyway.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("adhar_ai.sessions")

#: Prior exchanges replayed into a new prompt. Four is enough for "and what
#: about the other one?" and short enough that it never dominates the window.
DEFAULT_TURNS = 4
#: Silence after which a conversation is considered finished.
DEFAULT_TTL = 1800.0
#: Per-turn cap. A long answer is summarised by truncation rather than dropped,
#: because the shape of the previous answer is most of what a follow-up needs.
MAX_TURN_CHARS = 1200


@dataclass(slots=True)
class Turn:
    prompt: str
    answer: str
    at: float = field(default_factory=time.time)
    #: Tools used, so a follow-up can be told what has already been looked at
    #: rather than looking at it again.
    tools: list[str] = field(default_factory=list)

    def as_messages(self) -> list[dict[str, str]]:
        answer = self.answer[:MAX_TURN_CHARS]
        if len(self.answer) > MAX_TURN_CHARS:
            answer += " […]"
        return [
            {"role": "user", "content": self.prompt[:MAX_TURN_CHARS]},
            {"role": "assistant", "content": answer},
        ]


@dataclass
class Conversation:
    id: str
    requester: str = "anonymous"
    turns: deque[Turn] = field(default_factory=lambda: deque(maxlen=DEFAULT_TURNS))
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    #: Tasks started from this conversation, so "how is that going?" resolves.
    task_ids: list[str] = field(default_factory=list)

    def record(self, prompt: str, answer: str, tools: list[str] | None = None) -> None:
        self.turns.append(Turn(prompt=prompt, answer=answer, tools=list(tools or [])))
        self.updated_at = time.time()

    def history(self) -> list[dict[str, str]]:
        """Prior turns as messages, oldest first."""
        return [m for turn in self.turns for m in turn.as_messages()]

    def context_note(self) -> str:
        """A line telling the agent what has already been looked at.

        Without it a follow-up re-runs the same tools: the model cannot tell
        from a replayed answer which parts were retrieved and which were
        reasoned, so it re-retrieves everything to be safe.
        """
        seen = sorted({tool for turn in self.turns for tool in turn.tools})
        if not seen:
            return ""
        return (
            "Earlier in this conversation you already called: "
            + ", ".join(f"`{t}`" for t in seen)
            + ". Do not repeat a call whose result is still in the history above "
            "unless the answer depends on it having changed."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "requester": self.requester,
            "turns": len(self.turns),
            "tasks": list(self.task_ids),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ConversationStore:
    """Bounded, expiring, in-process.

    In process on purpose. A conversation is short-lived working context worth
    at most a few minutes of latency saving, so paying a database round trip per
    turn to share it across replicas would cost more than it returns. What
    genuinely must survive a restart is the TASK, which is durable.
    """

    def __init__(
        self,
        max_conversations: int = 1000,
        ttl: float = DEFAULT_TTL,
        turns: int = DEFAULT_TURNS,
    ) -> None:
        self.ttl = ttl
        self.turns = turns
        self.max_conversations = max_conversations
        self._conversations: OrderedDict[str, Conversation] = OrderedDict()

    def get(self, session_id: str, requester: str = "anonymous") -> Conversation | None:
        """An existing, unexpired conversation, or `None`."""
        if not session_id:
            return None
        conversation = self._conversations.get(session_id)
        if conversation is None:
            return None
        if time.time() - conversation.updated_at > self.ttl:
            del self._conversations[session_id]
            return None
        if conversation.requester != requester:
            # Two callers must never share a window: one person's cluster
            # detail would appear in another's prompt. A collision returns a
            # fresh conversation rather than someone else's.
            log.warning("session %s was claimed by a different requester", session_id)
            return None
        self._conversations.move_to_end(session_id)
        return conversation

    def open(self, session_id: str, requester: str = "anonymous") -> Conversation:
        existing = self.get(session_id, requester)
        if existing is not None:
            return existing
        conversation = Conversation(
            id=session_id,
            requester=requester,
            turns=deque(maxlen=self.turns),
        )
        self._conversations[session_id] = conversation
        self._conversations.move_to_end(session_id)
        while len(self._conversations) > self.max_conversations:
            self._conversations.popitem(last=False)
        return conversation

    def expire(self) -> int:
        """Drop finished conversations. Returns how many went."""
        cutoff = time.time() - self.ttl
        stale = [k for k, c in self._conversations.items() if c.updated_at < cutoff]
        for key in stale:
            del self._conversations[key]
        return len(stale)

    def snapshot(self) -> dict[str, Any]:
        return {
            "conversations": len(self._conversations),
            "ttl_seconds": self.ttl,
            "turns_kept": self.turns,
        }
