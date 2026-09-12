"""Environment + secret loading.

Every name here is read from the platform package manifests in
`platform/stack/packages/ai/adhar-ai/manifests/`. Where this repo prefers a
namespaced `ADHAR_AI_*` name, the manifest's plain name is accepted as a
fallback so the code runs unmodified against the shipped manifests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DOMAINS = ("cluster", "gitops", "provision", "observability", "security", "cost", "catalog")

#: Where LLM completions go IN THE PLATFORM: the ai/agentgateway data plane
#: (ADR-0025), not this repo's bundled gateway. It is an OpenAI-compatible base
#: URL that already includes `/v1`; a caller picks its provider by naming a model
#: (`claude-*` -> Anthropic, `gpt-*`/`o[1-9]-*` -> OpenAI, `local/*` -> vLLM).
#:
#: This is a DEFAULT, not a hardcoding: `LLM_GATEWAY_URL` (which the platform
#: manifests set to exactly this value) and `ADHAR_AI_LLM_GATEWAY_URL` both win,
#: which is how `docker compose` and `adhar-ai gateway` point the runtime and the
#: MCP servers at the bundled local-dev gateway instead.
PLATFORM_LLM_GATEWAY_URL = "http://adhar-ai-gateway.adhar-system.svc.cluster.local:8080/v1"

#: Domains whose manifest sets GITEA_WRITE_ENABLED=true (they carry a PR tool).
WRITE_DOMAINS = ("gitops", "provision", "security", "catalog")


def env(*names: str, default: str = "") -> str:
    """First non-empty value among ``names``."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.strip()
    return default


def env_bool(*names: str, default: bool = False) -> bool:
    raw = env(*names)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on", "enabled"}


