# ⚙️ Operations

How to configure, observe and debug Adhar AI — whether it is running as the
platform's `ai/adhar-ai` package or on your laptop.

Everything below is read from the source. The authoritative list of settings is
[`src/adhar_ai/config.py`](../src/adhar_ai/config.py); the ConfigMap schema is
[`src/adhar_ai/runtime/autonomy.py`](../src/adhar_ai/runtime/autonomy.py).

---

## 🚪 The entrypoints

One image, one binary, one subcommand per role. The role comes from the
container `args`, never from the image name.

```
adhar-ai [--log-level LEVEL] <mcp|gateway|runtime|index|tools> [flags]
```

`--log-level` defaults to `$LOG_LEVEL`, else `info`. It sets both Python logging
and uvicorn's log level.

| Command | Flags | Defaults | What it runs |
|---|---|---|---|
| `adhar-ai mcp` | `--domain`, `--listen` | `--domain` unset → `ADHAR_AI_MCP_DOMAIN` / `MCP_DOMAIN` / `cluster`; `--listen=:8080` | One MCP tool server. Streamable HTTP at `/mcp`, plus `/healthz` and `/readyz`. |
| `adhar-ai runtime` | `--config`, `--listen` | `--config` defaults to `$ADHAR_AI_CONFIG`; `--listen=:8080` | The agent runtime: `/chat`, `/operators/{name}/event`, `/findings`, `/config`, `/healthz`. |
| `adhar-ai gateway` | `--listen` | `--listen=:8080` | The bundled OpenAI-compatible LLM gateway. **Local development only** — see below. |
| `adhar-ai index` | `--docs`, `--dsn` | `--docs` → `ADHAR_AI_DOCS_PATH`; `--dsn` → the resolved RAG DSN | Re-indexes the docs tree into pgvector and exits. |
| `adhar-ai tools` | — | — | Prints the tool inventory of all seven domains as JSON and exits. No cluster, no key, no network. |

`--domain` accepts exactly: `cluster`, `gitops`, `provision`, `observability`,
`security`, `cost`, `catalog`. Anything else raises at start-up rather than
serving an empty tool list.

`--listen` is parsed Go-style: `:8080`, `0.0.0.0:8080` and `8080` all work; the
host defaults to `0.0.0.0`.

`adhar-ai index` exits `2` with `no RAG DSN: set ADHAR_AI_RAG_DSN or pass --dsn`
when no DSN resolves. It reads `rag.table` from `$ADHAR_AI_CONFIG`, truncates
the table and re-ingests, so it is idempotent.

### The gateway is not deployed in the platform

