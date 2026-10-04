"""Lexical retrieval — the grounding path that needs neither a key nor a database.

The README has always promised that "with no key configured the retriever
degrades to lexical search instead of pretending to be unavailable". It did not:
every failure path returned `[]`, so an unkeyed platform answered from the
model's prior with no Adhar grounding at all and no sign that anything was
missing. This module is that promise, implemented.

It is BM25 over the same chunks the pgvector indexer produces, held in process
and built from the docs tree on disk. That makes it useful in exactly the
situations vector search is not available:

* no LLM provider key, so there is no embedding endpoint to call;
* no CNPG database, which is every `docker compose` and bare `adhar-ai runtime`;
* pgvector reachable but empty, mid-first-index;
* a database that has started failing, where silently ungrounded answers are
  the worst outcome.

BM25 rather than substring matching because the queries are natural-language
questions ("why is the console app degraded") against prose, where term
saturation and length normalization are most of what makes ranking work. There
is no stemmer: a dependency-free tokenizer that splits on non-word characters
and lowercases gets most of the benefit on technical text, where the terms that
matter are identifiers.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .documents import Chunk, Document

log = logging.getLogger("adhar_ai.rag")

TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-_./][a-z0-9]+)*")

#: Terms carried by nearly every platform document, which therefore separate
#: nothing. IDF handles this on a large corpus; on a few hundred chunks the
#: stop-list is what keeps "the adhar platform" from matching everything.
STOPWORDS = frozenset(
    """a an the and or but if then than that this these those is are was were be been being
    do does did done have has had of in on at to for from by with without into over under
    it its as not no yes can could should would may might will shall about what which who
    whom when where why how i you he she they we me him her them us our your their""".split()
)

K1 = 1.5  # term-frequency saturation
B = 0.75  # length normalization


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, keeping `kube-system` and `app.kubernetes.io` whole —
    AND their parts.

    The whole token is what makes an exact identifier retrievable. The parts
    are what make a question retrievable: people write "adhar console" and
    "API key" where the manifests say `adhar-console` and `API_KEY`, and a
    tokenizer that keeps only the compound never lets the two meet. The
    compound still scores higher, because it is rarer.
    """
    out: list[str] = []
    for token in TOKEN_RE.findall(text.lower()):
        if token in STOPWORDS or len(token) <= 1:
            continue
        out.append(token)
        parts = re.split(r"[-_.]", token) if ("-" in token or "_" in token or "." in token) else []
        for part in parts:
            if part and part not in STOPWORDS and len(part) > 1 and part != token:
                out.append(part)
        # A conservative stem, emitted ALONGSIDE the word: "deployed", "deploys"
        # and "deploy" should meet, as they already do on the Postgres path,
        # whose full-text index stems. Alongside rather than instead, so an
        # exact word still outscores an inflection of it.
        for word in [token, *parts]:
            stem = _stem(word)
            if stem != word:
                out.append(stem)
    return out


def _stem(word: str) -> str:
    """Strip the commonest English inflections. Deliberately timid: a stemmer
    that turns `kubernetes` into `kubernet` makes every identifier fuzzy."""
    if not word.isalpha() or len(word) < 5:
        return word
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4 and not word.endswith("ss"):
            return word[: -len(suffix)]
    return word


@dataclass(slots=True)
class _Doc:
    chunk: Chunk
    terms: Counter[str]
    length: int


@dataclass
class LexicalIndex:
    """An in-process BM25 index over doc chunks."""

    docs: list[_Doc] = field(default_factory=list)
    df: Counter[str] = field(default_factory=Counter)
    avg_length: float = 0.0

    @property
    def size(self) -> int:
        return len(self.docs)

    @classmethod
    def from_chunks(cls, chunks: list[Chunk]) -> LexicalIndex:
        index = cls()
        for chunk in chunks:
            terms = Counter(tokenize(f"{chunk.source}\n{chunk.text}"))
            if not terms:
                continue
            index.docs.append(_Doc(chunk=chunk, terms=terms, length=sum(terms.values())))
            index.df.update(terms.keys())
        if index.docs:
            index.avg_length = sum(d.length for d in index.docs) / len(index.docs)
        return index

    @classmethod
    def from_path(cls, docs_path: str | Path) -> LexicalIndex:
        """Build from the docs tree, synchronously.

        `DocsSource` is async because most sources do I/O over the network; this
        one only reads files, so the walk is inlined here rather than forcing
        every caller of a purely-CPU index build into an event loop.
        """
        from .sources import classify_doc

        root = Path(docs_path)
        if not root.exists():
            log.info("docs path %s does not exist; lexical index is empty", root)
            return cls()
        files = sorted(root.rglob("*.md")) if root.is_dir() else [root]
        chunks: list[Chunk] = []
        for file in files:
            try:
                text = file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if not text.strip():
                continue
            rel = str(file.relative_to(root)) if root.is_dir() else file.name
            chunks.extend(
                Document(
                    doc_id=f"docs:{rel}",
                    source=rel,
                    text=text,
                    kind=classify_doc(file),
                    origin="docs",
                ).chunks()
            )
        index = cls.from_chunks(chunks)
        log.info("lexical index built: %d chunks from %s", index.size, docs_path)
        return index

    def chunks(self) -> list[Chunk]:
        """The chunks currently indexed, so callers can add to them."""
        return [d.chunk for d in self.docs]

    def _idf(self, term: str) -> float:
        # Robertson/Sparck-Jones IDF, floored at zero so a term present in every
        # document contributes nothing rather than a negative score.
        n, df = len(self.docs), self.df.get(term, 0)
        if df == 0:
            return 0.0
        return max(0.0, math.log(1.0 + (n - df + 0.5) / (df + 0.5)))

    def search(
        self, query: str, k: int = 5, kinds: tuple[str, ...] = ()
    ) -> list[tuple[Chunk, float]]:
        """Top-k chunks by BM25, best first. Chunks scoring zero are dropped.

        `kinds` narrows to those kinds, exactly as the pgvector path does. The
        fallback used to ignore it, so an agent scoped to runbooks and
        incidents read manifests the moment the database was unavailable —
        the one time the scope was most likely to matter.
        """
        terms = tokenize(query)
        if not terms or not self.docs:
            return []
        weights = {t: self._idf(t) for t in set(terms)}
        scored: list[tuple[Chunk, float]] = []
        for doc in self.docs:
            if kinds and doc.chunk.kind not in kinds:
                continue
            score = 0.0
            for term in set(terms):
                tf = doc.terms.get(term, 0)
                if not tf:
                    continue
                norm = 1 - B + B * (doc.length / self.avg_length if self.avg_length else 1)
                score += weights[term] * (tf * (K1 + 1)) / (tf + K1 * norm)
            if score > 0:
                scored.append((doc.chunk, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:k]
