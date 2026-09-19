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
| 🧠 **Agent runtime** | seven specialist agents, durable tasks, event-driven operators and a tool-use chat loop | `adhar-ai runtime` |
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

## 🧑‍🚀 Specialist agents, durable work and quiet automation

A single assistant with twenty-seven tools answers every question the same
average way. Seven specialists do not, and none of them can do more than the one
assistant could:

| Agent | Answers | Ceiling |
|---|---|---|
| 🚨 `incident` | alerts, crashloops, outages, "why is this broken?" | `suggest` |
| 💰 `cost` | spend, budgets, showback, right-sizing | `suggest` |
| 🔐 `security` | policy, findings, CVEs, compliance evidence | `approve-to-apply` |
| 🏗️ `platform` | scaffolding, packages, golden paths, Crossplane | `suggest` |
| 🚢 `release` | promotion, drift, sync failures, rollback | `approve-to-apply` |
| 📖 `guide` | how-to, onboarding, conventions | `read-only` |
| 🧭 `generalist` | everything else — routing always terminates here | `suggest` |

Each agent's **ceiling narrows and never widens**: a `read-only` agent stays
read-only for a platform administrator at `scoped`, because the narrowing
belongs to the role rather than to the request. Routing is lexical, not a model
call — spending a completion to decide who should spend a completion doubles
latency on every request. An agent may hand work on, but only to a colleague it
declared, only with a stated reason, and only four times before the chain is
refused as a loop.

**Work outlives the request.** `POST /chat` answers inside the request, which is
right for a question and wrong for an investigation that takes twenty minutes or
a plan that waits on a human. `POST /tasks` returns an id immediately; the work
runs behind a bounded worker pool, survives a restart where a database is
configured, and reports its own state:

```
queued ──► planning ──► awaiting_approval ──► running ──► done | failed | cancelled
```

Above `suggest`, the agent writes a plan **without acting** and the task stops.
Releasing it needs the same `platform-admin` credential the PR-opening tools do,
because a task that can change the platform is exactly as privileged as a write.

**Seven chores ship, all off and all in dry-run.** Certificate expiry, drift,
orphaned resources, failing scorecards, cost outliers, stale findings, runbook
rot. Enabling one and letting it act are two separate decisions, and
`maxProposals` bounds what a single run can open — the difference between a
helpful Monday morning and an unreviewable flood.

**The agent turns up where the work is:** a Slack thread, a pull-request review
that declares itself, a failed pipeline. A thread is a conversation, so a
follow-up continues rather than starting fresh. Every inbound payload is treated
as attacker-influenced, and the structural guarantee holds whatever it says.

**It knows what it cannot answer.** `GET /capabilities` is derived from the live
tool surface rather than hand-written, so it never promises something that no
longer exists. `GET /coverage` is the queue of questions the platform answered
badly, clustered by how often each has been asked — which is the list somebody
writes the missing runbook from.

👉 Full reference: **[docs/AGENTS.md](docs/AGENTS.md)**

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

### The runtime's HTTP surface

| Route | What it does |
|---|---|
| `GET /healthz` | posture: connected MCP servers, grounding mode, auth, findings store |
| `GET /config` | the effective autonomy policy, read back |
| `POST /chat` | one agent run — an answer, or a proposed pull request |
| `POST /operators/{name}/event` | operator webhook (Alertmanager, ArgoCD notifications) |
| `GET /findings` | what the operators concluded |
| `GET /knowledge` | what the knowledge base holds, by origin and kind |
| `POST /knowledge` | add a note, runbook or incident write-up |
| `POST /knowledge/search` | retrieve grounding without running the agent |
| `POST /knowledge/refresh` | re-derive knowledge now |
| `POST /feedback` | say whether an answer's grounding helped |
| `POST /tasks` | start work that outlives the request |
| `GET /tasks` · `GET /tasks/{id}` | what is running, and what one task did |
| `POST /tasks/{id}/approve` | release or reject a plan — needs a writer |
| `GET /agents` · `POST /agents/route` | the roster, and who would take this |
| `POST /journeys/{surface}` | Slack, a pull request, a failed pipeline |
| `GET /chores` · `POST /chores/{name}/run` | the catalogue, and one run now |
| `GET /capabilities` | what the platform can do right now, derived not declared |
| `GET /coverage` | what it could not answer — the queue of runbooks to write |

