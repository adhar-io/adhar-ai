"""Config parsing against the exact env the platform manifests set, and the
provenance markers the Kyverno guardrail keys off."""

from __future__ import annotations

import pytest

from adhar_ai.config import (
    DEFAULT_MODELS,
    DOMAINS,
    PLATFORM_LLM_GATEWAY_URL,
    WRITE_DOMAINS,
    ArgoCDConfig,
    GiteaConfig,
    MCPConfig,
    RuntimeEnv,
    openai_v1_base,
    parse_listen,
    rag_dsn_from_env,
)
from adhar_ai.provenance import (
    BRANCH_PREFIX,
    ORIGIN_LABEL_KEY,
    ORIGIN_LABEL_VALUE,
    PR_TITLE_PREFIX,
    branch_name,
    commit_trailer,
    pr_title,
    render_pr_body,
    slugify,
)


@pytest.mark.parametrize(
    "raw,expected",
    [(":8080", ("0.0.0.0", 8080)), ("0.0.0.0:9090", ("0.0.0.0", 9090)), ("8080", ("0.0.0.0", 8080)),
     ("", ("0.0.0.0", 8080)), ("127.0.0.1:1234", ("127.0.0.1", 1234))],
)
def test_parse_listen_accepts_the_go_style_flag(raw, expected):
    assert parse_listen(raw) == expected


def test_mcp_domain_from_the_manifest_env(monkeypatch):
    """The manifests set MCP_DOMAIN; this repo also honours ADHAR_AI_MCP_DOMAIN."""
    monkeypatch.setenv("MCP_DOMAIN", "observability")
    assert MCPConfig.from_env().domain == "observability"
    monkeypatch.setenv("ADHAR_AI_MCP_DOMAIN", "security")
    assert MCPConfig.from_env().domain == "security"
    # An explicit --domain argument wins over both.
    assert MCPConfig.from_env("cost").domain == "cost"


def test_unknown_domain_is_rejected(monkeypatch):
    monkeypatch.setenv("MCP_DOMAIN", "nonsense")
    with pytest.raises(ValueError, match="unknown MCP domain"):
        MCPConfig.from_env()


def test_write_domains_match_the_manifest_split():
    assert set(WRITE_DOMAINS) < set(DOMAINS)
    assert set(WRITE_DOMAINS) == {"gitops", "provision", "security", "catalog"}


def test_gitea_config_from_the_manifest_env(monkeypatch):
    monkeypatch.setenv("GITEA_API_URL", "http://gitea-http.adhar-system.svc.cluster.local:3000/")
    monkeypatch.setenv("GITEA_WRITE_ENABLED", "true")
    monkeypatch.setenv("GITEA_WRITE_REPOS", "packages,environments")
    monkeypatch.setenv("GITEA_BOT_USER", "adhar-ai-bot")
    monkeypatch.setenv("GITEA_BOT_TOKEN", "tok")
    cfg = GiteaConfig.from_env()
    assert cfg.api_url.endswith(":3000")  # trailing slash trimmed
    assert cfg.write_enabled is True
    assert cfg.write_repos == ("packages", "environments")
    assert cfg.org == "adhar"  # the platform's Gitea org


def test_argocd_accepts_the_argocd_credentials_secret_key(monkeypatch):
    monkeypatch.setenv("ARGOCD_URL", "http://argo-cd-argocd-server.adhar-system.svc.cluster.local:80")
    monkeypatch.setenv("ARGOCD_ADMIN_PASSWORD", "from-external-secret")
    cfg = ArgoCDConfig.from_env()
    assert cfg.password == "from-external-secret"
    assert cfg.configured is True


