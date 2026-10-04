"""The retrieval quality gate: every graded question must surface its source.

Runs over a checkout of the platform repository with the in-process BM25
index — no database, no key, deterministic — so a drop in retrieval quality
fails a build rather than surfacing as a complaint. The same questions run
against a real pgvector in `hack/verify-knowledge.py`.

Skipped when there is no checkout to read: the questions are about the real
platform, and a fixture corpus would test the fixture.

    ADHAR_REPO_PATH=../adhar uv run pytest tests/evals/test_retrieval_gate.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from adhar_ai.rag import KnowledgeBase
from adhar_ai.rag.benchmark import QUESTIONS, grade, summary

REPO = Path(os.environ.get("ADHAR_REPO_PATH", "../adhar")).resolve()
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not (REPO / "platform/stack/packages").is_dir(),
        reason=f"no platform checkout at {REPO}; set ADHAR_REPO_PATH",
    ),
]


@pytest.fixture(scope="module")
def knowledge() -> KnowledgeBase:
    """Built once per module: indexing 4,000 chunks per question would make
    the gate too slow to keep. Synchronous because the suite's event loop is
    function-scoped and the index, having no connections, has no loop."""
    import asyncio

    async def build() -> KnowledgeBase:
        kb = KnowledgeBase.build(
            dsn="",
            docs_path=str(REPO / "docs"),
            packages_path=str(REPO / "platform/stack/packages"),
            environments_path=str(REPO / "platform/stack/environments"),
            cli_path=str(REPO),
        )
        await kb.prepare()
        await kb.refresh()
        return kb

    return asyncio.run(build())


async def test_every_platform_origin_contributed(knowledge: KnowledgeBase) -> None:
    """A source that silently yields nothing is the failure this whole layer
    was built to remove."""
    origins = {chunk.origin for chunk in knowledge.lexical.chunks()}  # type: ignore[union-attr]
    assert {"docs", "packages", "manifests", "environments", "cli", "tools"} <= origins


@pytest.mark.parametrize("question", QUESTIONS, ids=lambda q: q.ask[:48])
async def test_a_graded_question_retrieves_an_expected_source(knowledge, question) -> None:
    results = await grade(knowledge, k=6, questions=(question,))
    result = results[0]
    assert result.passed, (
        f"{question.ask!r} did not surface any of {list(question.expect)}; "
        f"top sources were {result.sources[:4]}"
        + (f" — this row guards: {question.why}" if question.why else "")
    )


async def test_the_whole_set_passes(knowledge: KnowledgeBase) -> None:
    verdict = summary(await grade(knowledge, k=6))
    assert verdict["passed"] == verdict["total"], verdict["failed"]