`/healthz` is the only one left open — it is the readiness probe, and it
presents no credential. Everything else is gated, so
`ADHAR_AI_REQUIRE_AUTH=true` closes every route that carries cluster detail.

👉 Threat model and controls: **[docs/SECURITY.md](docs/SECURITY.md)**

---

## 🏭 Built to be left running

An agent that works on a good day is a demo. These are the controls that make it
safe to leave running on a bad one.

| Concern | What is there |
|---|---|
| 📊 **Observability** | `/metrics` on every component, two ServiceMonitors, a 12-panel Grafana dashboard, optional OTel tracing with GenAI semantic conventions |
| 💰 **Cost** | tokens and realized spend per model, and a refusal to *estimate* cost when the gateway does not report it |
| 🔁 **Resilience** | retry with full jitter on what is worth retrying, per-dependency circuit breakers, timeouts on everything outbound |
| 🚦 **Admission** | per-caller rate limiting, idempotent operator webhooks, graceful draining on SIGTERM |
| 🔒 **Safety** | outbound credential masking, an optional model allow-list, policy refusals counted as a first-class signal |
| 🎯 **Quality gates** | a behavioural red-team suite and a scenario eval suite, both in CI |

Two of these deserve a sentence each.

**Duplicate delivery is real.** Alertmanager retries, and so does ArgoCD
notifications. Without deduplication one flapping alert becomes N agent runs and,
above `read-only`, N near-identical pull requests for one problem. Operator
events are keyed on the event body with volatile timestamps stripped, so a retry
replays the first finding instead of doing the work again.

**`adhar_ai_denied_total` is the security signal.** A steady trickle of policy
refusals is the system working. A spike is either a misconfiguration or something
trying to make the agent exceed its authority — and it is the only metric that
tells those apart from "the agent is quiet today".

👉 Full detail: **[docs/PRODUCTION.md](docs/PRODUCTION.md)**

---

## 📚 Grounding — a knowledge base, not a docs folder

An agent that only knows Kubernetes in general is a search engine with extra
steps. Adhar AI builds a **knowledge base of your platform** and keeps it
current. Six sources, re-derived on a schedule:

| Source | What it answers |
|---|---|
| 📘 **Documentation** | "why is it built this way", "how do I do X" |
| 🧰 **Tool inventory** | "what can you actually do for me" |
| 📦 **Package catalogue** | "what is installed, what does it depend on" |
| ☸️ **Live cluster** | "what is running right now, and is it healthy" |
| 🔍 **Operator findings** | "has the platform noticed this before" |
| 📝 **Human notes** | "what did we decide, and what did we learn" |

**Retrieval is hybrid.** Vector similarity finds things phrased differently from
the question; Postgres full-text finds the exact identifier the user typed
(`CreateContainerConfigError`, `CompositeCluster`) that an embedding blurs away.
Platform questions contain a lot of exact identifiers, so the two are fused by
reciprocal rank rather than chosen between.

**It learns.** Notes are indexed the moment they are written, so a postmortem
typed at 02:00 is retrievable at 02:01. Every operator finding is fed back, so
the next similar alert retrieves what the last investigation concluded. And every
answer returns the chunk ids behind it, so `POST /feedback` teaches the ranking
what actually helped.

**It is incremental**, which is what makes keeping it current affordable. A
refresh re-embeds only what changed — measured on Adhar's own corpus, a second
pass over 1,299 chunks rewrote nothing and made zero embedding calls.

**It degrades without a cliff:**

