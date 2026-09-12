"""The runtime's HTTP surface, with authentication wired in.

`test_runtime.py` covers the routes' happy paths against a fake toolbox. This
file covers who is allowed to drive them, which is the part that decides whether
reaching the port is the same as being able to open a pull request.
"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from adhar_ai.config import RuntimeEnv
from adhar_ai.runtime.app import create_app
from adhar_ai.runtime.auth import AuthPolicy, Principal
from adhar_ai.runtime.autonomy import RuntimeConfig

from .test_runtime import CONFIGMAP_YAML, FakeGateway, FakeToolbox, _answer, _tool_turn

PR = {"url": "https://gitea.adhar.localtest.me/adhar/packages/pulls/7", "number": 7}


def build(policy: AuthPolicy, turns=None, config_yaml: str = CONFIGMAP_YAML):
    toolbox = FakeToolbox({"propose_change": PR, "app_status": {"health": "Degraded"}})
    gateway = FakeGateway(turns or [_answer("all healthy")] * 10)
    cfg = RuntimeConfig.from_mapping(yaml.safe_load(config_yaml))
    app = create_app(
        cfg=cfg, envcfg=RuntimeEnv(), toolbox=toolbox, gateway=gateway, auth=policy
    )
    return app, toolbox, gateway


@pytest.fixture
def open_runtime():
    """No credential configured — the `docker compose` and local-dev posture."""
    app, toolbox, gateway = build(AuthPolicy())
    with TestClient(app) as client:
        yield client, toolbox, gateway


# --------------------------------------------------------------------------- #
# The posture is visible
# --------------------------------------------------------------------------- #


def test_healthz_reports_the_auth_posture(open_runtime) -> None:
    client, _, _ = open_runtime
    auth = client.get("/healthz").json()["auth"]
    assert auth["oidc"] == "disabled"
    assert auth["webhookToken"] == "unset"
    assert auth["unauthenticated"] == "answered as read-only"


def test_healthz_reports_the_findings_store(open_runtime) -> None:
    client, _, _ = open_runtime
    assert "disabled" in client.get("/healthz").json()["findings_store"]


def test_config_reports_the_auth_posture_without_the_secret(open_runtime) -> None:
    app, _, _ = build(AuthPolicy(webhook_token="s3cret"))
    with TestClient(app) as client:
        body = client.get("/config").json()
    assert body["auth"]["webhookToken"] == "configured"
    assert "s3cret" not in str(body)


# --------------------------------------------------------------------------- #
# /chat
# --------------------------------------------------------------------------- #


def test_an_anonymous_chat_is_answered_but_pinned_to_read_only(open_runtime) -> None:
    client, _, gateway = open_runtime
    body = client.post("/chat", json={"prompt": "is the platform healthy?"}).json()
    assert body["kind"] == "answer"
    assert body["autonomy"] == "read-only"
    assert body["principal"]["authenticated"] is False
    # The write tool was never offered to the model.
    offered = {t.function.name for t in gateway.requests[0]["tools"]}
    assert "propose_change" not in offered
    assert "app_status" in offered


def test_an_anonymous_caller_cannot_raise_its_own_autonomy(open_runtime) -> None:
    """The request body used to be the last word on the stage."""
    client, _, gateway = open_runtime
    body = client.post(
        "/chat", json={"prompt": "fix it", "autonomy": "scoped"}
    ).json()
    assert body["autonomy"] == "read-only"
    assert "propose_change" not in {t.function.name for t in gateway.requests[0]["tools"]}


def test_a_caller_may_ask_for_a_lower_stage(open_runtime) -> None:
    client, _, _ = open_runtime
    body = client.post(
        "/chat", json={"prompt": "just look", "autonomy": "read-only"}
    ).json()
    assert body["autonomy"] == "read-only"


def test_require_auth_refuses_an_anonymous_chat() -> None:
    app, _, _ = build(AuthPolicy(require_auth=True))
    with TestClient(app) as client:
        response = client.post("/chat", json={"prompt": "hello"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


# --------------------------------------------------------------------------- #
# /operators/{name}/event
# --------------------------------------------------------------------------- #


ALERT = {
    "alerts": [
        {
            "labels": {"alertname": "KubePodCrashLooping", "namespace": "demo"},
            "annotations": {"summary": "pod is restarting"},
            "status": "firing",
        }
    ]
}


def test_an_unauthenticated_alert_is_triaged_but_opens_no_pull_request() -> None:
    """The hole: this endpoint took no credential, so anyone who could reach the
    pod could drive an LLM run that opens a real PR at the shipped `suggest`
    stage. Triage still happens — it is useful and read-only — but the write
    tool is not offered and the finding records the stage it actually ran at."""
    app, toolbox, gateway = build(
        AuthPolicy(webhook_token="s3cret"),
        turns=[_tool_turn("app_status", {"app": "demo"}), _answer("image pull failure")],
    )
    with TestClient(app) as client:
        body = client.post("/operators/alert-triage/event", json=ALERT).json()

    assert body["autonomy"] == "read-only"
    assert body["pull_request"] is None
    assert "propose_change" not in {t.function.name for t in gateway.requests[0]["tools"]}
    assert ("propose_change", {}) not in toolbox.invoked


def test_the_webhook_token_restores_the_configured_stage() -> None:
    app, _, gateway = build(
        AuthPolicy(webhook_token="s3cret"),
        turns=[_answer("no action needed")],
    )
    with TestClient(app) as client:
        body = client.post(
            "/operators/alert-triage/event",
            json=ALERT,
            headers={"Authorization": "Bearer s3cret"},
        ).json()

    assert body["autonomy"] == "suggest"
    assert "propose_change" in {t.function.name for t in gateway.requests[0]["tools"]}


def test_a_wrong_token_is_treated_as_anonymous() -> None:
    app, _, _ = build(AuthPolicy(webhook_token="s3cret"), turns=[_answer("looked")])
    with TestClient(app) as client:
        body = client.post(
            "/operators/alert-triage/event",
            json=ALERT,
            headers={"Authorization": "Bearer wrong"},
        ).json()
    assert body["autonomy"] == "read-only"


def test_require_auth_refuses_an_unsigned_webhook() -> None:
    app, _, _ = build(AuthPolicy(webhook_token="s3cret", require_auth=True))
    with TestClient(app) as client:
        assert client.post("/operators/alert-triage/event", json=ALERT).status_code == 401


def test_an_unknown_operator_is_a_404(open_runtime) -> None:
    client, _, _ = open_runtime
    response = client.post("/operators/not-an-operator/event", json={})
    assert response.status_code == 404
    assert "alert-triage" in response.json()["detail"]["available"]


def test_an_operator_never_runs_above_the_global_default() -> None:
    """An operator policy is a ceiling narrower than the global one, never a way
    around it."""
    lowered = CONFIGMAP_YAML.replace("default: suggest", "default: read-only")
    app, _, gateway = build(
        AuthPolicy(webhook_token="s3cret"), turns=[_answer("looked")], config_yaml=lowered
    )
    with TestClient(app) as client:
        body = client.post(
            "/operators/alert-triage/event",
            json=ALERT,
            headers={"Authorization": "Bearer s3cret"},
        ).json()
    assert body["autonomy"] == "read-only"


def test_findings_accumulate_and_are_listed(open_runtime) -> None:
    client, _, _ = open_runtime
    client.post("/operators/alert-triage/event", json=ALERT)
    client.post("/operators/upgrade-preflight/event", json={"target": "1.31"})
    listing = client.get("/findings").json()
    assert listing["count"] == 2
    filtered = client.get("/findings", params={"operator": "alert-triage"}).json()
    assert filtered["count"] == 1


# --------------------------------------------------------------------------- #
# Reaching a Strict-mode LLM gateway
# --------------------------------------------------------------------------- #


def test_the_run_presents_a_token_to_the_llm_gateway() -> None:
    """agentgateway runs `jwtAuthentication: Strict` across the whole Gateway,
    so a completion request carrying no `Authorization` header is a 401 and the
    agent loop cannot run at all in the platform. The loop used to send only
    `X-Adhar-Tenant`.

    The token is also what agentgateway meters the per-group token budget
    against, so billing a user's run to the runtime's own service account would
    make per-team budgets meaningless.
    """
    policy = AuthPolicy(webhook_token="s3cret")
    app, _, gateway = build(policy, turns=[_answer("no action needed")])
    with TestClient(app) as client:
        client.post(
            "/operators/alert-triage/event",
            json=ALERT,
            headers={"Authorization": "Bearer s3cret"},
        )
    # No Keycloak service account is configured in this test, so the bearer is
    # empty — but the parameter is plumbed all the way through, which is what
    # was missing. With a client secret set it carries a real token.
    assert "bearer" in gateway.requests[0]


async def test_a_service_token_is_minted_when_there_is_no_caller_token() -> None:
    """A poller has no user token to forward. Without an identity of its own the
    runtime cannot reach a Strict-mode gateway, so no operator could ever run."""
    import respx
    from httpx import Response

    policy = AuthPolicy(
        issuer="https://keycloak.adhar.localtest.me:8443/realms/adhar",
        client_id="adhar-ai",
        client_secret="shh",
    )
    assert policy.service_account_enabled is True

    with respx.mock:
        route = respx.post(
            "https://keycloak.adhar.localtest.me:8443/realms/adhar"
            "/protocol/openid-connect/token"
        ).mock(return_value=Response(200, json={"access_token": "svc-tok", "expires_in": 300}))
        assert await policy.service_token() == "svc-tok"
        # Cached: a Keycloak round-trip per agent step would be absurd.
        assert await policy.service_token() == "svc-tok"
        assert route.call_count == 1


async def test_a_caller_token_is_preferred_over_the_service_token() -> None:
    policy = AuthPolicy(issuer="https://kc/realms/adhar", client_id="adhar-ai",
                        client_secret="shh")
    caller = Principal(subject="dev", authenticated=True, token="user-tok")
    assert await policy.bearer_for(caller) == "user-tok"


async def test_an_unreachable_keycloak_does_not_stop_the_loop() -> None:
    """The bundled local-dev gateway requires no token at all, so a failure to
    mint one must fail at the gateway that cares, not here."""
    import respx

    policy = AuthPolicy(issuer="https://kc/realms/adhar", client_id="adhar-ai",
                        client_secret="shh")
    with respx.mock:
        respx.post("https://kc/realms/adhar/protocol/openid-connect/token").mock(
            side_effect=ConnectionError("no route to host")
        )
        assert await policy.service_token() == ""


# --------------------------------------------------------------------------- #
# A presented credential that does not verify
# --------------------------------------------------------------------------- #


def test_an_unverifiable_token_is_a_401_not_a_silent_downgrade() -> None:
    """Presenting a bad token is not the same as presenting none. Falling
    through to anonymous turned an expired session or a wrong realm into a quiet
    read-only downgrade that the caller reads as the agent refusing to help."""
    policy = AuthPolicy(
        issuer="https://keycloak.adhar.localtest.me:8443/realms/adhar",
        jwks_url="http://keycloak.adhar-system.svc.cluster.local:8080/certs",
    )
    app, _, _ = build(policy)
    with TestClient(app) as client:
        response = client.post(
            "/chat",
            json={"prompt": "hello"},
            headers={"Authorization": "Bearer not-a-real-jwt"},
        )
    assert response.status_code == 401
    assert "invalid_token" in response.headers["www-authenticate"]


# --------------------------------------------------------------------------- #
# Findings carry cluster evidence
# --------------------------------------------------------------------------- #


def test_findings_are_gated_like_chat() -> None:
    """A finding carries the evidence that produced it — tool arguments, pod
    names, log excerpts, cost figures — so an ungated listing hands out exactly
    the detail the read tools are RBAC-scoped to protect."""
    app, _, _ = build(AuthPolicy(require_auth=True))
    with TestClient(app) as client:
        assert client.get("/findings").status_code == 401
        assert client.get("/config").status_code == 401
        # The probe stays open: a readinessProbe presents no credential.
        assert client.get("/healthz").status_code == 200
