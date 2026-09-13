#!/usr/bin/env python
"""End-to-end verification of the knowledge base against a real pgvector.

    docker run -d --name adhar-rag -p 15432:5432 \
      -e POSTGRES_USER=adhar_ai -e POSTGRES_PASSWORD=adhar_ai \
      -e POSTGRES_DB=adhar_ai_rag pgvector/pgvector:pg16

    ADHAR_AI_RAG_DSN=postgresql://adhar_ai:adhar_ai@127.0.0.1:15432/adhar_ai_rag \
      uv run hack/verify-knowledge.py --docs ../adhar/docs \
      --packages ../adhar/platform/stack/packages

WHAT THE EMBEDDER HERE IS, AND IS NOT
-------------------------------------
The production embedder is a neural model reached through the LLM gateway
(`GatewayEmbeddings`) or, offline, sentence-transformers (`LocalEmbeddings`).
Neither is usable in every environment — an unkeyed account cannot call the
first, and torch has no wheel for some platforms — so this script carries its
own **hashed TF-IDF** embedder.

It is a real, content-bearing vector: documents about the same subject land near
each other, so it genuinely exercises pgvector's cosine index, the reciprocal
rank fusion, the kind weighting and the feedback adjustment. It is NOT a
substitute for a neural embedding and will not match a paraphrase that shares no
vocabulary. Retrieval quality measured here is a floor, not a forecast.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import math
import os
import sys
from collections import Counter

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))

from adhar_ai.rag import KnowledgeBase  # noqa: E402
from adhar_ai.rag.lexical import tokenize  # noqa: E402

DIM = 1536

GREEN, RED, DIM_, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"
FAILURES = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILURES
    mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    if not ok:
        FAILURES += 1
    print(f"  {mark}  {label}" + (f"\n        {DIM_}{detail}{RESET}" if detail else ""))


def step(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


class HashedTfIdf:
    """Dependency-free bag-of-words vectors, hashed into DIM buckets."""

    name = "hashed-tfidf (verification only)"

    def __init__(self) -> None:
        self.df: Counter[str] = Counter()
        self.n = 0

    def fit(self, texts: list[str]) -> None:
        for text in texts:
            self.n += 1
            self.df.update(set(tokenize(text)))

    def _bucket(self, term: str) -> int:
        return int.from_bytes(hashlib.md5(term.encode()).digest()[:4], "big") % DIM

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vector = [0.0] * DIM
            counts = Counter(tokenize(text))
            for term, tf in counts.items():
                idf = math.log(1 + (self.n + 1) / (1 + self.df.get(term, 0)))
                vector[self._bucket(term)] += (1 + math.log(tf)) * idf
            norm = math.sqrt(sum(v * v for v in vector)) or 1.0
            out.append([v / norm for v in vector])
        return out


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docs", required=True)
    parser.add_argument("--packages", default="")
    parser.add_argument("--dsn", default=os.environ.get("ADHAR_AI_RAG_DSN", ""))
    args = parser.parse_args()

    if not args.dsn:
        print("set ADHAR_AI_RAG_DSN or pass --dsn", file=sys.stderr)
        return 2

    embedder = HashedTfIdf()
    kb = KnowledgeBase.build(
        dsn=args.dsn, docs_path=args.docs, packages_path=args.packages, embedder=embedder
    )

    step("1. Schema on a real pgvector")
    check("prepare() creates the schema", await kb.prepare(), kb.store.status)
    check("mode reports hybrid retrieval", "hybrid" in kb.mode, kb.mode)

    # Fit the IDF table on the corpus before ingesting it.
    corpus: list[str] = []
    for source in kb.sources:
        for doc in await source.documents():
            corpus.extend(c.text for c in doc.chunks())
    embedder.fit(corpus)

    step("2. Populating from the platform itself")
    first = await kb.refresh()
    by_origin = {r.origin: r for r in first}
    for report in first:
        print(f"        {DIM_}{report.as_dict()}{RESET}")
    check("no origin errored", all(not r.error for r in first))
    check(
        "documentation was indexed",
        by_origin.get("docs", None) is not None and by_origin["docs"].chunks_written > 100,
    )
    check(
        "the agent's own tool inventory was indexed",
        by_origin.get("tools") is not None and by_origin["tools"].chunks_written > 0,
    )
    if args.packages:
        check(
            "the package catalogue was indexed",
            by_origin.get("packages") is not None
            and by_origin["packages"].chunks_written > 50,
        )

    step("3. Incremental refresh does not re-embed unchanged content")
    second = await kb.refresh()
    for report in second:
        if report.chunks_written or report.error:
            print(f"        {DIM_}{report.as_dict()}{RESET}")
    rewritten = sum(r.chunks_written for r in second)
    unchanged = sum(r.chunks_unchanged for r in second)
    check(
        "a second pass rewrites nothing",
        rewritten == 0,
        f"{rewritten} rewritten, {unchanged} unchanged",
    )
    check("embedding calls avoided", sum(r.embeddings_called for r in second) == 0)

    step("4. Hybrid retrieval over real content")
    queries = {
        "pull request": "how does the agent propose a change",
        "gateway": "which component routes LLM traffic by model name",
        "degraded": "an argocd application is degraded, what should I check",
    }
    for label, query in queries.items():
        hits = await kb.search(query, k=3)
        check(f"'{label}' returns grounding", bool(hits))
        for hit in hits:
            print(f"        {DIM_}[{hit.retrieval:14s}] {hit.kind:8s} {hit.source[:62]}{RESET}")

    step("5. Exact identifiers are found lexically, not blurred away")
    hits = await kb.search("CompositeCluster", k=5)
    check("an exact identifier retrieves something", bool(hits))
    check(
        "at least one hit came from the lexical half",
        any("lexical" in h.retrieval for h in hits),
        ", ".join(sorted({h.retrieval for h in hits})),
    )

    step("6. Learning: a note is retrievable immediately")
    note = await kb.add_note(
        title="Gitea bot token rotation breaks the write path",
        body=(
            "Symptom: every propose_change fails with 401 from the Gitea API.\n"
            "Cause: the adhar-ai-bot personal access token expired.\n"
            "Fix: reissue the token and update the adhar-ai-bot secret in Vault."
        ),
        kind="incident",
        author="verification",
        tags=["gitea", "auth"],
    )
    check("the note was stored durably", note["durable"], str(note))
    check("the note was embedded and indexed", note["indexed"], str(note))
    hits = await kb.search("propose_change fails with 401 from Gitea", k=3)
    found = [h for h in hits if "rotation" in h.source.lower()]
    check("the new note is retrievable straight away", bool(found))
    if found:
        print(f"        {DIM_}{found[0].source} ({found[0].kind}){RESET}")

    step("7. Learning: feedback moves ranking")
    target = found[0] if found else (hits[0] if hits else None)
    if target is not None and target.chunk_id >= 0:
        before = target.score
        updated = await kb.record_feedback([target.chunk_id], helpful=True)
        check("feedback was recorded against the chunk", updated == 1, f"{updated} row(s)")
        again = await kb.search("propose_change fails with 401 from Gitea", k=3)
        after = next((h.score for h in again if h.chunk_id == target.chunk_id), None)
        check(
            "an upvoted chunk scores higher than before",
            after is not None and after > before,
            f"{before:.6f} -> {after}",
        )
    else:
        check("feedback was recorded against the chunk", False, "no chunk id to vote on")

    step("8. Deletions propagate")
    from adhar_ai.rag.documents import Document

    temp = Document(
        doc_id="verify:temporary",
        source="verify/temporary.md",
        text="# Temporary\n\nThis document exists only to be removed.",
        kind="doc",
        origin="verify",
    )
    await kb.store.ingest("verify", [temp], embedder)
    hits = await kb.search("This document exists only to be removed", k=5)
    check("the temporary document is retrievable", any(h.doc_id == "verify:temporary" for h in hits))
    removed = await kb.store.ingest("verify", [], embedder)
    check(
        "re-ingesting the origin without it removes it",
        removed.chunks_deleted > 0,
        f"{removed.chunks_deleted} chunk(s) deleted",
    )
    hits = await kb.search("This document exists only to be removed", k=5)
    check("it is no longer retrievable", not any(h.doc_id == "verify:temporary" for h in hits))

    step("9. What the knowledge base now holds")
    stats = await kb.stats()
    print(f"        {DIM_}chunks={stats['chunks']} documents={stats['documents']}{RESET}")
    for origin in stats["origins"]:
        print(
            f"        {DIM_}{origin['origin']:10s} {origin['kind']:9s} "
            f"chunks={origin['chunks']:5d} docs={origin['documents']:4d}{RESET}"
        )
    check("the base holds the whole platform", stats["chunks"] > 1000)
    check("more than one kind of knowledge is present", len({o["kind"] for o in stats["origins"]}) >= 3)

    print()
    if FAILURES:
        print(f"{RED}{FAILURES} check(s) failed.{RESET}\n")
        return 1
    print(f"{GREEN}All knowledge-base checks passed.{RESET}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