| You have | You get |
|---|---|
| pgvector + an embedding endpoint | hybrid vector and lexical retrieval |
| pgvector, no provider key | full-text over the same indexed corpus |
| no database | in-process BM25 over the docs tree |
| no docs either | honest: "no grounding is available" |

So an unkeyed platform is still grounded. `GET /healthz` says which rung you are
on, and every grounding block names its source *and* which path found it.

👉 Full detail: **[docs/KNOWLEDGE.md](docs/KNOWLEDGE.md)**

---

## 🔀 Providers

`local` — no key. The model runs on the platform (`ai/llm-d`: vLLM replicas behind the llm-d router with an agentgateway sidecar) and agentgateway routes every `local/*` model name there; `ADHAR_AI_LLM_PROVIDER=local` selects it and `DEFAULT_MODELS["local"]` names the served model.

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
docker compose up                      # the whole stack: gateway, all seven
                                       # MCP servers, the runtime and pgvector
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
| 🧠 **[Knowledge Base](docs/KNOWLEDGE.md)** | What the agent knows, how it stays current, and how it learns |
| 🤖 **[Agents & Automation](docs/AGENTS.md)** | The specialist roster, durable tasks, approvals, chores and journeys |
| 🏭 **[Production](docs/PRODUCTION.md)** | Metrics, resilience, admission control, safety and the quality gates |
| ⚙️ **[Operations](docs/OPERATIONS.md)** | Every setting, the health surface, and how to diagnose it when it is wrong |
| 🔐 **[Security](docs/SECURITY.md)** | Threat model, the write path, authentication, autonomy, prompt injection |
| 🤝 **[Contributing](CONTRIBUTING.md)** | Adding a tool or an operator without breaking the guarantees |

---

## 🧪 Development and verification

```bash
uv sync --extra rag
uv run pytest -q                       # 498 tests (517 with a database)
uv run ruff check src tests
uv run mypy src
uv run adhar-ai tools | diff -u contract/tools.json -   # the Go-CLI contract
```

The last one is not a formatting check. `contract/tools.json` is the schema
snapshot the Adhar Go CLI is written against, so a diff there is a breaking
change rather than a refactor.

### Beyond the unit suite

Unit tests drive tools in-process and fake the toolbox, which is fast and blind
to a whole class of defect. Every bug in the list below was found by something
in this section while the unit suite was green, so both harnesses are worth
running.

```bash
./hack/e2e-local.sh
```

Stands up all seven MCP servers and the runtime over **real HTTP** and asserts
the seam between them: the transport's Host-header handling, the MCP client's
result unwrapping, and whether an unconfigured backend's error text survives the
trip to the model. A second runtime then runs against a scripted
OpenAI-compatible server ([`hack/stub-llm.py`](hack/stub-llm.py)), which drives
the agentic layer end to end — a task that outlives its request, a handoff
recorded on the task, a Slack thread that remembers its first message, a
dry-run chore that proposes nothing. It needs no cluster, no key and no
database: what it asserts is either true offline or honestly reported as
unavailable, which is the property being tested. It runs in CI.

```bash
docker run -d --name adhar-rag -p 15432:5432 \
  -e POSTGRES_USER=adhar_ai -e POSTGRES_PASSWORD=adhar_ai \
  -e POSTGRES_DB=adhar_ai_rag pgvector/pgvector:pg16

ADHAR_AI_RAG_DSN=postgresql://adhar_ai:adhar_ai@127.0.0.1:15432/adhar_ai_rag \
  uv run hack/verify-knowledge.py --docs ../adhar/docs \
  --packages ../adhar/platform/stack/packages
```

Checks the knowledge base against a **real pgvector**: schema, ingestion from
every source, incrementality, hybrid retrieval, exact-identifier lookup,
immediate note availability, feedback ranking and deletion propagation. The same
database makes the 13 skipped tests run:

