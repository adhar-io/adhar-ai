"""Who is calling the agent runtime, and what that lets them do.

The runtime is the ONE Adhar AI surface that does not sit behind
`ai/agentgateway`: it publishes its own hostname (`agent.<host>`) so the Console
and the `adhar ai` CLI can reach `/chat` directly, and so Alertmanager and
ArgoCD notifications can reach `/operators/{name}/event`. ADR-0025 moved
identity and authorization into the data plane for the LLM and MCP surfaces —
this module is what keeps the runtime from being the hole left behind.

Two caller classes, two credentials:

* **People** (Console, `adhar ai`) present a **Keycloak JWT**. It is verified
  against the realm's JWKS, and its `groups` claim decides whether the caller
  may drive a write. The issuer is checked against the PUBLIC realm URL (it must
  match the `iss` claim byte-for-byte) while the JWKS may be fetched from the
  IN-CLUSTER Keycloak Service, so key retrieval does not depend on the
  platform's own ingress being up — the same split ADR-0025 specifies for
  agentgateway.
* **Machines** (Alertmanager, ArgoCD notifications) present a **shared webhook
  token**. Alertmanager has no OIDC client; it has
  `http_config.authorization.credentials`, which is exactly a bearer token.

The important design choice is what happens with NO credential. Refusing
outright would break `docker compose up` and every local run. Serving normally
would leave an unauthenticated path to an LLM run that opens real Gitea PRs at
the shipped `suggest` stage. So the rule is:

    an unauthenticated caller is answered, but is pinned to `read-only`

Investigation stays open to anyone who can reach the port; *authority* requires
a credential. Set `ADHAR_AI_REQUIRE_AUTH=true` to refuse the request instead,
which is the right posture once the platform's own clients are wired up.
"""

from __future__ import annotations

import hmac
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request

from ..config import env, env_bool, env_list

log = logging.getLogger("adhar_ai.runtime.auth")

#: Keycloak groups that may drive a write (a Gitea PR). Mirrors the
#: authorization table in ADR-0025: `platform-admin` gets the PR-opening tools,
#: `platform-developer` gets reads. Keycloak renders group paths with a leading
#: slash (`/platform-admin`), which `_normalize_groups` strips.
DEFAULT_WRITE_GROUPS = ("platform-admin",)

ANONYMOUS = "anonymous"


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated identity behind one request."""

    subject: str = ANONYMOUS
    groups: tuple[str, ...] = ()
    method: str = ANONYMOUS
    authenticated: bool = False
    #: The caller's verified bearer, kept so the loop can present it to the LLM
    #: gateway. agentgateway runs `jwtAuthentication` in `Strict` mode across the
    #: whole Gateway, so a completion request with no token is a 401 — and it
    #: also meters token budgets per Keycloak group, which only works if the
    #: request still carries the identity that spent them.
    token: str = ""
    #: False when the caller authenticated but holds no write-capable group.
    write_allowed: bool = False

    def ceiling(self, configured: str) -> str:
        """The highest autonomy stage this caller may reach.

        An unauthenticated caller — or an authenticated one outside the write
        groups — is pinned to `read-only` no matter what the ConfigMap says.
        """
        return configured if self.write_allowed else "read-only"

    def as_dict(self) -> dict[str, Any]:
        """What a response may say about the caller. Never the token itself."""
        return {
            "subject": self.subject,
            "method": self.method,
            "authenticated": self.authenticated,
            "groups": list(self.groups),
            "write_allowed": self.write_allowed,
        }


def _normalize_groups(raw: Any) -> tuple[str, ...]:
    """Keycloak emits group paths (`/platform-admin`); compare on the leaf."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return ()
    return tuple(str(g).strip("/").split("/")[-1] for g in raw if str(g).strip("/"))


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


