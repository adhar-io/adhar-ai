"""Who may drive the agent, and how far.

The runtime is the one AI surface not behind agentgateway, so these are the
controls that decide whether reaching the port is the same thing as being able
to open a pull request against the platform repos. It used to be.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers
from starlette.requests import Request

from adhar_ai.runtime.auth import ANONYMOUS, AuthPolicy, Principal, _normalize_groups
from adhar_ai.runtime.autonomy import (
    LADDER,
    AutonomyError,
    RuntimeConfig,
    WritePolicy,
    lower_of,
    rank,
)
from adhar_ai.runtime.loop import Session


def request_with(headers: dict[str, str] | None = None) -> Request:
    raw = Headers(headers or {}).raw
    return Request({"type": "http", "method": "POST", "path": "/chat", "headers": raw})


# --------------------------------------------------------------------------- #
# The unauthenticated posture
# --------------------------------------------------------------------------- #


def test_an_unauthenticated_caller_is_answered_but_cannot_write() -> None:
    """The hole this closes: `POST /operators/alert-triage/event` took no
    credential at all, so anyone who could reach the pod could drive an LLM run
    that opens a real Gitea PR at the shipped `suggest` stage."""
    principal = AuthPolicy().principal(request_with())
    assert principal.authenticated is False
    assert principal.subject == ANONYMOUS
    assert principal.write_allowed is False
    # Investigation stays open; authority does not.
    assert principal.ceiling("scoped") == "read-only"
    assert principal.ceiling("suggest") == "read-only"


def test_require_auth_refuses_instead_of_downgrading() -> None:
    policy = AuthPolicy(require_auth=True)
    with pytest.raises(HTTPException) as raised:
        policy.principal(request_with())
    assert raised.value.status_code == 401
    assert raised.value.headers["WWW-Authenticate"] == "Bearer"


def test_the_401_says_what_it_would_have_accepted() -> None:
    policy = AuthPolicy(require_auth=True, webhook_token="s3cret")
    with pytest.raises(HTTPException) as raised:
        policy.principal(request_with(), allow_webhook_token=True)
    assert "webhook" in str(raised.value.detail["accepts"]).lower()


def test_an_unkeyed_runtime_says_it_accepts_nothing() -> None:
    policy = AuthPolicy(require_auth=True)
    with pytest.raises(HTTPException) as raised:
        policy.principal(request_with())
    assert "no credential is configured" in str(raised.value.detail["accepts"])


# --------------------------------------------------------------------------- #
# Webhook token
# --------------------------------------------------------------------------- #


def test_the_webhook_token_authenticates_alertmanager() -> None:
    """Alertmanager holds no OIDC client, only
    `http_config.authorization.credentials` — a bearer token."""
    policy = AuthPolicy(webhook_token="s3cret")
    principal = policy.principal(
        request_with({"authorization": "Bearer s3cret"}), allow_webhook_token=True
    )
    assert principal.authenticated is True
    assert principal.method == "webhook-token"
    assert principal.ceiling("suggest") == "suggest"


def test_a_wrong_webhook_token_does_not_authenticate() -> None:
    policy = AuthPolicy(webhook_token="s3cret")
    principal = policy.principal(
        request_with({"authorization": "Bearer guess"}), allow_webhook_token=True
    )
    assert principal.authenticated is False
    assert principal.ceiling("suggest") == "read-only"


def test_the_webhook_token_is_not_accepted_on_the_chat_route() -> None:
    """`/chat` is for people. Only the operator webhook takes the shared secret,
    so leaking it cannot turn into an interactive agent session."""
    policy = AuthPolicy(webhook_token="s3cret")
    principal = policy.principal(request_with({"authorization": "Bearer s3cret"}))
    assert principal.authenticated is False


def test_a_non_bearer_authorization_header_is_ignored() -> None:
    policy = AuthPolicy(webhook_token="s3cret")
    principal = policy.principal(
        request_with({"authorization": "Basic czNjcmV0"}), allow_webhook_token=True
    )
    assert principal.authenticated is False


# --------------------------------------------------------------------------- #
# Groups
# --------------------------------------------------------------------------- #


def test_keycloak_group_paths_are_compared_on_the_leaf() -> None:
    """Keycloak renders groups as paths (`/platform-admin`), and a nested group
    as `/adhar/platform-admin`."""
    assert _normalize_groups(["/platform-admin"]) == ("platform-admin",)
    assert _normalize_groups(["/adhar/platform-admin"]) == ("platform-admin",)
    assert _normalize_groups("platform-developer") == ("platform-developer",)
    assert _normalize_groups(None) == ()


def test_a_developer_may_investigate_but_not_propose() -> None:
    """ADR-0025's authorization table, enforced on the runtime's own surface."""
    developer = Principal(
        subject="dev", groups=("platform-developer",), method="oidc",
        authenticated=True, write_allowed=False,
    )
    assert developer.ceiling("suggest") == "read-only"

    admin = Principal(
        subject="ops", groups=("platform-admin",), method="oidc",
        authenticated=True, write_allowed=True,
    )
    assert admin.ceiling("suggest") == "suggest"


def test_the_auth_posture_is_reported_without_the_secret() -> None:
    described = AuthPolicy(webhook_token="s3cret", issuer="https://kc/realms/adhar").describe()
    assert described["webhookToken"] == "configured"
    assert "s3cret" not in str(described)


# --------------------------------------------------------------------------- #
# The ladder
# --------------------------------------------------------------------------- #


def test_authority_only_ever_narrows() -> None:
    """`lower_of` is what stops a request body widening its own authority: a
    `/chat` caller may ask for less than the ConfigMap allows, never more."""
    assert lower_of("scoped", "read-only") == "read-only"
    assert lower_of("suggest", "approve-to-apply") == "suggest"
    assert lower_of("scoped") == "scoped"
    assert lower_of(*LADDER) == "read-only"


def test_an_unknown_stage_is_rejected_rather_than_guessed() -> None:
    with pytest.raises(AutonomyError):
        lower_of("suggest", "yolo")
    with pytest.raises(AutonomyError):
        rank("root")


def test_suggest_stops_at_the_first_proposal_and_higher_rungs_do_not() -> None:
    """The observable difference between `suggest` and `approve-to-apply`.
    Before this, all three write rungs behaved identically."""
    assert Session(autonomy="suggest").stop_after_write is True
    assert Session(autonomy="approve-to-apply").stop_after_write is False
    assert Session(autonomy="scoped").stop_after_write is False
    assert Session(autonomy="read-only").may_write is False


# --------------------------------------------------------------------------- #
# writePolicy, enforced where it is read
# --------------------------------------------------------------------------- #


def test_the_configmap_write_policy_is_actually_enforced() -> None:
    """It used to change what `GET /config` printed and nothing else.

    Note the path convention: a tool's `path` is RELATIVE TO ITS REPOSITORY, and
    the prefixes are repo-qualified, so `ai/adhar-ai/values.yaml` in `packages`
    is checked as `packages/ai/adhar-ai/values.yaml`.
    """
    policy = WritePolicy(allowed_repos=("packages",), allowed_path_prefixes=("packages/ai/",))
    session = Session(autonomy="suggest", write_policy=policy)

    assert session.write_refusal(
        {"repo": "packages", "changes": [{"path": "ai/adhar-ai/values.yaml"}]}
    ) == ""
    assert "outside this stage's allow-list" in session.write_refusal(
        {"repo": "environments", "changes": [{"path": "ai/x.yaml"}]}
    )
    assert "outside the allowed prefixes" in session.write_refusal(
        {"repo": "packages", "changes": [{"path": "security/kyverno/policy.yaml"}]}
    )


@pytest.mark.parametrize(
    ("repo", "path"),
    [
        ("packages", "security/vault/manifests/install.yaml"),
        ("packages", "ai/adhar-ai/values.yaml"),
        ("environments", "dev/apps/console.yaml"),
    ],
)
def test_the_runtime_and_the_server_agree_on_what_a_path_means(repo, path) -> None:
    """The two layers must decide the same way about the same change.

    They enforce the same policy in two processes, and they compare DIFFERENT
    strings if this is got wrong: the runtime saw the bare `path` while the MCP
    server qualifies it with the repo. A real package path like
    `security/vault/manifests/install.yaml` would then be refused by the runtime
    and accepted by the server, which is the worst way for a policy to disagree
    with itself.
    """
    from adhar_ai.config import GiteaConfig
    from adhar_ai.mcp.common.policy import guard_write

    session = Session(autonomy="suggest", write_policy=WritePolicy())
    assert session.write_refusal({"repo": repo, "changes": [{"path": path}]}) == ""
    # The server accepts it too (no WriteNotPermitted raised).
    guard_write(
        GiteaConfig(write_enabled=True, bot_token="t", write_repos=("packages", "environments")),
        repo,
        [path],
    )


def test_path_traversal_is_refused() -> None:
    session = Session(autonomy="suggest", write_policy=WritePolicy())
    assert "escapes the repository root" in session.write_refusal(
        {"repo": "packages", "changes": [{"path": "../../etc/passwd"}]}
    )


def test_scoped_autonomy_permits_nothing_until_a_scope_is_named() -> None:
    """`scoped` runs unattended, so inheriting the broad write policy would be
    a silent widening. It fails closed and says which field to set."""
    session = Session(autonomy="scoped", write_policy=WritePolicy())
    refusal = session.write_refusal(
        {"repo": "packages", "changes": [{"path": "ai/x.yaml"}]}
    )
    assert "no configured scope" in refusal
    assert "writePolicy.scoped" in refusal


def test_scoped_autonomy_uses_the_narrower_list_once_configured() -> None:
    policy = WritePolicy(
        allowed_repos=("packages", "environments"),
        allowed_path_prefixes=("packages/", "environments/"),
        scoped_repos=("environments",),
        scoped_path_prefixes=("environments/dev/",),
    )
    scoped = Session(autonomy="scoped", write_policy=policy)
    suggest = Session(autonomy="suggest", write_policy=policy)

    change = {"repo": "packages", "changes": [{"path": "ai/x.yaml"}]}
    assert suggest.write_refusal(change) == ""   # allowed at the lower rung
    assert scoped.write_refusal(change) != ""    # refused unattended

    dev = {"repo": "environments", "changes": [{"path": "dev/app.yaml"}]}
    assert scoped.write_refusal(dev) == ""


def test_no_write_policy_leaves_enforcement_to_the_mcp_server() -> None:
    assert Session(autonomy="suggest", write_policy=None).write_refusal(
        {"repo": "anything", "changes": [{"path": "wherever.yaml"}]}
    ) == ""


def test_the_scoped_allow_list_is_read_from_the_configmap() -> None:
    cfg = RuntimeConfig.from_mapping(
        {
            "autonomy": {"default": "suggest"},
            "writePolicy": {
                "allowedRepos": ["packages"],
                "allowedPathPrefixes": ["packages/"],
                "scoped": {
                    "allowedRepos": ["environments"],
                    "allowedPathPrefixes": ["environments/dev/"],
                },
            },
        }
    )
    assert cfg.write_policy.scoped_repos == ("environments",)
    assert cfg.write_policy.scope_for("scoped") == (
        ("environments",),
        ("environments/dev/",),
    )
    assert cfg.write_policy.scope_for("suggest") == (("packages",), ("packages/",))
