"""Two cheap, large improvements to what the model is handed.

**Reranking.** Hybrid retrieval returns a good top twenty and a mediocre top
five: the fusion knows which chunks matched, not which ones answer. A small
completion that reads the question and twenty candidate titles-and-openings and
orders them is the single cheapest retrieval-quality gain available, and it is
how every serious RAG system closes that gap.

**Query rewriting.** People ask "why is checkout down". The documents say
"OutOfSync", "Degraded", "repo-server", "CrashLoopBackOff". One completion that
restates the question in the platform's own vocabulary, retrieved alongside the
original and fused, finds what the original alone does not.

Both cost one extra, small completion per question. Both **degrade to a no-op
without a gateway**, so a keyless platform retrieves exactly as before. Both
fail open: a rerank that errors returns the original order, a rewrite that
errors returns the original query. Retrieval must never be the reason an
answer did not happen.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import OrderedDict
from typing import Any, Protocol

from ..gateway.types import Message
from .store import Hit

log = logging.getLogger("adhar_ai.rag.retrieval")

#: Rank-fusion constant, the same one the store uses, so a rewritten query's
#: results fuse with the original's on equal terms.
RRF_K = 60

#: How much of each candidate the reranker reads. Enough to judge relevance,
#: little enough that twenty candidates fit comfortably in one prompt.
CANDIDATE_CHARS = 420

#: Rewrites are cached by exact question. People retry the same question
#: verbatim far more often than they rephrase it, and the rewrite of a question
#: does not change between asks.
REWRITE_CACHE = 512
REWRITE_TTL = 3600.0

RERANK_PROMPT = """You rank search results for an internal developer platform called Adhar.

Question:
{question}

Candidates (one per line, `id | source | opening`):
{candidates}

Return ONLY a JSON array of candidate ids, most relevant first, including every
candidate that could help answer the question and omitting the rest. No prose."""

REWRITE_PROMPT = """Restate the user's question in the vocabulary an internal developer platform's
documentation and Kubernetes manifests would use, so it retrieves well.

Platform vocabulary includes: package, manifest, environment, namespace, ArgoCD
Application, OutOfSync, Degraded, Synced, Healthy, sync wave, HTTPRoute,
hostname, Gateway, Deployment, StatefulSet, CronJob, Secret, ExternalSecret,
ConfigMap, CNPG Cluster, PostgreSQL, Keycloak realm, client, Gitea, pull request,
Kyverno ClusterPolicy, Crossplane composite, OpenCost, Prometheus, Loki, Tempo,
certificate, cert-manager, CrashLoopBackOff, OOMKilled, ImagePullBackOff.

Question: {question}

Return ONLY a JSON object: {{"query": "<one rewritten search query, under 25 words>",
"terms": ["<up to 6 exact identifiers or platform terms worth matching literally>"]}}"""


class Reranker(Protocol):
    async def rerank(self, question: str, hits: list[Hit], k: int) -> list[Hit]: ...


class QueryRewriter(Protocol):
    async def rewrite(self, question: str) -> str | None: ...


def _completion_text(response: dict[str, Any]) -> str:
    try:
        return str(response["choices"][0]["message"].get("content") or "")
    except (KeyError, IndexError, TypeError):
        return ""


def _first_json(text: str) -> Any:
    """The first JSON value in a reply, tolerating a code fence or a preamble.

    The EARLIEST opener wins. Trying `[` before `{` returned the `terms` array
    from inside a `{"query": …, "terms": […]}` object and discarded the query.
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    starts = [(text.find(o), o, c) for o, c in (("[", "]"), ("{", "}")) if text.find(o) != -1]
    for start, _opener, closer in sorted(starts):
        end = text.rfind(closer)
        if end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


def fuse(primary: list[Hit], secondary: list[Hit], k: int) -> list[Hit]:
    """Reciprocal rank fusion of two hit lists by chunk identity.

    A chunk found by both queries ranks above one found by either, which is
    the property that makes a rewrite safe: it can only promote what the
    original also found, or add what the original missed — never bury a hit
    the original ranked first.
    """
    scores: dict[str, float] = {}
    first: dict[str, Hit] = {}
    for hits in (primary, secondary):
        for rank, hit in enumerate(hits):
            key = chunk_key(hit)
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
            if key in first and hit.retrieval not in first[key].retrieval:
                first[key].retrieval = f"{first[key].retrieval}+{hit.retrieval}"
            first.setdefault(key, hit)
    ordered = sorted(scores, key=lambda key: scores[key], reverse=True)
    return [first[key] for key in ordered[:k]]