@dataclass
class AuthPolicy:
    """How the runtime decides who a caller is. Built once, at start-up."""

    #: Public realm URL. Must equal the token's `iss` claim exactly.
    issuer: str = ""
    #: Where to fetch signing keys. Defaults to the issuer's well-known path;
    #: override to reach Keycloak in-cluster while `iss` stays public.
    jwks_url: str = ""
    audience: str = ""
    #: Shared bearer for machine webhooks (Alertmanager, ArgoCD notifications).
    webhook_token: str = ""
    write_groups: tuple[str, ...] = DEFAULT_WRITE_GROUPS
    #: Refuse an unauthenticated request instead of pinning it to `read-only`.
    require_auth: bool = False
    #: The Keycloak client this runtime authenticates AS when it has no caller
    #: token to forward (pollers, webhook-authenticated operator events).
    client_id: str = "adhar-ai"
    client_secret: str = ""
    token_url: str = ""
    _jwks: Any = field(default=None, repr=False, compare=False)
    _service_token: str = field(default="", repr=False, compare=False)
    _service_token_expiry: float = field(default=0.0, repr=False, compare=False)

    @classmethod
    def from_env(cls) -> AuthPolicy:
        issuer = env("OIDC_ISSUER_URL", "ADHAR_AI_OIDC_ISSUER_URL").rstrip("/")
        return cls(
            issuer=issuer,
            jwks_url=env("ADHAR_AI_OIDC_JWKS_URL", "OIDC_JWKS_URL").rstrip("/")
            or (f"{issuer}/protocol/openid-connect/certs" if issuer else ""),
            audience=env("ADHAR_AI_OIDC_AUDIENCE", "OIDC_AUDIENCE"),
            webhook_token=env("ADHAR_AI_WEBHOOK_TOKEN", "WEBHOOK_TOKEN"),
            write_groups=env_list(
                "ADHAR_AI_WRITE_GROUPS", "WRITE_GROUPS", default=DEFAULT_WRITE_GROUPS
            ),
            require_auth=env_bool("ADHAR_AI_REQUIRE_AUTH", "REQUIRE_AUTH"),
            client_id=env("OIDC_CLIENT_ID", "ADHAR_AI_OIDC_CLIENT_ID", default="adhar-ai"),
            client_secret=env("ADHAR_AI_OIDC_CLIENT_SECRET", "OIDC_CLIENT_SECRET"),
            token_url=env("ADHAR_AI_OIDC_TOKEN_URL", "OIDC_TOKEN_URL").rstrip("/"),
        )

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.issuer and self.jwks_url)

    @property
    def service_account_enabled(self) -> bool:
        return bool(self.issuer and self.client_id and self.client_secret)

    async def service_token(self) -> str:
        """A token for the runtime's OWN identity, via OIDC client credentials.

        There are callers with no user token to forward: the drift and cost
        pollers, and an Alertmanager webhook authenticated by the shared secret.
        They still have to reach the LLM gateway, which enforces
        `jwtAuthentication: Strict` across the whole Gateway — so without an
        identity of its own the runtime simply cannot run an operator.

        The Keycloak `adhar-ai` client's service account supplies one. It is
        cached until shortly before it expires, because minting one per agent
        step would put a Keycloak round-trip inside the loop.
        """
        if not self.service_account_enabled:
            return ""
        now = time.time()
        if self._service_token and now < self._service_token_expiry:
            return self._service_token

        import httpx

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    f"{self.token_url or self.issuer + '/protocol/openid-connect/token'}",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:  # noqa: BLE001
            # Never fatal: against the bundled local gateway there is no JWT
            # requirement at all, so an unreachable Keycloak must not stop the
            # loop — it should fail, if at all, at the gateway that cares.
            log.warning("could not mint a service token: %s: %s", type(exc).__name__, exc)
            return ""

        self._service_token = str(payload.get("access_token", ""))
        # 30s of slack so a token never expires in flight mid-agent-run.
        self._service_token_expiry = now + max(0, int(payload.get("expires_in", 60)) - 30)
        return self._service_token

    async def bearer_for(self, principal: Principal) -> str:
        """The token the LLM gateway should see for this run.

        The caller's own token is preferred: agentgateway meters token budgets
        per Keycloak group, so billing an operator's spend to the runtime's
        service account would make per-team budgets meaningless.
        """
        return principal.token or await self.service_token()

    def describe(self) -> dict[str, Any]:
        """What `/healthz` and `/config` report. Never the token itself."""
        return {
            "oidc": self.issuer or "disabled",
            "webhookToken": "configured" if self.webhook_token else "unset",
            "requireAuth": self.require_auth,
            "serviceAccount": "configured" if self.service_account_enabled else "unset",
            "writeGroups": list(self.write_groups),
            "unauthenticated": "refused" if self.require_auth else "answered as read-only",
        }

    # ------------------------------------------------------------------ jwt --

    def _client(self) -> Any:
        """Lazily build a cached PyJWKClient. It caches keys and refetches on an
        unknown `kid`, so a Keycloak key rotation needs no restart."""
        if self._jwks is None:
            import jwt  # imported here so a runtime with OIDC off never needs it

            self._jwks = jwt.PyJWKClient(self.jwks_url, cache_keys=True)
        return self._jwks

    def _verify(self, token: str) -> dict[str, Any] | None:
        import jwt

        try:
            key = self._client().get_signing_key_from_jwt(token).key
            return jwt.decode(
                token,
                key,
                algorithms=["RS256", "RS512", "ES256"],
                issuer=self.issuer,
                audience=self.audience or None,
                # Keycloak puts the client id in `azp` and only lists an
                # `aud` when a client scope adds one, so audience is verified
                # only when the operator asked for it.
                options={"verify_aud": bool(self.audience)},
            )
        except Exception as exc:  # noqa: BLE001 - any failure is a failed auth
            log.warning("rejected bearer token: %s: %s", type(exc).__name__, exc)
            return None

    # ------------------------------------------------------------- decision --

    def principal(self, request: Request, *, allow_webhook_token: bool = False) -> Principal:
        """Identify the caller. Raises 401 only when `require_auth` is set."""
        token = _bearer(request)

        if allow_webhook_token and self.webhook_token and token:
            # compare_digest, not `==`: a plain comparison leaks the shared
            # secret one byte at a time to anyone who can time the endpoint.
            if hmac.compare_digest(token, self.webhook_token):
                return Principal(
                    subject="webhook",
                    method="webhook-token",
                    authenticated=True,
                    write_allowed=True,
                )

        if token and self.oidc_enabled:
            claims = self._verify(token)
            if claims is not None:
                groups = _normalize_groups(claims.get("groups"))
                return Principal(
                    subject=str(
                        claims.get("preferred_username") or claims.get("sub") or "unknown"
                    ),
                    groups=groups,
                    method="oidc",
                    authenticated=True,
                    token=token,
                    write_allowed=any(g in self.write_groups for g in groups),
                )
            # A token was PRESENTED and did not verify. That is not the same as
            # presenting none: falling through to anonymous would turn an
            # expired session, a wrong realm or a tampered token into a quiet
            # downgrade that the caller reads as "the agent refused to help".
            # Say what happened.
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "the bearer token could not be verified",
                    "issuer": self.issuer,
                    "hint": "the token may be expired, from another realm, or malformed",
                },
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )

        if self.require_auth:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "authentication required",
                    "accepts": _accepted(self, allow_webhook_token),
                    "hint": (
                        "ADHAR_AI_REQUIRE_AUTH is set, so this runtime refuses "
                        "unauthenticated requests."
                    ),
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
        return Principal()


def _accepted(policy: AuthPolicy, allow_webhook_token: bool) -> list[str]:
    accepted = []
    if policy.oidc_enabled:
        accepted.append(f"Keycloak JWT from {policy.issuer}")
    if allow_webhook_token and policy.webhook_token:
        accepted.append("the shared webhook bearer token")
    return accepted or ["nothing — no credential is configured on this runtime"]