def test_argocd_is_unconfigured_without_a_credential(monkeypatch):
    monkeypatch.setenv("ARGOCD_URL", "http://argocd")
    for key in ("ARGOCD_PASSWORD", "ARGOCD_ADMIN_PASSWORD", "ARGOCD_AUTH_TOKEN", "ARGOCD_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    assert ArgoCDConfig.from_env().configured is False


def test_rag_dsn_is_composed_from_the_cnpg_secret_env(monkeypatch):
    """agent-runtime.yaml sets RAG_DB_HOST/RAG_DB_NAME and envFrom's the CNPG
    app secret, whose keys arrive lowercase."""
    monkeypatch.delenv("ADHAR_AI_RAG_DSN", raising=False)
    monkeypatch.setenv("RAG_DB_HOST", "adhar-ai-rag-rw.adhar-system.svc.cluster.local")
    monkeypatch.setenv("RAG_DB_NAME", "adhar_ai_rag")
    monkeypatch.setenv("username", "adhar_ai")
    monkeypatch.setenv("password", "s3cret")
    assert rag_dsn_from_env() == (
        "postgresql://adhar_ai:s3cret@adhar-ai-rag-rw.adhar-system.svc.cluster.local"
        ":5432/adhar_ai_rag"
    )


def test_explicit_rag_dsn_wins(monkeypatch):
    monkeypatch.setenv("ADHAR_AI_RAG_DSN", "postgresql://u@h/db")
    monkeypatch.setenv("RAG_DB_HOST", "ignored")
    assert rag_dsn_from_env() == "postgresql://u@h/db"


def test_rag_dsn_is_empty_when_nothing_is_configured(monkeypatch):
    for key in ("ADHAR_AI_RAG_DSN", "RAG_DSN", "DATABASE_URL", "RAG_DB_HOST", "host"):
        monkeypatch.delenv(key, raising=False)
    assert rag_dsn_from_env() == ""


def test_runtime_env_docs_path_default(monkeypatch):
    monkeypatch.delenv("ADHAR_AI_DOCS_PATH", raising=False)
    assert RuntimeEnv.from_env().docs_path == "/etc/adhar-ai/docs"


# ------------------------------------------------------------- provenance ----


def test_branch_names_are_namespaced_and_unique():
    a, b = branch_name("Raise demo memory limit"), branch_name("Raise demo memory limit")
    assert a.startswith(BRANCH_PREFIX) and b.startswith(BRANCH_PREFIX)
    assert a != b
    assert "raise-demo-memory-limit" in a


def test_slugify_is_dns_safe():
    assert slugify("Fix: Vault (OutOfSync!) — urgent") == "fix-vault-outofsync-urgent"
    assert slugify("") == "change"


def test_pr_title_is_prefixed_once():
    assert pr_title("scale demo").startswith(PR_TITLE_PREFIX)
    assert pr_title(pr_title("scale demo")).count(PR_TITLE_PREFIX) == 1


def test_commit_trailer_carries_model_audit_user_and_origin():
    trailer = commit_trailer("claude-sonnet-5", "aud-1", "alice")
    assert "model=claude-sonnet-5" in trailer
    assert "Audit-Id: aud-1" in trailer
    assert "Requested-by: alice" in trailer
    assert f"{ORIGIN_LABEL_KEY}: {ORIGIN_LABEL_VALUE}" in trailer


def test_pr_body_states_the_provenance_and_the_safety_property():
    body = render_pr_body("because", "aud-2", None, "claude-sonnet-5", ["a.yaml"], "propose_change")
    assert "because" in body
    assert "`a.yaml`" in body
    assert "aud-2" in body
    assert "propose_change" in body
    assert "only* write path" in body
    assert "operator (event-driven)" in body  # no user on an operator-opened PR


def test_audit_redacts_credential_shaped_fields():
    from adhar_ai.mcp.common.audit import redact

    out = redact({"token": "secret", "nested": {"api_key": "k", "app": "vault"}, "n": 1})
    assert out["token"] == "***"
    assert out["nested"]["api_key"] == "***"
    assert out["nested"]["app"] == "vault"
    assert out["n"] == 1


# ------------------------------------------------- the platform AI data plane --
#
# ADR-0025: the bundled gateway is a LOCAL-DEV component; in the platform, LLM
# traffic goes to ai/agentgateway. These tests pin the default and the two URL
# shapes that reach it.


def _clear_gateway_env(monkeypatch):
    for name in ("LLM_GATEWAY_URL", "ADHAR_AI_LLM_GATEWAY_URL", "MODEL", "ADHAR_AI_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "raw,expected",
    [
        # The platform manifests set the URL WITH the prefix (agentgateway's LLM
        # route matches /v1), so it must not be doubled.
        (PLATFORM_LLM_GATEWAY_URL, PLATFORM_LLM_GATEWAY_URL),
        (PLATFORM_LLM_GATEWAY_URL + "/", PLATFORM_LLM_GATEWAY_URL),
        # docker compose / `adhar-ai gateway` hand us a bare origin.
        ("http://gateway:8080", "http://gateway:8080/v1"),
        ("http://gateway:8080/", "http://gateway:8080/v1"),
        ("", ""),
    ],
)
def test_openai_base_carries_exactly_one_v1(raw, expected):
    assert openai_v1_base(raw) == expected


def test_mcp_and_runtime_default_to_the_platform_gateway(monkeypatch):
    _clear_gateway_env(monkeypatch)
    monkeypatch.setenv("MCP_DOMAIN", "cluster")
    assert MCPConfig.from_env().llm_gateway_url == PLATFORM_LLM_GATEWAY_URL
    assert RuntimeEnv.from_env().llm_gateway_url == PLATFORM_LLM_GATEWAY_URL


def test_the_local_dev_gateway_still_wins_when_set(monkeypatch):
    """`docker compose` sets LLM_GATEWAY_URL=http://gateway:8080 — the bundled
    gateway must remain reachable without touching code."""
    _clear_gateway_env(monkeypatch)
    monkeypatch.setenv("MCP_DOMAIN", "cluster")
    monkeypatch.setenv("LLM_GATEWAY_URL", "http://gateway:8080")
    assert MCPConfig.from_env().llm_gateway_url == "http://gateway:8080"
    assert RuntimeEnv.from_env().llm_gateway_url == "http://gateway:8080"


def test_runtime_names_a_default_model(monkeypatch):
    """Under agentgateway the model name IS the routing key, so the runtime must
    always have one to name."""
    _clear_gateway_env(monkeypatch)
    assert RuntimeEnv.from_env().llm_model == DEFAULT_MODELS["anthropic"]
    # `MODEL` is the key the adhar-ai-llm Secret projects.
    monkeypatch.setenv("MODEL", "claude-opus-4-5-20251101")
    assert RuntimeEnv.from_env().llm_model == "claude-opus-4-5-20251101"


def test_mcp_servers_carry_no_oidc_validation_settings_by_default(monkeypatch):
    """ADR-0025 item 6: the manifests stopped setting OIDC_ISSUER_URL /
    OIDC_CLIENT_ID on the MCP Deployments — agentgateway validates the JWT once
    in front of all seven. Nothing in the server may require them."""
    _clear_gateway_env(monkeypatch)
    monkeypatch.delenv("OIDC_ISSUER_URL", raising=False)
    monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)
    monkeypatch.setenv("MCP_DOMAIN", "cluster")
    cfg = MCPConfig.from_env()
    assert cfg.oidc_issuer_url == ""
    assert cfg.oidc_client_id == "adhar-ai"


