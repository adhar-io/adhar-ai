"""What the knowledge base stores, and how a source becomes storable.

A `Document` is one thing worth knowing: an ADR section, a package contract, a
live ArgoCD Application, a resolved incident, a meeting note. Sources produce
documents; the store chunks, embeds and indexes them.

Two fields carry most of the design:

* **`doc_id` is stable across re-ingestion.** It is how the store recognises
  that `adr/0024.md#Decision` is the same document it saw yesterday, so a
  refresh updates it in place, deletes what has genuinely gone, and — because
  the content hash is unchanged for the rest — re-embeds almost nothing. A
  knowledge base that re-embedded its whole corpus nightly would cost real money
  to keep current, which is the same as not keeping it current.
* **`kind` is what the document IS**, and it is used at retrieval time. A
  runbook and a two-year-old meeting note are not equally authoritative about
  how to fix something today.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

#: Chunk target. Large enough to keep a procedure or a decision intact, small
#: enough that a handful fit in a prompt alongside the live tool output the
#: answer is actually built from.
MAX_CHARS = 2000
#: Overlap between consecutive chunks of one document, so a sentence split
#: across a boundary is still retrievable from both sides.
OVERLAP_CHARS = 200

HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$", re.MULTILINE)

#: Every kind the knowledge base understands, with how much a hit of that kind
#: is trusted relative to the others at retrieval time. These are deliberately
#: mild: they break ties between comparable matches rather than override
#: relevance.
KIND_WEIGHTS: dict[str, float] = {
    "runbook": 1.25,  # written to be followed, under pressure
    "incident": 1.20,  # what actually happened, and what fixed it
    "finding": 1.15,  # the platform's own observation about itself
    "adr": 1.10,  # why the platform is the way it is
    "tool": 1.10,  # what the agent can actually do
    "package": 1.05,  # what is installed and how it is configured
    "resource": 1.05,  # live state
    "doc": 1.00,
    "qa": 0.95,  # a previous answer: useful precedent, not a source of truth
    "note": 0.95,  # meeting notes and asides
}
DEFAULT_KIND = "doc"


@dataclass(slots=True)
class Document:
    """One unit of knowledge, before chunking."""

    doc_id: str
    source: str
    text: str
    kind: str = DEFAULT_KIND
    origin: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)

    def chunks(self) -> list[Chunk]:
        return chunk_document(self)


@dataclass(slots=True)
class Chunk:
    """One embedded row."""

    doc_id: str
    chunk_index: int
    source: str
    kind: str
    origin: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        """Identity of this chunk's CONTENT, used to skip re-embedding.

        Covers the text and the citation, because a chunk whose source label
        changed is a different citation even when the prose is identical.
        """
        material = f"{self.source}\x00{self.kind}\x00{self.text}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def as_grounding(self, retrieval: str = "vector") -> str:
        return f"### {self.source} ({self.kind}, {retrieval})\n\n{self.text}"


def split_on_headings(text: str) -> list[tuple[str, str]]:
    """Split Markdown into `(heading, body)` sections, preserving order."""
    matches = list(HEADING_RE.finditer(text))
    if not matches:
        return [("", text)]
    sections: list[tuple[str, str]] = []
    preamble = text[: matches[0].start()].strip()
    if preamble:
        sections.append(("", preamble))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end].strip()
        if body:
            sections.append((match.group(2).strip(), body))
    return sections


def _wrap(text: str, limit: int = MAX_CHARS, overlap: int = OVERLAP_CHARS) -> list[str]:
    """Hard-wrap an over-long section, breaking on a paragraph where possible."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            # Prefer a paragraph break, then a sentence end, then wherever.
            window = text[start:end]
            for marker in ("\n\n", ". ", "\n"):
                cut = window.rfind(marker)
                if cut > limit // 2:
                    end = start + cut + len(marker)
                    break
        parts.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [p for p in parts if p]


def chunk_document(doc: Document) -> list[Chunk]:
    """Chunk on Markdown headings, carrying the heading into the citation.

    The heading rides in the `source` because it is what makes a citation
    actionable: "ADR-0024 §Decision" sends a reader to a paragraph, while
    "ADR-0024" sends them to a document.
    """
    chunks: list[Chunk] = []
    for heading, body in split_on_headings(doc.text):
        source = f"{doc.source}#{heading}" if heading else doc.source
        for piece in _wrap(body):
            chunks.append(
                Chunk(
                    doc_id=doc.doc_id,
                    chunk_index=len(chunks),
                    source=source,
                    kind=doc.kind,
                    origin=doc.origin,
                    text=piece,
                    metadata=dict(doc.metadata),
                )
            )
    return chunks


def kind_weight(kind: str) -> float:
    return KIND_WEIGHTS.get(kind, 1.0)
