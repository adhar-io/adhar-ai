"""A graded question set: does retrieval hand the model the right source?

The seven questions this started with were the ones that exposed the gap — each
retrieved the wrong domain because the right material was not indexed. They are
now rows here, with what a correct retrieval must surface, and the set grows
whenever a bad answer teaches us something.

The grade is **about retrieval, not about the model**. A question passes when
one of its expected sources is among the top results. That is deterministic,
needs no key, and fails for exactly the reasons that make answers bad: a source
nobody indexed, a chunker that split a fact from its heading, a reranker that
demoted the right page. It is run in CI over a checkout of the platform and by
`hack/verify-knowledge.py` against a real database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class Question:
    """One graded question."""

    ask: str
    #: Substrings, any ONE of which appearing in a hit's `source` is a pass.
    expect: tuple[str, ...]
    #: The kind the best answer comes from, for the report.
    kind: str = ""
    #: Why this row exists — the failure it caught.
    why: str = ""


#: The set. Keep each row's `why` honest: it is the regression this row guards.
QUESTIONS: tuple[Question, ...] = (
    Question(
        ask="which packages are enabled in the local environment?",
        expect=("environment local",),
        kind="environment",
        why="retrieved a pull-request guide; the environment file was never indexed",
    ),
    Question(
        ask="what packages are enabled in production that are not enabled locally?",
        expect=("environment production", "environment local"),
        kind="environment",
    ),
    Question(
        ask="what database does keycloak use and where is its secret?",
        expect=("security/keycloak: Cluster keycloak-db", "security/keycloak: what it deploys"),
        kind="manifest",
        why=(
            "retrieved three unrelated package contracts; the manifest was a Go "
            "template that did not parse"
        ),
    ),
    Question(
        ask="how is adhar-ai deployed and what does it depend on?",
        expect=("package ai/adhar-ai: what it deploys", "ai/adhar-ai: Deployment adhar-ai-runtime"),
        kind="manifest",
        why="retrieved cert-manager and llm-d; no single document described the package",
    ),
    Question(
        ask="what is the URL of the console?",
        expect=("core/adhar-console: HTTPRoute console", "core/adhar-console: what it deploys"),
        kind="manifest",
        why="hostnames live only in HTTPRoutes, which were not indexed",
    ),
    Question(
        ask="which secret holds the LLM API key for adhar-ai and where does it come from?",
        expect=("ai/adhar-ai: ExternalSecret adhar-ai-llm",),
        kind="manifest",
    ),
    Question(
        ask="what is the adhar CLI command to see platform health?",
        expect=("CLI reference: adhar health", "CLI reference: adhar get"),
        kind="cli",
        why="retrieved a day-2 operations design doc; the CLI was not indexed",
    ),
    Question(
        ask="how do I create a new environment with the adhar CLI?",
        expect=("CLI reference: adhar", "CUSTOMIZATION.md"),
        kind="cli",
    ),
    Question(
        ask="which image does the adhar console run?",
        expect=("core/adhar-console: Deployment", "core/adhar-console: what it deploys"),
        kind="manifest",
    ),
    Question(
        ask="what sync wave does the adhar-ai runtime deploy in?",
        expect=("ai/adhar-ai: Deployment adhar-ai-runtime",),
        kind="manifest",
    ),
    Question(
        ask="why are writes pull requests instead of applying to the cluster?",
        expect=("0024", 'USER_GUIDE.md#What "write" means'),
        kind="adr",
        why="a rationale question must still reach the ADR, not a manifest",
    ),
    Question(
        ask="how do I add a new package to the platform?",
        expect=("CUSTOMIZATION.md", "MARKETPLACE", "0004"),
        kind="doc",
    ),
)


@dataclass(slots=True)
class Result:
    question: Question
    passed: bool
    sources: list[str] = field(default_factory=list)
    matched: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question.ask,
            "passed": self.passed,
            "matched": self.matched,
            "top": self.sources[:5],
            "kind": self.question.kind,
        }


async def grade(
    knowledge: Any, k: int = 6, questions: tuple[Question, ...] = QUESTIONS
) -> list[Result]:
    """Ask every question of a knowledge base and say which passed."""
    results: list[Result] = []
    for question in questions:
        hits = await knowledge.search(question.ask, k=k)
        sources = [h.source for h in hits]
        matched = next(
            (e for e in question.expect if any(e in s for s in sources)),
            "",
        )
        results.append(
            Result(question=question, passed=bool(matched), sources=sources, matched=matched)
        )
    return results


def summary(results: list[Result]) -> dict[str, Any]:
    passed = sum(1 for r in results if r.passed)
    return {
        "passed": passed,
        "total": len(results),
        "failed": [r.as_dict() for r in results if not r.passed],
    }