[ADR-0025](https://github.com/adhar-io/adhar/blob/main/docs/adr/0025-ai-gateway-agentgateway.md)
retired `adhar-ai-llm-gateway` in favour of upstream
[agentgateway](https://agentgateway.dev). The platform package deploys the
runtime and seven MCP servers and **nothing else**; CI publishes eight image
names and there is deliberately no `adhar-ai-gateway` among them.

`adhar-ai gateway` remains in the CLI as the dependency-free local-dev
fallback, which is what `docker compose` runs. In the cluster, the same wire
contract (`/v1/chat/completions`, `/v1/models`, `/v1/embeddings`) is served by
agentgateway at `https://ai.<host>/v1`, and `LLM_GATEWAY_URL` points there.

---

## 🔧 Configuration reference

`env()` takes the **first non-empty** value among the names listed, in the order
listed, and strips surrounding whitespace. The "Variable" column below is in
precedence order, and that order is not uniform: for the backend URLs (Gitea,
ArgoCD, the telemetry stack, `LLM_GATEWAY_URL`) and for `OIDC_ISSUER_URL` the
**unprefixed** name wins, because that is the name the platform manifests
project; for the LLM, MCP and runtime-auth settings the `ADHAR_AI_`-prefixed
name wins. Several names — `GITEA_BOT_USER`, `GITEA_BOT_TOKEN`,
`GITEA_WRITE_ENABLED`, the `ARGOCD_*` set and every `BUDGET_*` — have no
prefixed alias at all.

Booleans are true for `1`, `true`, `yes`, `on`, `enabled` (case-insensitive);
anything else is false. Lists are comma-separated with blanks dropped. An
unparseable integer silently falls back to the default.

### LLM and gateway

Read by `adhar-ai gateway`. In the platform these live on agentgateway instead.

| Variable | Default | What it does |
|---|---|---|
| `ADHAR_AI_LLM_PROVIDER` → `PROVIDER` → `ADHAR_AI_DEFAULT_PROVIDER` | `anthropic` | Backend selection. Accepts `anthropic` (alias `claude`), `openai`, `azure` (alias `azure-openai`), `openai-compatible` (alias `compatible`), `ollama`. An unknown value raises at start-up. |
| `ADHAR_AI_LLM_API_KEY` → `API_KEY` → `ANTHROPIC_API_KEY` → `OPENAI_API_KEY` | `""` | The provider key. It never leaves the gateway process. Unset means *unkeyed* — reported, not fatal. |
| `ADHAR_AI_LLM_MODEL` → `MODEL` | `claude-sonnet-5` (anthropic), `gpt-4o` (openai), `llama3.1` (ollama), `""` (azure, openai-compatible) | Default model when a request names none. Azure addresses a *deployment*, so it has no safe default. |
| `ADHAR_AI_LLM_ENDPOINT` → `ENDPOINT` | `""` | Base URL override. Falls back to `https://api.openai.com/v1` for OpenAI-compatible and `http://ollama.adhar-system.svc.cluster.local:11434` for Ollama. |
| `ADHAR_AI_RESPONSE_CACHE` | `enabled` | The response cache is on unless this is exactly `disabled` (case-insensitive). |
| `LLM_GATEWAY_URL` → `ADHAR_AI_LLM_GATEWAY_URL` | `http://adhar-ai-gateway.adhar-system.svc.cluster.local:8080/v1` | Where the runtime and the MCP servers send completions and embeddings. With or without the `/v1` suffix — it is normalized to exactly one. |

The default is deliberately the in-cluster data-plane address, so it is
unresolvable off-cluster. `docker compose` and every local entrypoint override
it (`LLM_GATEWAY_URL=http://gateway:8080`).

### Budgets and rate limits

Enforced by `adhar-ai gateway`, per tenant, in process. These names have **no**
`ADHAR_AI_` alias.

| Variable | Default | What it does |
|---|---|---|
| `BUDGET_PER_USER_DAILY_TOKENS` | `2000000` | Daily token ceiling per tenant. Exceeding it → `429 {"budget": "per_user_daily_tokens"}`. |
| `BUDGET_PER_OP_MAX_TOKENS` | `400000` | Rejects a request whose `max_tokens` is larger, before any provider call → `429 {"budget": "per_op_max_tokens"}`. |
| `BUDGET_PER_OP_MAX_TOOL_CALLS` | `40` | Reported by `/healthz`. The tool-call cap actually applied to an agent run comes from the ConfigMap's `limits.maxToolCallsPerOp`. |
| `BUDGET_MAX_CONCURRENT_SESSIONS` | `8` | Caps in-flight upstream calls with a semaphore. Extra requests **queue**, they are not refused. |
| `RATE_LIMIT_REQUESTS_PER_MINUTE` | `60` | Sliding 60-second window per tenant → `429 {"budget": "rate_limit"}`. |

The tenant is `body.tenant`, else the `X-Adhar-Tenant` header, else
`anonymous`. The runtime sets that header to the session tenant (the
authenticated subject, or `operator:<name>` for an operator run).

### Gitea — the write path

| Variable | Default | What it does |
|---|---|---|
| `GITEA_API_URL` → `ADHAR_AI_GITEA_API_URL` | `""` | Base service URL. `/api/v1` is appended by the client; trailing slashes are stripped. Unset → every Gitea-backed tool reports `BackendNotConfigured`. |
| `GITEA_ORG` → `ADHAR_AI_GITEA_ORG` | `adhar` | Organisation that owns the GitOps repos. |
| `GITEA_BOT_USER` | `adhar-ai-bot` | Commit author. |
| `GITEA_BOT_TOKEN` | `""` | The bot's token. Without it a write tool refuses before touching the network. |
| `GITEA_WRITE_ENABLED` | `false` | Whether this MCP server carries a write tool at all. The manifests set it `true` only for `gitops`, `provision`, `security` and `catalog`. |
| `GITEA_WRITE_REPOS` → `ADHAR_AI_GITEA_WRITE_REPOS` | `""` → falls back to `packages,environments` | Repos the bot may open a PR against. |

### ArgoCD

| Variable | Default | What it does |
|---|---|---|
| `ARGOCD_URL` → `ADHAR_AI_ARGOCD_URL` | `""` | ArgoCD API base. Unset → the gitops tools fall back to reading Application CRs through the Kubernetes API. |
| `ARGOCD_USERNAME` | `admin` | Used with the password login. |
| `ARGOCD_PASSWORD` → `ARGOCD_ADMIN_PASSWORD` | `""` | The second name is the raw key in the platform's `argocd-credentials` secret, so a bare `envFrom` works. |
| `ARGOCD_AUTH_TOKEN` → `ARGOCD_TOKEN` | `""` | Preferred over username/password. |
| `ARGOCD_VERIFY_TLS` | `false` | Note the default: TLS verification is **off**, matching the platform's self-signed in-cluster ArgoCD. |

ArgoCD counts as configured only when a URL *and* a token or password are set.

### Telemetry backends

All default to `""`; trailing slashes are stripped. An unset backend makes its
tools answer "not configured" — they never fabricate a result.

| Variable | Default | What it does |
|---|---|---|
| `PROMETHEUS_URL` → `ADHAR_AI_PROMETHEUS_URL` | `""` | `promql`, `slo_burn`, `correlate`. |
| `LOKI_URL` → `ADHAR_AI_LOKI_URL` | `""` | `logql`. |
| `TEMPO_URL` → `ADHAR_AI_TEMPO_URL` | `""` | `traceql`. |
| `OPENCOST_URL` → `ADHAR_AI_OPENCOST_URL` | `""` | `cost_by`, `budget_status`, `showback`, and the daily cost poller. |

### MCP servers

| Variable | Default | What it does |
|---|---|---|
| `ADHAR_AI_MCP_DOMAIN` → `MCP_DOMAIN` | `cluster` | Which domain this process serves. `--domain` overrides it. An unknown domain raises `ValueError` at start-up. |
| `ADHAR_AI_MCP_ALLOWED_HOSTS` → `MCP_ALLOWED_HOSTS` | `*` | Host-header allow-list for the transport's DNS-rebinding guard. `*` turns the guard **off**, which is the default and what makes in-cluster federation work. Set a comma-separated list to switch it back on. |
| `OIDC_ISSUER_URL` | `""` | Kept for the token-exchange path and standalone local runs. The MCP servers do **not** validate tokens: agentgateway does that once in front of all seven (ADR-0025), so the manifests leave this unset. |
| `OIDC_CLIENT_ID` | `adhar-ai` | Client id for the same path. |

When `ADHAR_AI_MCP_ALLOWED_HOSTS` is a real list, the guard is enabled and the
allowed *origins* are derived from it as `http://<host>` and `https://<host>`.

### Runtime authentication

The runtime is the one surface that is not behind agentgateway, so it
authenticates for itself.

| Variable | Default | What it does |
|---|---|---|
| `OIDC_ISSUER_URL` → `ADHAR_AI_OIDC_ISSUER_URL` | `""` | Public Keycloak realm URL. Must equal the token's `iss` claim byte-for-byte. Unset → OIDC is disabled and every caller is anonymous. |
| `ADHAR_AI_OIDC_JWKS_URL` → `OIDC_JWKS_URL` | `<issuer>/protocol/openid-connect/certs` when an issuer is set, else `""` | Where signing keys are fetched. Point it at the in-cluster Keycloak Service so key retrieval does not depend on the platform's own ingress. Keys are cached and refetched on an unknown `kid`, so a rotation needs no restart. |
| `ADHAR_AI_OIDC_AUDIENCE` → `OIDC_AUDIENCE` | `""` | `aud` is verified **only** when this is set — Keycloak puts the client id in `azp` and emits `aud` only when a client scope adds one. |
| `ADHAR_AI_WEBHOOK_TOKEN` → `WEBHOOK_TOKEN` | `""` | Shared bearer for machine callers (Alertmanager, ArgoCD notifications). Accepted on `/operators/{name}/event` only, compared with `hmac.compare_digest`. A matching token is a write-capable principal. |
| `ADHAR_AI_WRITE_GROUPS` → `WRITE_GROUPS` | `platform-admin` | Keycloak groups whose members may drive a write. Group paths are compared on the leaf, so `/platform-admin` matches. |
| `ADHAR_AI_REQUIRE_AUTH` → `REQUIRE_AUTH` | `false` | `false`: an unauthenticated caller is answered but pinned to `read-only`. `true`: the request is refused with `401` and a `WWW-Authenticate: Bearer` header. |
| `OIDC_CLIENT_ID` | `adhar-ai` | Client id recorded in the runtime environment. |

Accepted algorithms are `RS256`, `RS512` and `ES256`. The subject is
`preferred_username`, else `sub`, else `unknown`.

### Runtime, RAG and pollers

| Variable | Default | What it does |
|---|---|---|
| `ADHAR_AI_CONFIG` | unset | Path to `config.yaml`. `--config` sets it into the environment for the process. A missing file falls back to the shipped defaults rather than failing. |
| `ADHAR_AI_DOCS_PATH` | `/etc/adhar-ai/docs` | Docs tree for both retrieval paths. A missing path disables ingestion; it does not crash the runtime. |
| `ADHAR_AI_RAG_DSN` → `RAG_DSN` → `DATABASE_URL` | composed from `RAG_DB_*` (below) | pgvector DSN. Also the findings store's database. |
| `RAG_DB_HOST` → `host` | `""` | If no DSN was given and no host is set, the DSN is empty and RAG runs lexical-only. |
| `RAG_DB_NAME` → `dbname` | `adhar_ai_rag` | |
| `RAG_DB_PORT` → `port` | `5432` | |
| `RAG_DB_USER` → `username` → `user` → `PGUSER` | `adhar_ai` | |
| `RAG_DB_PASSWORD` → `password` → `PGPASSWORD` | `""` | Omitted from the composed DSN when empty. |
| `ADHAR_AI_DRIFT_POLL_SECONDS` | `300` | ArgoCD OutOfSync poll interval. The poller waits 15 s after start-up before its first run. |
| `ADHAR_AI_COST_POLL_SECONDS` | `86400` | OpenCost review interval. First run 60 s after start-up. |
| `ADHAR_AI_LLM_MODEL` → `MODEL` | `claude-sonnet-5` | The model the loop **names** when a request does not choose one. Under agentgateway the model name is the routing key, so a body without one lands on the fallback rule. |

The lowercase fallbacks (`host`, `dbname`, `port`, `username`, `password`) are
the keys the CNPG-issued `adhar-ai-rag-app` secret projects through `envFrom`.

### Miscellaneous

| Variable | Default | What it does |
|---|---|---|
| `LOG_LEVEL` | `info` | Default for `--log-level`. |
| `ADHAR_AI_KUBE_DISABLED` | unset | Exactly `1` makes every Kubernetes-backed tool raise `BackendNotConfigured` immediately instead of hanging on the in-cluster API address. `docker compose` sets it. |

---

## 📄 The `adhar-ai-config` ConfigMap

Mounted at `/etc/adhar-ai/config.yaml` and passed with `--config`. Only the keys
below are parsed; anything else in the file is ignored (the RAG and findings
databases, for instance, come from the DSN, never from this file).

| Key | Default | Notes |
|---|---|---|
| `autonomy.default` | `suggest` | One of `read-only`, `suggest`, `approve-to-apply`, `scoped`. A typo raises at load. |
| `limits.maxSteps` | `12` | Maximum plan–act–observe iterations per run. |
| `limits.maxToolCallsPerOp` | `40` | Maximum tool calls per run. |
| `writePolicy.allowedRepos` | `[packages, environments]` | An empty or absent list means the default, not "nothing". |
| `writePolicy.allowedPathPrefixes` | `["packages/", "environments/"]` | Same: empty means the default. |
| `writePolicy.scoped.allowedRepos` | `[]` | **Empty by default.** |
| `writePolicy.scoped.allowedPathPrefixes` | `[]` | **Empty by default.** |
| `operators.<name>.trigger` | `manual` | Free text; documentation of what fires it. |
| `operators.<name>.autonomy` | `autonomy.default` | Validated at load. Combined with the global default by taking the lower rung. |
| `operators.<name>.allowedTools` | `[]` | Empty means the operator's own built-in default list. |
| `mcpServers.<domain>` | in-cluster Service URLs for all seven domains | Your entries are **merged over** the defaults, so omitting a domain does not remove it. Give the base URL; `/mcp` is appended. |
| `rag.enabled` | `true` | `false` skips the whole grounding bootstrap and `/healthz` reports `disabled`. |
| `rag.table` | `kb_chunk` | |
| `findings.table` | `finding` | Must be alphanumeric plus underscores — it is interpolated into SQL, and anything else raises at start-up. |

A complete, valid file:

```yaml
autonomy:
  default: suggest

limits:
  maxSteps: 12
  maxToolCallsPerOp: 40

writePolicy:
  allowedRepos: [packages, environments]
  allowedPathPrefixes:
    - "packages/"
    - "environments/"
  # The narrower list `scoped` autonomy runs under. Empty by default.
  scoped:
    allowedRepos: [packages]
    allowedPathPrefixes:
      - "packages/observability/"

operators:
  alert-triage:
    trigger: alertmanager
    autonomy: suggest
    allowedTools: [promql, logql, app_status, correlate, propose_change]
  drift-explain:
    trigger: argocd-notifications
    autonomy: read-only
    allowedTools: [app_status, app_diff, sync_status]
  cost-advisor:
    trigger: cron
    autonomy: suggest
    allowedTools: [cost_by, budget_status, showback, propose_change]
  upgrade-preflight:
    trigger: manual
    autonomy: suggest
    allowedTools: [resource_health, app_status, findings, propose_change]

mcpServers:
  cluster:       http://adhar-ai-mcp-cluster.adhar-system.svc.cluster.local:8080
  gitops:        http://adhar-ai-mcp-gitops.adhar-system.svc.cluster.local:8080
  provision:     http://adhar-ai-mcp-provision.adhar-system.svc.cluster.local:8080
  observability: http://adhar-ai-mcp-observability.adhar-system.svc.cluster.local:8080
  security:      http://adhar-ai-mcp-security.adhar-system.svc.cluster.local:8080
  cost:          http://adhar-ai-mcp-cost.adhar-system.svc.cluster.local:8080
  catalog:       http://adhar-ai-mcp-catalog.adhar-system.svc.cluster.local:8080

rag:
  enabled: true
  table: kb_chunk

findings:
  table: finding
```

### `scoped` permits nothing until you enumerate it

`writePolicy.scoped` is empty in the shipped ConfigMap. `scoped` is the rung
that runs **unattended**, so it does not inherit the general write policy —
raising the stage without naming a scope fails closed:

```
denied by writePolicy: autonomy `scoped` has no configured scope on this
runtime, so it permits no write at all; set writePolicy.scoped in adhar-ai-config
```

Every other stage uses `allowedRepos` / `allowedPathPrefixes`.

### Where each half of the write policy is enforced

The policy is checked twice, and the two checks do not see the same string.

- **The runtime** checks the ConfigMap's `allowedPathPrefixes` against the
  **bare** `path` argument of the write tool call, and refuses with
  `denied by writePolicy: …`.
- **The MCP server** that holds the Gitea token checks the **repo-qualified**
  path (`<repo>/<path>`) against a fixed `("packages/", "environments/")`, and
  the repo against `GITEA_WRITE_REPOS`. Editing `allowedPathPrefixes` in the
  ConfigMap does not change that second check.

So write prefixes the way the tool spells its `path` arguments, and keep the
two consistent — otherwise a change passes one gate and is refused by the other.

---

## 🩺 The health surface

### Runtime — `GET /healthz`

```json
{
  "status": "ok",
  "autonomy_default": "suggest",
  "operators": ["alert-triage", "cost-advisor", "drift-explain", "upgrade-preflight"],
  "mcp_servers_connected": ["catalog", "cluster", "cost", "gitops", "observability", "provision", "security"],
  "mcp_servers_unreachable": {},
  "tools": ["app_diff", "app_status", "budget_status", "correlate", "cost_by", "describe", "findings", "get_events", "list_pods", "list_xrs", "logql", "logs", "policy_explain", "posture", "promql", "propose_change", "propose_exception", "propose_xr", "resource_health", "scaffold", "search_packages", "showback", "slo_burn", "sync_status", "template_params", "traceql", "xr_status"],
  "rag": "vector (pgvector) with lexical fallback over 1051 chunks — 1051 chunks indexed from /etc/adhar-ai/docs with gateway embeddings",
  "auth": {
    "oidc": "https://keycloak.adhar.example/realms/adhar",
    "webhookToken": "configured",
    "requireAuth": false,
    "writeGroups": ["platform-admin"],
    "unauthenticated": "answered as read-only"
  },
  "findings_held": 12,
  "findings_store": "ready (finding)",
  "adhar.io/origin": "adhar-ai"
}
```

| Field | How to read it |
|---|---|
| `operators` | The operator names in the ConfigMap, sorted. This is what the ConfigMap declares, not what the code registers — a name here that is not in the registry will 404 on its webhook. |
| `mcp_servers_connected` | Derived from the **tools actually listed**, so a domain appears only if its session initialized *and* it returned at least one tool. |
| `mcp_servers_unreachable` | `{domain: "ExceptionType: message"}` for every server whose connect or `list_tools` failed. Populated once, at start-up — connections are opened for the process lifetime, so this does not re-probe. |
| `tools` | The flat tool namespace the model sees. Names are global across domains. |
| `rag` | See the mode strings below. |
| `auth` | `oidc` is the issuer or `"disabled"`; `webhookToken` is `configured`/`unset`. Never the token itself. |
| `findings_held` | Size of the in-memory ring buffer (capped at 200). |
| `findings_store` | `"disabled (no database)"`, `"pending"`, `"ready (<table>)"`, or `"unavailable: <Type>: <message>"`. |

RAG mode strings, in full:

| String | Meaning |
|---|---|
| `disabled` | `rag.enabled: false`, or the bootstrap has not started yet. |
| `lexical only (N chunks) — no embeddings or database configured` | BM25 over the docs tree. No DSN, or no embedding backend. |
| `vector (pgvector) with lexical fallback over N chunks` | Both paths live. |
| `… — N chunks indexed from <path> with <gateway\|local> embeddings` | Suffix appended after a successful ingest. |
| `… — pgvector unavailable (<Type>: <message>)` | Suffix appended when the ingest failed. The lexical retriever published earlier stays in place, so this is a downgrade, not a loss. |
| `unavailable (no database, no embeddings, no docs)` | Nothing to retrieve from — check `ADHAR_AI_DOCS_PATH`. |

The bootstrap runs as a background task, so a slow or missing database never
blocks readiness: `/healthz` answers `ok` throughout and the `rag` field is
where the truth lives.

### Runtime — `GET /config`

The effective policy, read back from the loaded ConfigMap:

```json
{
  "autonomy": {"default": "suggest"},
  "limits": {"maxSteps": 12, "maxToolCallsPerOp": 40},
  "writePolicy": {
    "allowedRepos": ["packages", "environments"],
    "allowedPathPrefixes": ["packages/", "environments/"]
  },
  "auth": {
    "oidc": "disabled",
    "webhookToken": "unset",
    "requireAuth": false,
    "writeGroups": ["platform-admin"],
    "unauthenticated": "answered as read-only"
  },
  "operators": {
    "drift-explain": {
      "trigger": "argocd-notifications",
      "autonomy": "read-only",
      "allowedTools": ["app_status", "app_diff", "sync_status"]
    }
  },
  "mcpServers": {"cluster": "http://adhar-ai-mcp-cluster.adhar-system.svc.cluster.local:8080"}
}
```

This is the fastest check that a ConfigMap edit actually reached the pod: the
runtime reads the file once, at start-up. Edit the ConfigMap, then restart the
Deployment.

Note that `/config` does **not** echo `writePolicy.scoped`. To confirm a scoped
allow-list is in force, read the mounted file directly:

```bash
kubectl -n adhar-system exec deploy/adhar-ai-runtime -- cat /etc/adhar-ai/config.yaml
```

### Runtime — `GET /findings`

```bash
curl -s 'localhost:8080/findings?limit=10&operator=drift-explain' | jq
```

```json
{
  "count": 3,
  "findings": [
    {
      "id": "drift-explain-1a2b3c4d",
      "operator": "drift-explain",
      "title": "drift: adhar-console",
      "severity": "warning",
      "summary": "adhar-console is OutOfSync because the live Deployment has 3 replicas …",
      "autonomy": "read-only",
      "subject": {"app": "adhar-console", "detected": null},
      "evidence": [{"tool": "app_status", "args": {"name": "adhar-console"}, "decision": "ok"}],
      "citations": [{"source": "app_status", "kind": "tool", "detail": "{'name': 'adhar-console'}"}],
      "recommendation": "adhar-console is OutOfSync because the live Deployment has 3 replicas …",
      "pull_request": null,
      "created_at": 1757740000.0,
      "origin": "adhar-ai"
    }
  ]
}
```

`count` is the number of findings **matching the filter**, before `limit` is
applied — so `count > len(findings)` simply means there are more pages' worth.
`pull_request: null` means no change was proposed, either because the stage
forbids writes or because none was needed. `limit` defaults to `50`.

On start-up the runtime reloads up to 200 findings from the database before
serving, so a rollout does not look to an on-call engineer like nothing
happened.

### Gateway — `GET /healthz`

```json
{
  "status": "ok",
  "provider": "anthropic",
  "model": "claude-sonnet-5",
  "keyed": true,
  "budgets": {
    "per_user_daily_tokens": 2000000,
    "per_op_max_tokens": 400000,
    "per_op_max_tool_calls": 40,
    "rate_limit_requests_per_minute": 60
  },
  "adhar.io/origin": "adhar-ai"
}
```

`keyed` is `true` when the provider is `ollama` or an API key is set. Unkeyed is
a *reported* state, not a crash: `/healthz` stays `ok` and only the routes that
need a provider answer `503`.

### Gateway — the `/v1` surface

| Route | Returns |
|---|---|
| `GET /v1/models` | `{"object": "list", "data": [{"id": "…", "object": "model", "created": 0, "owned_by": "<provider>"}]}` — whatever the configured backend reports. `503` when unkeyed. |
| `GET /v1/budget` | The caller's ledger. Pass `X-Adhar-Tenant`, or you read the `anonymous` tenant. |
| `GET /v1/cache` | Response-cache statistics. |
| `POST /v1/chat/completions` | OpenAI-compatible, tool-calling aware. `stream: true` is refused with `400`. |
| `POST /v1/embeddings` | Used by the RAG indexer. `501` when the provider has no embeddings endpoint (Anthropic). |

```json
// GET /v1/budget  -H 'X-Adhar-Tenant: alice'
{
  "tokens_today": 18432,
  "daily_token_budget": 2000000,
  "requests_last_minute": 3,
  "rate_limit_per_minute": 60
}
```

```json
// GET /v1/cache
{
  "enabled": true,
  "entries": 17,
  "hits": 42,
  "misses": 310,
  "ttl_seconds": 300,
  "cacheable": "temperature=0, non-streaming, per tenant"
}
```

The cache holds at most 256 entries for 300 seconds, keyed on the tenant plus
the entire request (model, messages, tools, tool choice, `max_tokens`,
temperature). It is consulted **after** the budget check, so a cached answer
cannot be used to evade a rate limit, and it is per tenant, so it can never
disclose one caller's prompt to another. Only `temperature == 0` is cached —
`null` means "provider default", which is not necessarily zero, and is not
cached.

### MCP servers — `GET /healthz` and `GET /readyz`

Both served on the same port as `/mcp`.

```json
// GET /healthz
{
  "status": "ok",
  "domain": "gitops",
  "server": "adhar-gitops",
  "write_enabled": true,
  "write_path": "gitea-pull-request-only"
}
```

```json
// GET /readyz
{"status": "ok", "domain": "gitops"}
```

`write_enabled` reflects `GITEA_WRITE_ENABLED` only. It says the server *carries*
a write tool; it does not say the tool would succeed — that also needs
`GITEA_BOT_TOKEN` and a repo/path inside the allow-list.

---

## 🔭 Observability

### The audit stream

Every tool call emits exactly one JSON line on **stdout**, flushed. Alloy
scrapes container stdout into Loki, so the platform's "Adhar AI" dashboard gets
the audit trail without this process ever holding a Loki write credential.

Every record carries `ts` and `adhar.io/origin: adhar-ai`. A successful tool
call:

```json
{"ts": 1757740000.123, "adhar.io/origin": "adhar-ai", "audit_id": "aud-3f9c1b7e5a2d4c80", "tool": "app_status", "access": "read", "domain": "gitops", "args": {"name": "adhar-console"}, "decision": "ok", "duration_ms": 84.2}
```

A failed one adds `error` and keeps `decision: "error"` — a denied or errored
call is exactly the event an operator most wants to see:

```json
{"ts": 1757740001.004, "adhar.io/origin": "adhar-ai", "audit_id": "aud-…", "tool": "promql", "access": "read", "domain": "observability", "args": {"query": "up"}, "decision": "error", "error": "BackendNotConfigured: Prometheus is not configured", "duration_ms": 3.1}
```

| Field | Meaning |
|---|---|
| `ts` | Wall-clock seconds. |
| `audit_id` | `aud-<16 hex>`, minted per call. Each layer mints its own — the agent run, the tool call and the pull request each carry a different one. |
| `tool` | The Python function name of the tool. |
| `access` | `read` or `write`, from the decorator that registered the tool. |
| `domain` | Which of the seven servers emitted it. |
| `args` | The keyword arguments, redacted (below). |
| `decision` | `ok`, `error`, or `denied`. |
| `duration_ms` | Rounded to 0.1 ms. |
| `error` | `"<ExceptionType>: <message>"`, on failures only. |

The runtime emits a second shape, one per agent run:

```json
{"ts": 1757740010.5, "adhar.io/origin": "adhar-ai", "audit_id": "aud-…", "component": "runtime", "intent": "which applications are out of sync, and why?", "user": "alice", "tenant": "alice", "autonomy": "suggest", "model": "claude-sonnet-5", "steps": 3, "tool_calls": ["sync_status", "app_diff"], "decision": "proposed", "pull_requests": ["https://gitea.example/adhar/packages/pulls/41"], "duration_ms": 8421.0}
```

`intent` is the prompt truncated to 200 characters. `decision` is the run's
outcome: `answer`, `proposed`, `budget_exhausted` or `error`.

Opening a pull request emits a third shape, from the MCP server that holds the
Gitea token. Its `audit_id` is the one written into the PR body's provenance
table and the commit trailer, so a PR can be traced back to this line:

```json
{"ts": 1757740003.9, "adhar.io/origin": "adhar-ai", "audit_id": "aud-…", "tool": "propose_change", "access": "write", "action": "open_pr", "repo": "packages", "pr": 41, "url": "https://gitea.example/adhar/packages/pulls/41", "files": ["packages/observability/values.yaml"], "user": "alice", "model": null, "decision": "proposed"}
```

A refused write emits its own record before anything is called:

```json
{"ts": 1757740002.7, "adhar.io/origin": "adhar-ai", "audit_id": "aud-…", "tool": "propose_change", "access": "write", "decision": "denied", "reason": "repo 'infra' is outside this stage's allow-list ['packages', 'environments']", "autonomy": "suggest", "args": {"repo": "infra"}}
```

### Redaction

`redact()` runs over every field before it is printed:

- A dict key whose lowercased, `-`-to-`_` form is one of `token`, `password`,
  `apikey`, `api_key`, `secret`, `authorization`, `credential` has its value
  replaced with `"***"`. So `Authorization`, `api-key` and `API_KEY` are all
  caught.
- Lists are truncated to their first 20 elements.
- Strings longer than 200 characters are truncated with a trailing `…`.
- Recursion deeper than 6 levels becomes `"…"`.

This is why the audit stream can be scraped wholesale: a credential-shaped
value cannot reach it, and nor can a multi-megabyte log blob.

Application logging (separate from the audit stream) goes to **stderr** in the
format `%(asctime)s %(levelname)s %(name)s %(message)s`, under the loggers
`adhar_ai`, `adhar_ai.runtime`, `adhar_ai.runtime.auth`, `adhar_ai.toolbox`,
`adhar_ai.loop`, `adhar_ai.rag`, `adhar_ai.store`, `adhar_ai.gateway` and
`adhar_ai.audit`.

---

## 🔍 Troubleshooting

| Symptom | Likely cause | What to check |
|---|---|---|
| MCP server answers `421 Invalid Host header` | The transport's DNS-rebinding guard is on and the caller's `Host` is not in the allow-list. agentgateway reaches these servers by Service `backendRef`, so the Host is a Service DNS name or a Pod IP — a set you cannot enumerate. | `ADHAR_AI_MCP_ALLOWED_HOSTS` defaults to `*`, which turns the guard off and is what prevents this. If something set it to a list, unset it or add every reachable name. Reproduce with `curl -H 'Host: adhar-ai-mcp-gitops.adhar-system.svc.cluster.local:8080' http://<pod-ip>:8080/healthz`. |
| A domain appears in `mcp_servers_unreachable` | Connect or `tools/list` failed at runtime start-up. | Read the message — it is `"<ExceptionType>: <message>"`. Verify the `mcpServers.<domain>` URL is a **base** URL (`/mcp` is appended for you), that the Service resolves, and that the MCP pod's `/readyz` answers. Fix the cause and **restart the runtime**: sessions are opened once, at start-up, and never re-probed. |
| Tools from one domain missing from `/healthz` `tools` | Same cause as above, or that server listed zero tools. | `mcp_servers_connected` is derived from listed tools, so a domain missing from both lists connected but returned nothing. |
| Gateway returns `503 "Adhar AI is unkeyed: provider=… has no API key"` | No provider key. Every route that needs a provider refuses; `/healthz` still answers `ok` with `"keyed": false`. | Set `ADHAR_AI_LLM_API_KEY` / `API_KEY` / `ANTHROPIC_API_KEY`, or switch to `ADHAR_AI_LLM_PROVIDER=ollama`, which needs no key. In the platform the key lives in `secret/adhar-ai/llm` in Vault (`PROVIDER` + `API_KEY`). |
| `429` with `{"budget": "per_op_max_tokens"}` | The request's `max_tokens` exceeds `BUDGET_PER_OP_MAX_TOKENS` (`400000`). Refused before any provider call. | Lower `max_tokens` or raise the budget. |
| `429` with `{"budget": "rate_limit"}` | More than `RATE_LIMIT_REQUESTS_PER_MINUTE` (`60`) requests in 60 s **for this tenant**. | `GET /v1/budget -H 'X-Adhar-Tenant: <tenant>'`. Remember an operator run's tenant is `operator:<name>`, and an anonymous caller's is `anonymous` — one noisy anonymous caller starves the others. |
| `429` with `{"budget": "per_user_daily_tokens"}` | Daily ceiling (`2000000`) reached. Resets at UTC midnight. | Same endpoint: `tokens_today` vs `daily_token_budget`. |
| An agent run returns `{"kind": "budget_exhausted"}` | Either the gateway answered `429`, or the run hit `limits.maxToolCallsPerOp`. | `error` says which: a tool-call cap reads `per-operation tool-call cap (40) reached`. Otherwise it is the gateway's 429 body. |
| `/healthz` `rag` says `lexical only (N chunks) — no embeddings or database configured` | No DSN, or no embedding backend. Grounding still works — BM25 over the docs tree — but it is a downgrade. | Check `ADHAR_AI_RAG_DSN` (or the `RAG_DB_*` set), that `psycopg` is installed (`uv sync --extra rag`), and that the gateway can embed. Anthropic exposes no embeddings endpoint, so the gateway answers `501` and the loader falls back to local embeddings, which need `uv sync --extra local-embeddings`. |
| `rag` ends with `— pgvector unavailable (…)` | Ingest failed after the lexical index was published. | The parenthesised exception is the reason: unreachable database, missing `vector` extension, wrong credentials. The runtime keeps serving lexical grounding meanwhile. |
| `rag` says `unavailable (no database, no embeddings, no docs)` | The docs tree is empty or not mounted. | `ADHAR_AI_DOCS_PATH` (default `/etc/adhar-ai/docs`); the indexer walks `*.md` recursively. |
| Findings disappear on every restart | No DSN, so the store is a no-op and the in-memory deque (200 entries) is the whole story. | `/healthz` `findings_store` reads `disabled (no database)`. Set `ADHAR_AI_RAG_DSN` — the findings table lives on the same CNPG database as the RAG index. `unavailable: …` instead means a DSN is set and the connection failed. |
| Findings older than a month vanish | Deliberate: rows older than 30 days are pruned at every start-up. | Not configurable by environment variable. |
| Write tool refused: `this MCP server is read-only (GITEA_WRITE_ENABLED=false)` | The domain is not one of the four write domains, or the flag is unset on its Deployment. | `GET /healthz` on that MCP server shows `write_enabled`. Write domains are `gitops`, `provision`, `security`, `catalog`. |
| Write tool refused: `no Gitea bot token configured` | `GITEA_BOT_TOKEN` is empty. Refused before any network call. | Check the `adhar-ai-bot` secret is projected into the MCP pod. |
| Write tool refused: `repo '…' is not in the allowed set […]` | `GITEA_WRITE_REPOS` (or its default `packages,environments`) does not contain the repo. | Set `GITEA_WRITE_REPOS` on the MCP Deployment **and** `writePolicy.allowedRepos` in the ConfigMap — the two gates are separate. |
| Write tool refused: `path '…' in repo '…' is outside the allowed prefixes` | The MCP server's repo-qualified check. It compares `<repo>/<path>` against a fixed `("packages/", "environments/")`. | Not settable by environment variable. Spell the path so the repo-qualified form falls under one of those two roots. |
| Refused earlier, with `denied by writePolicy: …` | The runtime's own check, using `writePolicy` from the ConfigMap. It compares the **bare** path against `allowedPathPrefixes`. | Edit `adhar-ai-config`, then restart the runtime. `GET /config` reads back what is actually in force. |
| Refused with `autonomy 'scoped' has no configured scope on this runtime` | The stage was raised to `scoped` without filling `writePolicy.scoped`. | Enumerate `writePolicy.scoped.allowedRepos` and `.allowedPathPrefixes`. Empty means "permit nothing", by design. |
| Refused with `denied: this session's autonomy level is read-only` | The effective stage is `read-only`: the ConfigMap says so, the operator policy says so, the request asked for it — or the caller is unauthenticated or outside `ADHAR_AI_WRITE_GROUPS`. | The `/chat` response echoes `autonomy` and a `principal` block with `authenticated` and `write_allowed`. Authority only narrows: a request may ask for a *lower* stage, never a higher one. |
| `principal.write_allowed` is `false` for an authenticated user | The token verified, but no group matched `ADHAR_AI_WRITE_GROUPS` (default `platform-admin`). | Decode the token's `groups` claim. Keycloak emits paths (`/platform-admin`); only the leaf is compared. |
| Every caller is `anonymous` even with a valid token | OIDC is off (no `OIDC_ISSUER_URL`, or no JWKS URL), or verification failed. | `/healthz` `auth.oidc` shows `disabled` when it is off. A failed verification logs `rejected bearer token: <Type>: <message>` on `adhar_ai.runtime.auth` — usually an `iss` mismatch (the issuer must equal the claim byte-for-byte) or an `aud` check you enabled with `ADHAR_AI_OIDC_AUDIENCE`. |
| Runtime answers `401 "authentication required"` | `ADHAR_AI_REQUIRE_AUTH` is true and the request carried no acceptable credential. | The response's `accepts` list names what this runtime would take; `nothing — no credential is configured on this runtime` means `require_auth` is on but neither OIDC nor a webhook token is configured. |
| `400 "streaming is not implemented by the Adhar AI gateway; set stream=false"` | A client sent `stream: true`. The bundled gateway is non-streaming by design. | Set `stream: false`. In the platform, stream against agentgateway at `https://ai.<host>/v1` instead; the bundled gateway is local-dev only. |
| `404` from `POST /operators/{name}/event` | The operator name is not registered. | The response carries `available`. Only four exist: `alert-triage`, `drift-explain`, `cost-advisor`, `upgrade-preflight`. A name in the ConfigMap that is not one of these is configuration only — no route appears for it. |
| A tool says a backend is "not configured" | Its URL env is unset. Tools report this rather than fabricating data. | Check the relevant `*_URL` from the tables above. For Kubernetes-backed tools, `ADHAR_AI_KUBE_DISABLED=1` produces `BackendNotConfigured: Kubernetes … disabled via ADHAR_AI_KUBE_DISABLED=1`. |
| Runtime logs `no credential configured … pinned to read-only` at start-up | Neither `OIDC_ISSUER_URL`, `ADHAR_AI_WEBHOOK_TOKEN` nor `ADHAR_AI_REQUIRE_AUTH` is set. Callers are answered but nothing can open a PR. | Expected for `docker compose`. In a cluster, wire Keycloak and the webhook token. |
| The drift or cost poller never produces a finding | Pollers swallow their exceptions at `DEBUG`. | Run with `--log-level debug` and look for `drift poll skipped: …` / `cost poll skipped: …`. The drift poller fires only on applications that are newly OutOfSync **since its previous pass** — the first pass after start-up reports everything currently drifting, and an app that stays drifting is not re-reported until it recovers and drifts again. |

---

## 💻 Local development

```bash
uv sync --extra rag                  # Python 3.12+
uv run adhar-ai tools                # tool inventory — no cluster, no key
```

Quality gates, all of which CI runs:

```bash
uv run pytest -q                                       # test suite
uv run ruff check src tests                            # lint
uv run mypy src                                        # types
uv run adhar-ai tools | diff -u contract/tools.json -  # the tool contract
```

The last one is the contract with the Go CLI: any drift between the registered
tools and `contract/tools.json` fails the build.

### The whole stack in containers

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # or leave unset for read-only mode
docker compose up --build
```

This brings up the gateway, all seven MCP servers, the runtime and a
pgvector Postgres:

| Service | Host port | Check |
|---|---|---|
| runtime | 8080 | `curl localhost:8080/healthz` |
| gateway | 8081 | `curl localhost:8081/healthz` |
| mcp-cluster | 8090 | `curl localhost:8090/healthz` |
| mcp-gitops | 8091 | |
| mcp-provision | 8092 | |
| mcp-observability | 8093 | |
| mcp-security | 8094 | |
| mcp-cost | 8095 | |
| mcp-catalog | 8096 | |
| rag-db (pgvector/pg16) | 5432 | `pg_isready -U adhar_ai -d adhar_ai_rag` |

Compose mounts `deploy/config.yaml` at `/etc/adhar-ai/config.yaml` — the same
ConfigMap content the Deployment mounts — and `${ADHAR_DOCS_PATH:-../../adhar/docs}`
at `/etc/adhar-ai/docs`, so grounding behaves as it does in the cluster. It sets
`ADHAR_AI_KUBE_DISABLED=1` by default, since no cluster is reachable from the
container; point `GITEA_API_URL`, `ARGOCD_URL` and the telemetry URLs at
port-forwarded services in a `.env` file to exercise the real backends.

### The same stack without containers

Every process defaults to `:8080`, so give each one its own port. In separate
shells:

```bash
export LLM_GATEWAY_URL=http://127.0.0.1:8081   # the bundled gateway, below
export ADHAR_AI_KUBE_DISABLED=1                # unless you have a kubeconfig

uv run adhar-ai gateway --listen=:8081

uv run adhar-ai mcp --domain=cluster       --listen=:8090
uv run adhar-ai mcp --domain=gitops        --listen=:8091
uv run adhar-ai mcp --domain=provision     --listen=:8092
uv run adhar-ai mcp --domain=observability --listen=:8093
uv run adhar-ai mcp --domain=security      --listen=:8094
uv run adhar-ai mcp --domain=cost          --listen=:8095
uv run adhar-ai mcp --domain=catalog       --listen=:8096
```

Point the runtime at them with a local `config.yaml` — remember `mcpServers`
entries are merged over the in-cluster defaults, so every domain you run
locally must be listed or the runtime will try to reach the cluster address:

```yaml
autonomy:
  default: suggest
mcpServers:
  cluster:       http://127.0.0.1:8090
  gitops:        http://127.0.0.1:8091
  provision:     http://127.0.0.1:8092
  observability: http://127.0.0.1:8093
  security:      http://127.0.0.1:8094
  cost:          http://127.0.0.1:8095
  catalog:       http://127.0.0.1:8096
rag:
  enabled: true
```

```bash
uv run adhar-ai runtime --config=./local.yaml --listen=:8080
curl -s localhost:8080/healthz | jq '.mcp_servers_connected, .mcp_servers_unreachable'
```

Then ask it something:

```bash
curl -s localhost:8080/chat \
  -H 'content-type: application/json' \
  -d '{"prompt":"which applications are out of sync, and why?"}' | jq
```

`LLM_GATEWAY_URL` defaults to the in-cluster data-plane address, which is
unresolvable off-cluster on purpose — export it for every local run.

### Re-indexing the docs

```bash
export ADHAR_AI_RAG_DSN=postgresql://adhar_ai:adhar_ai@localhost:5432/adhar_ai_rag
uv run adhar-ai index --docs ../adhar/docs
# indexed 1051 chunks from ../adhar/docs using gateway embeddings
```

The table is truncated first, so re-running replaces the index rather than
accumulating duplicates. Never mix vectors from two embedding backends in one
index — re-index in full when you switch.

---

<sub>Part of the <a href="https://github.com/adhar-io/adhar">Adhar</a> open internal developer platform. See also
<a href="ARCHITECTURE.md">Architecture</a>, <a href="TOOLS.md">Tool Reference</a> and <a href="SECURITY.md">Security</a>.</sub>
