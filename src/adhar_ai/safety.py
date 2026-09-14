"""Controls on what leaves this process, and which models it may reach.

ADR-0025 puts prompt guards and budgets in agentgateway, and that is the right
place for them: one enforcement point in front of every AI request. This module
is not a second copy of that. It covers the two things the data plane cannot.

**Outbound credential scanning.** The agent's prompts are assembled here, from
tool output it did not write: pod environment blocks, log lines, ConfigMap
contents, PR diffs. A Secret mounted as an env var and echoed into a crash log
becomes part of a prompt automatically, and no amount of care in the system
prompt prevents it. agentgateway masks credentials too — but it sees the request
after it has crossed the network, and by then the value has already left the
process that was trusted with it. Masking here is the difference between "a
credential was redacted in transit" and "a credential never left".

**A model allow-list.** Under agentgateway the caller names a model and the
gateway routes on it, which means a request body chooses how much a run costs.
Naming a model nobody budgeted for is not an attack, it is a Tuesday — somebody
copies a curl from a blog post. The allow-list is empty by default (anything is
permitted) because an allow-list that blocks the platform's own default model on
upgrade day is worse than none.

Both are **fail-safe, not fail-closed**: a pattern that does not compile, or a
scan that throws, must not stop an agent from answering. They reduce exposure;
they are not the boundary. The boundary is that the agent holds no credential
that can mutate anything.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("adhar_ai.safety")

MASK = "«redacted by adhar-ai»"

#: Credential shapes worth masking on the way out. Anchored on structure —
#: prefixes, lengths, framing — rather than on the word "password", because the
#: dangerous case is a value with no label around it.
#:
#: Ordered most specific first: a PEM block should be masked as a block, not
#: shredded into base64 fragments by a laxer pattern that runs earlier.
CREDENTIAL_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "private-key",
        r"-----BEGIN (?:RSA |EC |OPENSSH |PGP |DSA )?PRIVATE KEY-----"
        r"[\s\S]{0,8000}?-----END (?:RSA |EC |OPENSSH |PGP |DSA )?PRIVATE KEY-----",
    ),
    # A JWT is three base64url segments. Masked because a caller's own bearer
    # token can reach a prompt through an error body or a log line.
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ("aws-access-key", r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"),
    ("gcp-service-account", r'"private_key_id"\s*:\s*"[0-9a-f]{32,}"'),
    ("github-token", r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    ("openai-key", r"\bsk-(?:proj-|ant-|or-v1-)?[A-Za-z0-9_-]{20,}\b"),
    ("slack-token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    # A URL carrying inline credentials: postgres://user:pw@host, https://x:y@z.
    ("url-credentials", r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@[^\s]+"),
    ("bearer-header", r"(?i)\b(?:authorization|proxy-authorization)\s*[:=]\s*\S+"),
    # A labelled secret, as it appears in a manifest, a .env or a log line.
    (
        "labelled-secret",
        r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|"
        r"client[_-]?secret|private[_-]?key)\b\s*[:=]\s*[\"']?[^\s\"',}{]{6,}",
    ),
)


@dataclass(slots=True)
class ScanResult:
    text: str
    findings: list[str] = field(default_factory=list)

    @property
    def masked(self) -> bool:
        return bool(self.findings)


class CredentialScanner:
    """Masks credential-shaped strings. Compiled once, applied per message."""

    def __init__(self, patterns: tuple[tuple[str, str], ...] = CREDENTIAL_PATTERNS) -> None:
        self._compiled: list[tuple[str, re.Pattern[str]]] = []
        for name, pattern in patterns:
            try:
                self._compiled.append((name, re.compile(pattern)))
            except re.error as exc:  # pragma: no cover - a bad pattern is a bug
                log.error("credential pattern %s does not compile: %s", name, exc)

    def scan(self, text: str) -> ScanResult:
        if not text:
            return ScanResult(text=text)
        findings: list[str] = []
        result = text
        for name, pattern in self._compiled:
            try:
                result, count = pattern.subn(f"{MASK}:{name}", result)
            except Exception:  # noqa: BLE001 - a scan must never break a run
                continue
            if count:
                findings.extend([name] * count)
        return ScanResult(text=result, findings=findings)

    def scrub_messages(self, messages: list) -> tuple[list, list[str]]:
        """Mask credentials across a whole conversation before it is sent.

        Returns the messages (new objects where anything changed) and what was
        found, so the caller can audit the event. The audit records the KIND of
        credential and never the value — an audit trail that leaks the secret it
        is reporting is worse than no audit trail.
        """
        findings: list[str] = []
        scrubbed = []
        for message in messages:
            content = getattr(message, "content", None)
            if not isinstance(content, str) or not content:
                scrubbed.append(message)
                continue
            result = self.scan(content)
            if not result.masked:
                scrubbed.append(message)
                continue
            findings.extend(result.findings)
            try:
                scrubbed.append(message.model_copy(update={"content": result.text}))
            except AttributeError:  # pragma: no cover - not a pydantic model
                scrubbed.append(message)
        return scrubbed, findings


@dataclass(slots=True)
class ModelPolicy:
    """Which models a caller may name.

    Empty means no restriction, which is the shipped default. An allow-list that
    rejects the platform's own default model the day it is upgraded is worse
    than not having one, so this is opt-in and the refusal says exactly what is
    permitted.
    """

    allowed: tuple[str, ...] = ()

    def permits(self, model: str) -> bool:
        if not self.allowed or not model:
            return True
        return any(_matches(model, rule) for rule in self.allowed)

    def refusal(self, model: str) -> str:
        return (
            f"model {model!r} is not in this platform's allow-list "
            f"({', '.join(self.allowed)}); set ADHAR_AI_ALLOWED_MODELS to change it"
        )


def _matches(model: str, rule: str) -> bool:
    """Exact match, or a `claude-*` style prefix wildcard.

    Wildcards matter: pinning exact model ids means the allow-list has to be
    edited every time a provider ships a point release, which in practice means
    it stops being maintained.
    """
    rule = rule.strip()
    if rule.endswith("*"):
        return model.startswith(rule[:-1])
    return model == rule