def chunk_key(hit: Hit) -> str:
    """One chunk's identity across retrievers.

    A pgvector hit carries its row id; an in-process BM25 hit of the same chunk
    carries `-1`. The document, the citation and the opening text are what both
    agree on, so that is the identity — or the same chunk arrives twice and
    takes two of the model's slots.
    """
    return f"{hit.doc_id}#{hit.source}#{' '.join(hit.text.split())[:120]}"


class GatewayReranker:
    """Orders candidates with the gateway model; falls back to their order."""

    def __init__(self, gateway: Any, model: str = "", tenant: str = "adhar-ai:retrieval") -> None:
        self.gateway = gateway
        self.model = model
        self.tenant = tenant
        self.calls = 0
        self.failures = 0

    async def rerank(self, question: str, hits: list[Hit], k: int) -> list[Hit]:
        if len(hits) <= 1 or k <= 0:
            return hits[:k]
        lines = []
        for index, hit in enumerate(hits):
            opening = " ".join(hit.text.split())[:CANDIDATE_CHARS]
            lines.append(f"{index} | {hit.source} | {opening}")
        prompt = RERANK_PROMPT.format(question=question, candidates="\n".join(lines))
        self.calls += 1
        try:
            response = await self.gateway.chat(
                [Message(role="user", content=prompt)],
                None,
                self.tenant,
                model=self.model or None,
                max_tokens=256,
            )
            order = _first_json(_completion_text(response))
            if not isinstance(order, list):
                raise ValueError("reranker did not return a list")
            chosen: list[int] = []
            for item in order:
                try:
                    idx = int(item)
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(hits) and idx not in chosen:
                    chosen.append(idx)
            if not chosen:
                raise ValueError("reranker chose nothing")
        except Exception as exc:  # noqa: BLE001 - never let ranking block an answer
            self.failures += 1
            log.debug("rerank fell back to retrieval order: %s", exc)
            return hits[:k]
        # Candidates the model omitted keep their retrieval order after the
        # ones it ranked, so a terse reply does not throw away good hits.
        remainder = [i for i in range(len(hits)) if i not in chosen]
        reranked = [hits[i] for i in chosen + remainder]
        for hit in reranked[: len(chosen)]:
            if "rerank" not in hit.retrieval:
                hit.retrieval = f"{hit.retrieval}+rerank"
        return reranked[:k]


class GatewayQueryRewriter:
    """Restates a question in platform vocabulary; falls back to the original."""

    def __init__(self, gateway: Any, model: str = "", tenant: str = "adhar-ai:retrieval") -> None:
        self.gateway = gateway
        self.model = model
        self.tenant = tenant
        self.calls = 0
        self.failures = 0
        self._cache: OrderedDict[str, tuple[float, str | None]] = OrderedDict()

    async def rewrite(self, question: str) -> str | None:
        key = " ".join(question.lower().split())
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < REWRITE_TTL:
            self._cache.move_to_end(key)
            return cached[1]

        self.calls += 1
        result: str | None = None
        try:
            response = await self.gateway.chat(
                [Message(role="user", content=REWRITE_PROMPT.format(question=question))],
                None,
                self.tenant,
                model=self.model or None,
                max_tokens=160,
            )
            payload = _first_json(_completion_text(response))
            if isinstance(payload, dict):
                query = str(payload.get("query") or "").strip()
                terms = [str(t).strip() for t in payload.get("terms") or [] if str(t).strip()]
                combined = " ".join([query, *terms]).strip()
                # A rewrite identical to the question buys nothing and costs a
                # second retrieval; say so by returning None.
                if combined and " ".join(combined.lower().split()) != key:
                    result = combined[:400]
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            log.debug("query rewrite fell back to the original: %s", exc)

        self._cache[key] = (time.monotonic(), result)
        self._cache.move_to_end(key)
        while len(self._cache) > REWRITE_CACHE:
            self._cache.popitem(last=False)
        return result