# ------------------------------------------------------------ build identity --


def test_the_package_version_matches_pyproject():
    """Two places hold the number, so they must be asserted equal.

    A release tags `vX.Y.Z` and the workflow publishes `:X.Y.Z` from the tag,
    while the running code reports `__version__`. If those drift, `/healthz`
    confidently names a version that was never built.
    """
    import tomllib
    from pathlib import Path

    import adhar_ai

    root = Path(__file__).resolve().parents[1]
    declared = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    assert adhar_ai.__version__ == declared


def test_build_info_is_honest_when_nothing_stamped_it(monkeypatch):
    """A local run has no revision. Inventing one would be worse than saying so."""
    from adhar_ai import build_info

    monkeypatch.delenv("ADHAR_AI_REVISION", raising=False)
    monkeypatch.delenv("ADHAR_AI_BUILD_VERSION", raising=False)
    info = build_info()
    assert info["revision"] == "unknown"
    assert info["version"]


def test_build_info_reports_what_ci_stamped(monkeypatch):
    from adhar_ai import build_info

    monkeypatch.setenv("ADHAR_AI_REVISION", "a" * 40)
    monkeypatch.setenv("ADHAR_AI_BUILD_VERSION", "9.9.9")
    assert build_info() == {"version": "9.9.9", "revision": "a" * 40}


def test_the_dockerfile_and_workflow_agree_on_the_build_args():
    """The stamp only works if all three spell it the same way.

    A rename in one place leaves every pod reporting `unknown`, which is the
    silent-failure mode this whole mechanism exists to avoid.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "Dockerfile").read_text()
    workflow = (root / ".github/workflows/images.yml").read_text()

    for arg in ("REVISION", "VERSION"):
        assert f"ARG {arg}" in dockerfile, arg
        assert f"{arg}=${{{{" in workflow, f"the workflow never passes {arg}"
    assert "ADHAR_AI_REVISION=${REVISION}" in dockerfile
    assert "ADHAR_AI_BUILD_VERSION=${VERSION}" in dockerfile
    assert "build-args:" in workflow

    # `git describe` needs the tags, and actions/checkout fetches none by
    # default. Without this every build off `main` reports 0.0.0 — and `main`
    # is what publishes `:latest`, which is what the platform deploys.
    assert "fetch-depth: 0" in workflow
    assert "git describe" in workflow