```bash
ADHAR_AI_TEST_DSN=postgresql://adhar_ai:adhar_ai@127.0.0.1:15432/adhar_ai_rag \
  uv run pytest -q                     # 279 passed
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
Phase 3 agentic entry.

| Capability | Status |
|---|---|
| MCP-native tools, 7 domains, PR-only writes | ✅ 498 tests, 517 with a database |
| Federated MCP over streamable HTTP at `/mcp` | ✅ verified against the Host headers a Pod actually receives |
| GitOps-safe runtime, four-rung autonomy ladder | ✅ each rung behaviourally distinct and tested |
| Authentication on the runtime's own surface | ✅ Keycloak JWT, webhook token, outbound service-account token |
| Knowledge base — 6 sources, hybrid retrieval, learning loop | ✅ verified against a real pgvector: 1,299 chunks, 0 re-embeds on an unchanged pass |
| Durable findings | ✅ Postgres-backed, degrades to memory |
| Production controls — metrics, tracing, resilience, admission, safety | ✅ `/metrics` on every component, dashboard drift-tested against the registry |
| Quality gates — behavioural red team, scenario evals | ✅ both in CI; the live eval graded a real model and found a real bug |
| Specialist agents, routing, handoff with a depth cap | ✅ ceiling narrowing and refusal paths tested; handoff verified over real HTTP |
| Durable tasks, plan-and-approve, bounded workers | ✅ state machine refuses illegal moves; approval needs a write credential |
| Knowledge graph over the same Postgres | ✅ verified against a real database: traversal, blast radius, cycles, per-origin refresh |
| Chores, journeys, capability catalogue, coverage gaps | ✅ every chore ships off and in dry-run, asserted as policy |
| **Live run against a real LLM and a real cluster** | ✅ **see below** |
| **Container images on GHCR** | ⏳ **the remaining gate** |
| GPU run of the `ai/vllm` profile | ⏳ needs a GPU node pool |
| Self-hosted inference on CPU via `ai/llm-d` (router + agentgateway sidecar → vLLM), provider `local` | ⏳ built 2026-09-15, awaiting the DigitalOcean end-to-end run |
| Tool re-discovery without a runtime restart (`MCPToolbox.refresh()`) | ✅ unit-tested (a rolled MCP server used to need a restart) |

### What a live run proved

Exercised against a real LLM through the gateway, a real bootstrapping Adhar
cluster and a real Gitea — not mocks:

- **The agent diagnosed a live cluster.** Three tool calls against a
  half-bootstrapped `adhar-system`, and every specific claim checked out: a
  missing `argocd-redis` Secret, Cilium returning
  `putEndpointIdTooManyRequests`, and a dex container exiting 20.
- **It refused to invent data.** Asked for CPU figures with no Prometheus
  configured, it named the backend and the environment variable that fixes it
  rather than producing a number.
- **It opened a real pull request.** Correct `adhar-ai/` branch, `adhar-ai`
  label, provenance block with an audit id, commit trailer — and `main`
  untouched, which is the guarantee that matters.
- **Embeddings are real.** 1536 dimensions with genuine semantic structure: a
  paraphrase scored 0.39 against its original where unrelated text scored 0.12.

That run also found four defects the unit suite could not, all since fixed:

| Defect | Why it mattered |
|---|---|
| The Gitea client doubled `/api/v1` | the first real pull request 404'd, and the docs told you to configure it the broken way |
| MCP sessions never recovered from a server restart | a rolling Deployment cost that domain its tools forever, while `/healthz` still called it connected |
| A transport failure raised `CancelledError` | `except Exception` does not catch it, so it killed the whole agent run |
| The runtime sent no bearer to the LLM gateway | which runs JWT validation in `Strict` mode, so no agent run could reach a model |

**Still outstanding.** The images are not published, so the platform package has
nothing to pull and the components have not run *as Deployments in a cluster* —
the live run drove them from a workstation against that cluster's services. The
build, sign and SBOM workflow is committed and its smoke test passes locally.

---

## 📄 Licence

Apache 2.0, same as the platform.

<div align="center">
<sub>Part of the <a href="https://github.com/adhar-io/adhar">Adhar</a> open internal developer platform.</sub>
</div>
