<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/branding/adhar-logo-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/branding/adhar-logo.svg">
  <img alt="Adhar — Open Cloud-Native Foundation" src="docs/images/branding/adhar-logo.svg" width="300">
</picture>

<h1>Adhar AI — the Agentic Layer of the Adhar Platform</h1>

<p><em>Give the platform an agent, not a root shell.</em></p>

[![Adhar AI](https://img.shields.io/badge/adhar--ai-0.1.0-blue?logo=data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMjQiIGhlaWdodD0iMjQiIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48cGF0aCBkPSJNMTIgMkw0IDhWMTVDNCAxOC4zMSA2LjY5IDIxIDEwIDIxQzEzLjMxIDIxIDE2IDE4LjMxIDE2IDE1VjhMMTIgMloiIGZpbGw9IndoaXRlIi8+PC9zdmc+)](https://github.com/adhar-io/adhar-ai)
[![MCP](https://img.shields.io/badge/MCP-native-7C3AED?logo=anthropic)](https://modelcontextprotocol.io)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue?logo=python&logoColor=white)](https://www.python.org)
[![Tools](https://img.shields.io/badge/governed_tools-27-blue)](contract/tools.json)
[![Writes](https://img.shields.io/badge/writes-pull_request_only-success?logo=git)](#-the-rule-that-shapes-everything)
[![License](https://img.shields.io/badge/license-Apache%202.0-green?logo=apache)](LICENSE)
[![Slack](https://img.shields.io/badge/slack-join_community-4A154B?logo=slack)](https://join.slack.com/t/adharworkspace/shared_invite/zt-26586j9sx-QGrIejNigvzGJrnyH~IXww)

<h3>Adhar • Built with ❤️ for developers!</h3>

</div>

---

## 🤖 What is Adhar AI?

**Sanskrit: अधार (Adhāra) – Foundation**

Adhar AI is the agent runtime behind the Adhar platform's `ai/adhar-ai` package
([ADR-0024](https://github.com/adhar-io/adhar/blob/main/docs/adr/0024-agentic-ai-platform.md)).
Configure one LLM key and the platform gains the ability to **investigate,
explain, scaffold, provision and remediate** — *through* its existing control
surfaces, never around them.

Operating an internal developer platform demands expert fluency across
Kubernetes, ArgoCD, Gitea, Crossplane, Keycloak, Vault, the LGTM stack, Kyverno
and OpenCost. Adhar AI is how the platform answers for itself. Ask it why an
application is degraded and it reads the real pods, the real logs, the real sync
status and the real burn rate, then tells you — citing every source. Ask it to
fix the problem and it opens a pull request.

> ⚠️ **Active development.** APIs and tool schemas may change. The container
> images are not yet published to GHCR — see [Project status](#-project-status).

---

## 🛡️ The rule that shapes everything

> **Read tools read. Write tools open a pull request. Nothing applies to a cluster.**

An agent holding `kubectl apply` and a cluster-admin token is a catastrophe
waiting for a prompt injection. Adhar's whole thesis is GitOps-first,
policy-enforced, RBAC-scoped, reviewable change, so an AI layer that bypassed
that thesis would be a regression rather than a feature.

Every write tool here commits to a branch in Gitea and opens a PR against the
GitOps repo. There is **no `kubectl apply` path in this codebase**, and the
ServiceAccount the platform binds is read-only. A human merges, ArgoCD
reconciles, and the change is as reviewable and revertible as any other. Every
artifact the agent creates carries `adhar.io/origin: adhar-ai` and a branch
prefixed `adhar-ai/`.

The property is **structural, not configured**. A test asserts, at the source
level, that every write tool's body reaches `open_pr` and contains no
`apply`/`kubectl`/`patch_`/`delete_` call, and that the PR module imports no
Kubernetes, AWS, Azure or GCP client at all. The agent's authority is exactly a
contributor's, and it cannot be raised by a setting.

---

## 🧩 What ships here

One image, three entrypoints:

| Component | What it is | Entrypoint |
|---|---|---|
| 🔌 **MCP tool servers** | one server per domain, streamable HTTP at `/mcp` on `:8080` | `adhar-ai mcp --domain <domain>` |
| 🧠 **Agent runtime** | event-driven operators plus a tool-use chat loop | `adhar-ai runtime` |
| 🚪 **LLM gateway** | provider-agnostic, OpenAI-compatible — **local development only** | `adhar-ai gateway` |

In the platform, LLM traffic and the federated MCP endpoint are served by
[agentgateway](https://agentgateway.dev) rather than by the gateway in this repo
([ADR-0025](https://github.com/adhar-io/adhar/blob/main/docs/adr/0025-ai-gateway-agentgateway.md));
the bundled gateway stays as the dependency-free local-dev fallback.

---

## 🧰 Tool inventory

`adhar-ai tools` prints this as JSON; it is the same contract as
[`contract/tools.json`](contract/tools.json), and CI fails on any drift between
the two.

| Domain | 👀 Read tools | ✍️ Write tools (PR-only) |
|---|---|---|
| `cluster` | `list_pods`, `describe`, `get_events`, `logs`, `resource_health` | — |
| `gitops` | `app_status`, `sync_status`, `app_diff` | `propose_change` |
| `provision` | `list_xrs`, `xr_status` | `propose_xr` |
| `observability` | `promql`, `logql`, `traceql`, `slo_burn`, `correlate` | — |
| `security` | `findings`, `policy_explain`, `posture` | `propose_exception` |
| `cost` | `cost_by`, `budget_status`, `showback` | — |
| `catalog` | `search_packages`, `template_params` | `scaffold` |

**27 tools, 4 of them writes.** There is deliberately no `kubectl_apply`,
`argo_sync`, `helm_install` or cloud-mutation tool, and a test asserts each of
those names is absent from every domain.

Outward, the seven servers are one federated endpoint — `https://mcp.<host>/mcp`
on agentgateway, which multiplexes them all, validates the Keycloak token and
authorizes per tool (read tools need `platform-developer`, the PR-opening tools
need `platform-admin`). **External agents — Claude Code, an IDE, ChatOps — drive
Adhar through exactly these governed tools with one URL and one token.** Tool
names arrive prefixed by their server (`gitops_propose_change`), which is what
the gateway's authorization rules key on.

The servers themselves no longer validate tokens: that control moved to the data
plane, once, in front of all seven. They still receive the caller's bearer token
(agentgateway sets `preserveToken: true`) so RBAC-scoped reads run as the user.

👉 Full reference: **[docs/TOOLS.md](docs/TOOLS.md)**

---

## 🪜 Staged autonomy

The runtime reads its stage from the `adhar-ai-config` ConfigMap and never acts
above it. Each rung differs from the one below in a way you can observe:

| Stage | Behaviour |
|---|---|
| `read-only` | write tools are not offered at all, and are refused if called anyway |
| `suggest` *(default)* | writes allowed; the **first** pull request ends the run, so a human reads one proposal rather than a chain |
| `approve-to-apply` | the run **continues** after a PR, so the agent can verify its own proposal; a human still merges every one |
| `scoped` | runs unattended, and is therefore confined to the **narrower** `writePolicy.scoped` allow-list — empty by default, so this rung permits nothing until an operator enumerates it |

Authority only ever **narrows** as it flows. The ConfigMap sets a ceiling, a
request may ask for less, an operator policy may be stricter still, and an
unauthenticated caller is pinned to `read-only` regardless. No path widens it,
and there is no rung at which the model chooses its own scope — the allow-list is
read from the ConfigMap, never from a tool argument, because an argument is
something a prompt injection can set.

Four operators run on events rather than a schedule: **alert-triage**
(Alertmanager webhook), **drift-explain** (ArgoCD OutOfSync), **cost-advisor**
(OpenCost) and **upgrade-preflight**. Each emits a structured finding. Whether
that finding becomes a PR is the stage's decision, not the model's.

---

## 🔐 Who may drive it

The runtime is the one Adhar AI surface that does **not** sit behind
agentgateway — it publishes its own hostname so the Console, the `adhar ai` CLI,
Alertmanager and ArgoCD notifications can reach it directly. So it authenticates
for itself:

| Caller | Credential | Route |
|---|---|---|
| People (Console, `adhar ai`) | Keycloak JWT, verified against the realm JWKS | `/chat` |
| Machines (Alertmanager, ArgoCD) | shared webhook bearer token | `/operators/{name}/event` |

With **no credential at all**, a caller is still answered — and pinned to
`read-only`. Investigation stays open to anyone who can reach the port;
*authority* requires a credential. Set `ADHAR_AI_REQUIRE_AUTH=true` to refuse
the request outright instead, which is the right posture once the platform's own
clients are wired up.

👉 Threat model and controls: **[docs/SECURITY.md](docs/SECURITY.md)**

---

## 📚 Grounding

Retrieval runs over the platform's own docs, ADRs and runbooks. Two paths, and
the second is what makes an **unkeyed** platform still useful:

- **Vector** — chunks embedded through the gateway into pgvector (the
  `adhar-ai-rag` CNPG database), queried by cosine distance. `adhar-ai index
  --docs <path>` re-indexes.
- **Lexical** — a dependency-free BM25 index built in-process from the same docs
  tree. It needs no key and no database, so it answers when there is no provider
  key, no CNPG, an empty table mid-first-index, or a database that has started
  failing.

Every grounding block names the source **and which path found it**, and
`GET /healthz` reports the live mode. Answers cite what they were grounded on.

---

## 🔀 Providers

Everything here speaks `/v1/chat/completions` and `/v1/models` to whatever
gateway `LLM_GATEWAY_URL` points at, and **always names a model in the body**.
Which backend that model reaches depends on which gateway is in front:

| | In the platform (agentgateway) | Locally (`adhar-ai gateway`) |
|---|---|---|
| Provider choice | the **model name**: `claude-*` → Anthropic, `gpt-*`/`o[1-9]-*` → OpenAI, `local/*` → in-cluster vLLM | `ADHAR_AI_LLM_PROVIDER`: `anthropic` (default), `openai`, `azure`, any OpenAI-compatible base URL, `ollama` |
| Credentials | `ANTHROPIC_API_KEY` + `OPENAI_API_KEY` in the `adhar-ai-llm` Secret, read by the proxy | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` in the process env |
| Budgets | `conditional` rate limits per Keycloak group, in the gateway package | `BUDGET_*` env in this process |

`LLM_GATEWAY_URL` may be given with or without the `/v1` suffix — the platform
sets it with, `docker compose` without, and the client normalizes it to exactly
one `/v1` either way. Token and request budgets are enforced in whichever gateway
is in front, so a runaway agent costs a 429 rather than a bill.

---

## ⚡ Quick start

```bash
uv sync --extra rag                    # Python 3.12+
uv run adhar-ai tools                  # the tool inventory — no cluster needed
uv run adhar-ai mcp --domain cluster   # :8081/mcp
uv run adhar-ai runtime                # :8082
docker compose up                      # gateway + one MCP server + pgvector
```

Then ask it something:

```bash
curl -s localhost:8082/chat \
  -H 'content-type: application/json' \
  -d '{"prompt":"which applications are out of sync, and why?"}' | jq
```

👉 The full walkthrough — every entrypoint, a real cluster, an external agent,
and what to expect at each step — is **[docs/GETTING_STARTED.md](docs/GETTING_STARTED.md)**.

---

## 📖 Documentation

| Guide | What it covers |
|---|---|
| 🚀 **[Getting Started](docs/GETTING_STARTED.md)** | From `uv sync` to a grounded answer to an opened PR, step by step |
| 🏛️ **[Architecture](docs/ARCHITECTURE.md)** | How the three components, the seven servers and the data plane fit together |
| 🧰 **[Tool Reference](docs/TOOLS.md)** | Every one of the 27 tools: arguments, backend, failure mode |
| ⚙️ **[Operations](docs/OPERATIONS.md)** | Every setting, the health surface, and how to diagnose it when it is wrong |
| 🔐 **[Security](docs/SECURITY.md)** | Threat model, the write path, authentication, autonomy, prompt injection |
| 🤝 **[Contributing](CONTRIBUTING.md)** | Adding a tool or an operator without breaking the guarantees |

---

## 🧪 Development

```bash
uv sync --extra rag
uv run pytest -q                       # 260 tests
uv run ruff check src tests
uv run mypy src
uv run adhar-ai tools | diff -u contract/tools.json -   # the Go-CLI contract
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
is how the platform manifests inject them. [`src/adhar_ai/config.py`](src/adhar_ai/config.py)
is the list, and [docs/OPERATIONS.md](docs/OPERATIONS.md) is the annotated version.

---

## 🏗️ How the platform consumes this

The `ai/adhar-ai` package in the platform repo is **opt-in and disabled by
default**, so the platform runs unaffected until someone supplies a key. When
enabled it deploys the runtime and one MCP Deployment per domain (all this image,
differing only by `ADHAR_AI_MCP_DOMAIN`), wires the `adhar-ai-llm` Secret through
Vault and External Secrets, creates the Keycloak `adhar-ai` client, and applies a
Kyverno `adhar-ai-guardrails` policy over what the agent may touch.

**It does not deploy the gateway from this repo.** The AI data plane is
`ai/agentgateway` — upstream [agentgateway](https://agentgateway.dev), configured
through Gateway API. It is the production data plane for both AI protocols:

| | Endpoint | What it does |
|---|---|---|
| 🚪 LLM | `https://ai.<host>/v1/chat/completions` | routes on the model name; holds the provider keys server-side; masks credential-shaped strings; per-group token budgets |
| 🔌 MCP | `https://mcp.<host>/mcp` | federates the seven servers into one tool list; authorizes per tool |
| 🧠 Agent | `https://agent.<host>/` | this repo's runtime (`/chat`, `/findings`, operator webhooks) — the one AI surface not behind the data plane |

---

## 📍 Project status

Against the [platform roadmap](https://github.com/adhar-io/adhar/blob/main/docs/ROADMAP.md)'s
Phase 3 agentic entry:

| Capability | Status |
|---|---|
| MCP-native tools, 7 domains, PR-only writes | ✅ implemented, 260 tests |
| Federated MCP over streamable HTTP at `/mcp` | ✅ verified against in-cluster Host headers |
| GitOps-safe runtime, four-rung autonomy ladder | ✅ each rung behaviourally distinct and tested |
| Authentication on the runtime's own surface | ✅ Keycloak JWT + webhook token |
| Grounding (pgvector + lexical fallback) | ✅ lexical verified on 1,051 real doc chunks |
| Durable findings | ✅ Postgres-backed, degrades to memory |
| **Container images on GHCR** | ⏳ **the remaining gate** — `.github/workflows/images.yml` builds, signs and SBOMs them; it has not been run |
| End-to-end run with a real LLM key | ⏳ needs a keyed cluster |

---

## 📄 Licence

Apache 2.0, same as the platform.

<div align="center">
<sub>Part of the <a href="https://github.com/adhar-io/adhar">Adhar</a> open internal developer platform.</sub>
</div>
