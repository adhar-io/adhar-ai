# adhar-ai

The agent runtime behind the Adhar platform's `ai/adhar-ai` package
([ADR-0024](https://github.com/adhar-io/adhar/blob/main/docs/adr/0024-agentic-ai-platform.md)).
It lets the platform investigate, explain, scaffold, provision and remediate
**through** its existing control surfaces — never around them.

Three things live here, shipped as one image with three entrypoints:

| Component | What it is | Entrypoint |
|---|---|---|
| **MCP tool servers** | one server per domain, streamable HTTP at `/mcp` on `:8080` | `adhar-ai mcp --domain <domain>` |
| **LLM gateway** | provider-agnostic, OpenAI-compatible API — **local development only** (see below) | `adhar-ai gateway` |
| **Agent runtime** | event-driven operators plus a tool-use chat loop | `adhar-ai runtime` |

In the platform, LLM traffic and the federated MCP endpoint are served by
[agentgateway](https://agentgateway.dev) rather than by the gateway in this repo
([ADR-0025](https://github.com/adhar-io/adhar/blob/main/docs/adr/0025-ai-gateway-agentgateway.md));
the bundled gateway stays as the dependency-free local-dev fallback.

## The rule that shapes the design

**Read tools read. Write tools open a pull request. Nothing applies to a cluster.**

Every write tool below commits to a branch in Gitea and opens a PR against the
GitOps repo. There is no `kubectl apply` path in this codebase, and the
ServiceAccount the platform package binds is read-only. A human merges, ArgoCD
reconciles, and the change is as reviewable and revertible as any other. Every
artifact the agent creates carries `adhar.io/origin: adhar-ai` and a branch
prefixed `adhar-ai/`.

## Tool inventory

`adhar-ai tools` prints this as JSON; it is the same contract as `contract/tools.json`.

| Domain | Read tools | Write tools (PR-only) |
|---|---|---|
| `cluster` | `list_pods`, `describe`, `get_events`, `logs`, `resource_health` | — |
| `gitops` | `app_status`, `sync_status`, `app_diff` | `propose_change` |
| `provision` | `list_xrs`, `xr_status` | `propose_xr` |
| `observability` | `promql`, `logql`, `traceql`, `slo_burn`, `correlate` | — |
| `security` | `findings`, `policy_explain`, `posture` | `propose_exception` |
| `cost` | `cost_by`, `budget_status`, `showback` | — |
| `catalog` | `search_packages`, `template_params` | `scaffold` |

The servers are exposed outward as ONE federated endpoint — `https://mcp.<host>/mcp`
on agentgateway, which multiplexes all seven, validates the Keycloak token and
authorizes per tool (read tools need `platform-developer`, the PR-opening tools
need `platform-admin`). External agents — Claude Code, an IDE, ChatOps — drive
Adhar through exactly these governed tools with one URL and one token. Tool names
arrive prefixed by their server (`gitops_propose_change`), which is what the
gateway's authorization rules key on.

The servers themselves no longer validate tokens: that control moved to the data
plane, once, in front of all seven. They still receive the caller's bearer token
(agentgateway sets `preserveToken: true`) so RBAC-scoped reads run as the user.

## Staged autonomy

The runtime reads its stage from the `adhar-ai-config` ConfigMap the platform
package ships, and never acts above it:

| Stage | Behaviour |
|---|---|
| `read-only` | investigates and answers; writes nothing |
| `suggest` (default) | writes findings and drafts a PR body, does not open it |
| `approve-to-apply` | opens the PR; a human merges |
| `scoped` | opens PRs unattended, only for the repos and paths allow-listed in config |

Four operators run on events rather than a schedule: **alert-triage** (Alertmanager
webhook), **drift-explain** (ArgoCD OutOfSync), **cost-advisor** (OpenCost) and
**upgrade-preflight**. Each emits a structured finding. Whether that finding
becomes a PR is the stage's decision, not the model's.

## Grounding

Retrieval runs over the platform's own docs, ADRs and runbooks, indexed into
pgvector (the `adhar-ai-rag` CNPG database). `adhar-ai index --docs <path>`
re-indexes. Embeddings go through the gateway, so the provider choice is one
setting rather than a code change. With no key configured the retriever degrades
to lexical search instead of pretending to be unavailable.

## Providers

Everything in this repo speaks `/v1/chat/completions` and `/v1/models` to
whatever gateway `LLM_GATEWAY_URL` points at, and **always names a model in the
body**. Which backend that model reaches depends on which gateway is in front:

| | In the platform (agentgateway) | Locally (`adhar-ai gateway`) |
|---|---|---|
| Provider choice | the **model name**: `claude-*` → Anthropic, `gpt-*`/`o[1-9]-*` → OpenAI, `local/*` → in-cluster vLLM | `ADHAR_AI_LLM_PROVIDER`: `anthropic` (default), `openai`, `azure`, any OpenAI-compatible base URL, `ollama` |
| Credentials | `API_KEY`/`ANTHROPIC_API_KEY` + `OPENAI_API_KEY` in the `adhar-ai-llm` Secret, read by the proxy | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` in the process env |
| Budgets | `conditional` rate limits per Keycloak group, in the gateway package | `BUDGET_*` env in this process |

`LLM_GATEWAY_URL` may be given with or without the `/v1` suffix — the platform
sets it with (`…:8080/v1`, the path agentgateway's LLM route matches) and
`docker compose` sets it without; the client normalizes it to exactly one `/v1`
either way.

Token and request budgets are enforced in whichever gateway is in front, so a
runaway agent costs a 429 rather than a bill.

## Run it locally

```bash
uv sync                      # Python 3.12+
uv run pytest -q             # 162 tests
uv run ruff check src tests
uv run mypy src

uv run adhar-ai tools                      # tool inventory, no cluster needed
uv run adhar-ai gateway                    # :8080 — the local-dev LLM gateway
uv run adhar-ai mcp --domain cluster       # :8081/mcp
uv run adhar-ai runtime                    # :8082
docker compose up                          # gateway + one MCP server + pgvector
```

`LLM_GATEWAY_URL` defaults to the platform data plane
(`http://adhar-ai-gateway.adhar-system.svc.cluster.local:8080/v1`), which is
unresolvable off-cluster on purpose — it is the production wiring, and every
local entrypoint above overrides it. `docker compose` sets
`LLM_GATEWAY_URL=http://gateway:8080`; for a bare `uv run adhar-ai runtime`,
export the same.

Against a real platform, point it at the cluster's services:

```bash
export ADHAR_AI_ARGOCD_URL=http://argo-cd-argocd-server.adhar-system.svc.cluster.local
export ADHAR_AI_GITEA_API_URL=http://gitea-http.adhar-system.svc.cluster.local:3000/api/v1
export ADHAR_AI_PROMETHEUS_URL=http://prometheus-kube-prometheus-prometheus.adhar-system.svc.cluster.local:9090
export ADHAR_AI_LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=...
```

Every setting has an `ADHAR_AI_`-prefixed name and an unprefixed fallback, which
is how the platform manifests inject them. `src/adhar_ai/config.py` is the list.

## How the platform consumes this

The `ai/adhar-ai` package in the platform repo is **opt-in and disabled by
default**, so the platform runs unaffected until someone supplies a key. When
enabled it deploys the runtime and one MCP Deployment per domain (all this image,
differing only by `ADHAR_AI_MCP_DOMAIN`), wires the `adhar-ai-llm` Secret through
Vault and External Secrets, creates the Keycloak `adhar-ai` client, and applies a
Kyverno `adhar-ai-guardrails` policy over what the agent may touch.

**It does not deploy the gateway from this repo.** The AI data plane is
`ai/agentgateway` — upstream [agentgateway](https://agentgateway.dev), configured
through Gateway API — enabled alongside `ai/adhar-ai`
([ADR-0025](https://github.com/adhar-io/adhar/blob/main/docs/adr/0025-ai-gateway-agentgateway.md)).
It is the production data plane for both AI protocols:

| | Endpoint | What it does |
|---|---|---|
| LLM | `https://ai.<host>/v1/chat/completions` | routes on the model name; holds the provider keys server-side; masks credential-shaped strings; per-group token budgets |
| MCP | `https://mcp.<host>/mcp` | federates the seven servers below into one tool list; authorizes per tool |
| Agent | `https://agent.<host>/` | this repo's runtime (`/chat`, `/findings`, operator webhooks) — the one AI surface not behind the data plane |

So, concretely, what the platform sets on the Deployments this repo's image runs:

```
LLM_GATEWAY_URL=http://adhar-ai-gateway.adhar-system.svc.cluster.local:8080/v1
```

and, on the MCP servers, **nothing else about identity** — no `OIDC_ISSUER_URL`,
no `OIDC_CLIENT_ID`. Token validation, group-based authorization, prompt
guardrails, token budgets and OTel GenAI tracing are all the gateway's, in one
reviewable place, instead of seven Python re-implementations.

The gateway in this repo remains for local development: `docker compose up` and
`adhar-ai gateway` give the same OpenAI-compatible surface with no cluster,
no Keycloak and no Gateway API, so the loop and the tools can be worked on
offline.

Images publish to `ghcr.io/adhar-io/adhar-ai-*` from `.github/workflows/images.yml`
on tag, and `latest` on main. **They are not published yet** — that is the one
thing standing between this repo and the platform package running end to end.

## Licence

Apache 2.0, same as the platform.
