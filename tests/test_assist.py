"""The persona × stage map, asserted rather than claimed.

`assist.py` says the platform helps every kind of user at every stage of their
work. These are the tests that make that statement falsifiable: every offer
names an agent that exists, tools that exist, tools that the named agent can
actually call, and a question that really routes to it. Delete a tool, rename an
agent or narrow a roster and the offer that became a lie fails here.
"""

from __future__ import annotations

import asyncio

import pytest

from adhar_ai.config import DOMAINS, MCPConfig
from adhar_ai.mcp.server import build_server
from adhar_ai.runtime.agents import AgentRegistry
from adhar_ai.runtime.assist import (
    ENTRYPOINTS,
    OFFERS,
    PERSONA_NAMES,
    PERSONAS,
    STAGE_NAMES,
    gaps,
    offers,
    persona_for,
    report,
)


@pytest.fixture(scope="module")
def live_tools() -> set[str]:
    """Every tool name the eight MCP servers actually register."""
    cfg = MCPConfig()
    names: set[str] = set()
    for domain in DOMAINS:
        server = build_server(
            MCPConfig(domain=domain, gitea=cfg.gitea, argocd=cfg.argocd, telemetry=cfg.telemetry)
        )
        names |= {t.name for t in asyncio.run(server.list_tools())}
    return names


@pytest.fixture(scope="module")
def roster() -> AgentRegistry:
    return AgentRegistry()


# --------------------------------------------------------------------------- #
# Integrity: an offer must be true
# --------------------------------------------------------------------------- #


def test_every_offer_names_an_agent_that_exists(roster):
    for offer in OFFERS:
        assert offer.agent in roster.names, f"{offer.ask!r} names agent {offer.agent!r}"


def test_every_offer_names_tools_that_exist(live_tools):
    for offer in OFFERS:
        unknown = set(offer.tools) - live_tools
        assert not unknown, f"{offer.ask!r} names tools that do not exist: {sorted(unknown)}"


def test_every_offer_only_names_tools_its_agent_can_call(roster):
    """The subtle lie: a real tool, a real agent, and the agent is not allowed
    to call it. The offer would then route somewhere that cannot answer."""
    for offer in OFFERS:
        agent = roster.get(offer.agent)
        if not agent.tools:
            continue  # the generalist: every tool its ceiling allows
        beyond = set(offer.tools) - set(agent.tools)
        assert not beyond, f"{offer.agent!r} cannot call {sorted(beyond)} for {offer.ask!r}"


def test_every_offer_question_reaches_its_agent(roster):
    """The example questions are the product surface — a console renders them as
    "try asking". A question that routes elsewhere teaches the user the wrong
    thing, so routing is asserted, and a near miss is allowed only when the
    agent it lands on can hand off to the one the offer names."""
    for offer in OFFERS:
        routed, _confidence = roster.route(offer.ask)
        if routed == offer.agent:
            continue
        assert roster.get(routed).may_hand_to(offer.agent), (
            f"{offer.ask!r} routes to {routed!r}, which cannot hand to {offer.agent!r}"
        )


def test_personas_and_stages_are_declared_consistently():
    for offer in OFFERS:
        assert offer.persona in PERSONA_NAMES, offer.persona
        assert offer.stage in STAGE_NAMES, offer.stage
        assert offer.tools or offer.agent == "guide", (
            f"{offer.ask!r} names no tools; only knowledge-only guide offers may"
        )
        unknown = set(offer.entrypoints) - set(ENTRYPOINTS)
        assert not unknown, f"{offer.ask!r} names unknown entrypoints {sorted(unknown)}"
        assert offer.why, f"{offer.ask!r} does not say why it is worth offering"


# --------------------------------------------------------------------------- #
# Coverage: the actual claim
# --------------------------------------------------------------------------- #


def test_every_persona_is_helped_at_every_stage_it_claims():
    """This is the "assistance for all users at every stage" assertion. A new
    persona or a new stage with nothing behind it fails here, by name."""
    assert gaps() == [], f"uncovered persona/stage pairs: {gaps()}"


def test_every_stage_is_offered_to_someone():
    offered = {o.stage for o in OFFERS}
    assert offered == set(STAGE_NAMES), f"stages with no offer at all: {set(STAGE_NAMES) - offered}"


def test_every_persona_has_at_least_one_offer():
    for persona in PERSONAS:
        assert offers(persona.name), f"{persona.name} has no offers"


def test_the_proactive_and_event_surfaces_are_both_used():
    """An assistant that only answers when asked is not integrated into the
    work. Some offers must arrive on their own."""
    assert [o for o in OFFERS if "chore" in o.entrypoints], "nothing is offered proactively"
    assert [o for o in OFFERS if "operator" in o.entrypoints], "nothing is offered on an event"
    assert [o for o in OFFERS if "slack" in o.entrypoints], "nothing is offered in chat"


# --------------------------------------------------------------------------- #
# Picking a persona
# --------------------------------------------------------------------------- #


def test_persona_comes_from_the_callers_groups():
    assert persona_for(("platform-developer",)) == "developer"
    assert persona_for(("platform-admin",)) == "sre"  # first admin persona in roster order
    assert persona_for(()) == "newcomer"
    # Keycloak renders group paths with a leading slash.
    assert persona_for(("/platform-developer",)) == "developer"


def test_what_someone_is_doing_beats_what_they_may_do():
    """An admin asking a cost question is being a finops person for the next
    five minutes, and those are the offers worth showing them."""
    assert persona_for(("platform-admin",), agent="cost") == "finops"
    assert persona_for(("platform-developer",), agent="data") == "data-engineer"
    # An agent that identifies no persona leaves the group-derived one alone.
    assert persona_for(("platform-developer",), agent="guide") == "developer"


def test_filters_narrow_and_never_invent():
    assert all(o.persona == "developer" for o in offers("developer"))
    assert all(o.stage == "build" for o in offers(stage="build"))
    assert offers("developer", "build")
    assert offers("finops", "build") == [], "a filter must not invent an offer"
    assert all("chore" in o.entrypoints for o in offers(entrypoint="chore"))


def test_the_report_leads_with_the_callers_own_persona():
    whole = report()
    assert whole["persona"] is None
    assert whole["count"] == len(OFFERS)

    mine = report(groups=("platform-developer",))
    assert mine["persona"] == "developer"
    assert mine["count"] == len(offers("developer"))
    assert {o["persona"] for o in mine["offers"]} == {"developer"}
    # The map itself is always included: a developer should be able to see that
    # other stages exist and who they are for.
    assert len(mine["stages"]) == len(STAGE_NAMES)
    assert len(mine["personas"]) == len(PERSONAS)