def env_list(*names: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Comma-separated env value as a tuple, with blanks dropped."""
    raw = env(*names)
    if not raw:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def env_int(*names: str, default: int) -> int:
    raw = env(*names)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def openai_v1_base(url: str) -> str:
    """Normalize an OpenAI-compatible base URL so it ends in exactly one `/v1`.

    Two callers hand us two shapes and both must work:

    * the platform manifests set ``LLM_GATEWAY_URL`` to the agentgateway data
      plane *with* the prefix already on it —
      ``http://adhar-ai-gateway.adhar-system.svc.cluster.local:8080/v1`` — because
      that is the path agentgateway's LLM HTTPRoute matches;
    * ``docker compose`` and ``adhar-ai gateway`` hand us a bare origin
      (``http://gateway:8080``), since the bundled dev gateway mounts its routes
      at the root.

    Blindly appending ``/v1`` to the first would POST to ``/v1/v1/chat/completions``
    and 404 against the real gateway, so the suffix is added only when absent.
    """
    base = (url or "").strip().rstrip("/")
    if not base:
        return ""
    return base if base.endswith("/v1") else f"{base}/v1"


def parse_listen(listen: str, default_port: int = 8080) -> tuple[str, int]:
    """Parse a Go-style listen address (`:8080`, `0.0.0.0:8080`, `8080`)."""
    listen = (listen or "").strip()
    if not listen:
        return "0.0.0.0", default_port
    if listen.startswith(":"):
        return "0.0.0.0", int(listen[1:])
    if ":" in listen:
        host, _, port = listen.rpartition(":")
        return host or "0.0.0.0", int(port)
    return "0.0.0.0", int(listen)


@dataclass(slots=True)
class GiteaConfig:
    """Gitea REST. `api_url` is the *base* service URL; `/api/v1` is appended."""

    api_url: str = ""
    org: str = "adhar"
    bot_user: str = ""
    bot_token: str = ""
    write_enabled: bool = False
    write_repos: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> GiteaConfig:
        repos = env("GITEA_WRITE_REPOS", "ADHAR_AI_GITEA_WRITE_REPOS", default="")
        return cls(
            api_url=env("GITEA_API_URL", "ADHAR_AI_GITEA_API_URL").rstrip("/"),
            org=env("GITEA_ORG", "ADHAR_AI_GITEA_ORG", default="adhar"),
            bot_user=env("GITEA_BOT_USER", default="adhar-ai-bot"),
            bot_token=env("GITEA_BOT_TOKEN"),
            write_enabled=env_bool("GITEA_WRITE_ENABLED"),
            write_repos=tuple(r.strip() for r in repos.split(",") if r.strip()),
        )


@dataclass(slots=True)
class ArgoCDConfig:
    """ArgoCD REST. Credentials follow the platform's `argocd-credentials`
    ExternalSecret convention (see adhar-console's manifests)."""

    url: str = ""
    username: str = "admin"
    password: str = ""
    token: str = ""
    verify_tls: bool = True

    @classmethod
    def from_env(cls) -> ArgoCDConfig:
        return cls(
            url=env("ARGOCD_URL", "ADHAR_AI_ARGOCD_URL").rstrip("/"),
            username=env("ARGOCD_USERNAME", default="admin"),
            # ARGOCD_ADMIN_PASSWORD is the raw key name in `argocd-credentials`,
            # accepted here so a bare `envFrom` of that secret also works.
            password=env("ARGOCD_PASSWORD", "ARGOCD_ADMIN_PASSWORD"),
            token=env("ARGOCD_AUTH_TOKEN", "ARGOCD_TOKEN"),
            verify_tls=env_bool("ARGOCD_VERIFY_TLS", default=False),
        )

    @property
    def configured(self) -> bool:
        return bool(self.url and (self.token or self.password))


@dataclass(slots=True)
class TelemetryConfig:
    prometheus_url: str = ""
    loki_url: str = ""
    tempo_url: str = ""
    opencost_url: str = ""

    @classmethod
    def from_env(cls) -> TelemetryConfig:
        return cls(
            prometheus_url=env("PROMETHEUS_URL", "ADHAR_AI_PROMETHEUS_URL").rstrip("/"),
            loki_url=env("LOKI_URL", "ADHAR_AI_LOKI_URL").rstrip("/"),
            tempo_url=env("TEMPO_URL", "ADHAR_AI_TEMPO_URL").rstrip("/"),
            opencost_url=env("OPENCOST_URL", "ADHAR_AI_OPENCOST_URL").rstrip("/"),
        )


@dataclass(slots=True)
class MCPConfig:
    domain: str = "cluster"
    gitea: GiteaConfig = field(default_factory=GiteaConfig)
    argocd: ArgoCDConfig = field(default_factory=ArgoCDConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    llm_gateway_url: str = PLATFORM_LLM_GATEWAY_URL
    #: Empty in the platform: ai/agentgateway validates the Keycloak JWT in front
    #: of all seven servers (ADR-0025). Retained for the token-exchange path and
    #: for running a server standalone in development.
    oidc_issuer_url: str = ""
    oidc_client_id: str = ""
    #: Host header allow-list for the MCP transport's DNS-rebinding guard.
    #:
    #: The MCP SDK defaults this to localhost only, which is right for a laptop
    #: and WRONG in a Pod: agentgateway federates these servers by Service
    #: `backendRef`, so requests arrive with a Host of
    #: `adhar-ai-mcp-<domain>.adhar-system.svc.cluster.local:8080`, the Pod IP,
    #: or whatever the proxy chose — a set that cannot be enumerated ahead of
    #: time. Under the SDK default every one of those is answered `421 Invalid
    #: Host header`, which takes out the entire federated tool surface.
    #:
    #: `("*",)` — the default — turns the guard off. That is a deliberate,
    #: narrow trade: DNS rebinding is a *browser* attack against a loopback-bound
    #: dev server, and there is no browser on a Pod's loopback. In the platform
    #: the real boundary is the one ADR-0025 put in front of all seven servers:
    #: agentgateway validates the Keycloak JWT and authorizes per tool. Set
    #: `ADHAR_AI_MCP_ALLOWED_HOSTS` to a comma-separated list to switch the
    #: guard back on when a server is exposed some other way.
    allowed_hosts: tuple[str, ...] = ("*",)

    @property
    def dns_rebinding_protection(self) -> bool:
        """True when `allowed_hosts` is a real allow-list rather than `*`."""
        return "*" not in self.allowed_hosts

    @classmethod
    def from_env(cls, domain: str | None = None) -> MCPConfig:
        resolved = (domain or env("ADHAR_AI_MCP_DOMAIN", "MCP_DOMAIN", default="cluster")).lower()
        if resolved not in DOMAINS:
            raise ValueError(f"unknown MCP domain {resolved!r}; expected one of {DOMAINS}")
        return cls(
            domain=resolved,
            gitea=GiteaConfig.from_env(),
            argocd=ArgoCDConfig.from_env(),
            telemetry=TelemetryConfig.from_env(),
            llm_gateway_url=env(
                "LLM_GATEWAY_URL",
                "ADHAR_AI_LLM_GATEWAY_URL",
                default=PLATFORM_LLM_GATEWAY_URL,
            ).rstrip("/"),
            # Kept for the token EXCHANGE path (a caller's token -> a short-lived
            # RBAC-scoped Kubernetes token) and for local development. Per-server
            # token VALIDATION is gone: in the platform every request arrives
            # through ai/agentgateway, which validates the Keycloak JWT once for
            # all seven servers and authorizes per tool (ADR-0025), so the
            # manifests no longer set these at all and they are normally empty.
            oidc_issuer_url=env("OIDC_ISSUER_URL"),
            oidc_client_id=env("OIDC_CLIENT_ID", default="adhar-ai"),
            allowed_hosts=env_list(
                "ADHAR_AI_MCP_ALLOWED_HOSTS",
                "MCP_ALLOWED_HOSTS",
                default=("*",),
            ),
        )


# --------------------------------------------------------------------------- #
# LLM gateway
# --------------------------------------------------------------------------- #

#: Default model per provider. Anthropic is the platform default (ADR-0024 §4).
DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-4o",
    "azure": "",  # Azure addresses a *deployment*, so there is no safe default.
    "openai-compatible": "",
    "ollama": "llama3.1",
}

#: `claude` is the alias the platform manifests use for the Anthropic provider.
PROVIDER_ALIASES = {
    "claude": "anthropic",
    "anthropic": "anthropic",
    "openai": "openai",
    "azure": "azure",
    "azure-openai": "azure",
    "openai-compatible": "openai-compatible",
    "compatible": "openai-compatible",
    "ollama": "ollama",
}


@dataclass(slots=True)
class Budgets:
    per_user_daily_tokens: int = 2_000_000
    per_op_max_tool_calls: int = 40
    per_op_max_tokens: int = 400_000
    max_concurrent_sessions: int = 8
    rate_limit_requests_per_minute: int = 60

    @classmethod
    def from_env(cls) -> Budgets:
        return cls(
            per_user_daily_tokens=env_int("BUDGET_PER_USER_DAILY_TOKENS", default=2_000_000),
            per_op_max_tool_calls=env_int("BUDGET_PER_OP_MAX_TOOL_CALLS", default=40),
            per_op_max_tokens=env_int("BUDGET_PER_OP_MAX_TOKENS", default=400_000),
            max_concurrent_sessions=env_int("BUDGET_MAX_CONCURRENT_SESSIONS", default=8),
            rate_limit_requests_per_minute=env_int("RATE_LIMIT_REQUESTS_PER_MINUTE", default=60),
        )


@dataclass(slots=True)
class LLMConfig:
    provider: str = "anthropic"
    api_key: str = ""
    model: str = ""
    endpoint: str = ""
    budgets: Budgets = field(default_factory=Budgets)
    response_cache: bool = True

    @classmethod
    def from_env(cls) -> LLMConfig:
        raw = env(
            "ADHAR_AI_LLM_PROVIDER",  # this repo's canonical name
            "PROVIDER",  # key projected from the `adhar-ai-llm` secret
            "ADHAR_AI_DEFAULT_PROVIDER",  # static default in llm-gateway.yaml
            default="anthropic",
        ).lower()
        provider = PROVIDER_ALIASES.get(raw, raw)
        if provider not in DEFAULT_MODELS:
            raise ValueError(
                f"unknown LLM provider {raw!r}; expected one of {sorted(PROVIDER_ALIASES)}"
            )
        return cls(
            provider=provider,
            api_key=env("ADHAR_AI_LLM_API_KEY", "API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"),
            model=env("ADHAR_AI_LLM_MODEL", "MODEL", default=DEFAULT_MODELS[provider]),
            endpoint=env("ADHAR_AI_LLM_ENDPOINT", "ENDPOINT").rstrip("/"),
            budgets=Budgets.from_env(),
            response_cache=env("ADHAR_AI_RESPONSE_CACHE", default="enabled").lower() != "disabled",
        )

    @property
    def keyed(self) -> bool:
        """Ollama needs no key; everything else does. Unkeyed => read-only
        posture, reported by /healthz rather than crash-looping (ADR-0024 §9)."""
        return self.provider == "ollama" or bool(self.api_key)


# --------------------------------------------------------------------------- #
# Agent runtime
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RuntimeEnv:
    llm_gateway_url: str = PLATFORM_LLM_GATEWAY_URL
    #: The model the loop NAMES when a request does not choose one. Under
    #: agentgateway the model name is the routing key, so a request without one
    #: falls through to the gateway's fallback rule rather than being routed.
    llm_model: str = ""
    oidc_issuer_url: str = ""
    oidc_client_id: str = "adhar-ai"
    rag_dsn: str = ""
    docs_path: str = ""

    @classmethod
    def from_env(cls) -> RuntimeEnv:
        return cls(
            llm_gateway_url=env(
                "LLM_GATEWAY_URL",
                "ADHAR_AI_LLM_GATEWAY_URL",
                default=PLATFORM_LLM_GATEWAY_URL,
            ).rstrip("/"),
            llm_model=env("ADHAR_AI_LLM_MODEL", "MODEL", default=DEFAULT_MODELS["anthropic"]),
            oidc_issuer_url=env("OIDC_ISSUER_URL"),
            oidc_client_id=env("OIDC_CLIENT_ID", default="adhar-ai"),
            rag_dsn=rag_dsn_from_env(),
            docs_path=env("ADHAR_AI_DOCS_PATH", default="/etc/adhar-ai/docs"),
        )


def rag_dsn_from_env() -> str:
    """`ADHAR_AI_RAG_DSN` wins; otherwise compose one from the manifest's
    RAG_DB_* env plus the CNPG-issued `adhar-ai-rag-app` secret (whose keys
    arrive lowercase via `envFrom`)."""
    dsn = env("ADHAR_AI_RAG_DSN", "RAG_DSN", "DATABASE_URL")
    if dsn:
        return dsn
    host = env("RAG_DB_HOST", "host")
    if not host:
        return ""
    name = env("RAG_DB_NAME", "dbname", default="adhar_ai_rag")
    port = env("RAG_DB_PORT", "port", default="5432")
    user = env("RAG_DB_USER", "username", "user", "PGUSER", default="adhar_ai")
    password = env("RAG_DB_PASSWORD", "password", "PGPASSWORD")
    auth = f"{user}:{password}@" if password else f"{user}@"
    return f"postgresql://{auth}{host}:{port}/{name}"
