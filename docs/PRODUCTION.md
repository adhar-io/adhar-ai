# 🏭 Running Adhar AI in production

The controls that make an agent safe to leave running: what it exports, what it
does when a dependency fails, what it refuses, and how you know it is working.

**Sanskrit: अधार (Adhāra) – Foundation**

---

## Contents

1. [Observability](#1-observability)
2. [Resilience](#2-resilience)
3. [Admission control](#3-admission-control)
4. [Safety controls](#4-safety-controls)
5. [Lifecycle](#5-lifecycle)
6. [Quality gates](#6-quality-gates)
7. [Configuration reference](#7-configuration-reference)
8. [Alerts worth having](#8-alerts-worth-having)

---

## 1. Observability

Every component serves `/metrics` — the runtime, the local-dev gateway, and each
of the seven MCP servers. The platform package ships two ServiceMonitors and a
Grafana dashboard.

### The metrics, and the question each answers

| Question | Metric |
|---|---|
| Is it working? | `adhar_ai_agent_runs_total{outcome}` |
| Is it slow? | `adhar_ai_agent_run_duration_seconds` |
| What is it costing? | `adhar_ai_tokens_total`, `adhar_ai_cost_usd_total` |
| Is it trying things it should not? | `adhar_ai_denied_total{reason}` |
| Is it changing anything? | `adhar_ai_pull_requests_total` |
| Which backend is broken? | `adhar_ai_tool_calls_total{decision}`, `adhar_ai_circuit_state` |
| Has it lost a domain? | `adhar_ai_mcp_servers_connected` |
| Is its knowledge current? | `adhar_ai_knowledge_chunks{origin}` |

**`adhar_ai_denied_total` is the one to watch.** A steady trickle of policy
refusals is the system working. A spike is either a misconfiguration or
something trying to make the agent exceed its authority, and it is the only
signal that tells those apart from "the agent is quiet today".

**Cost is recorded only where the gateway reports it.** Deriving it from a
hardcoded price table would put a number on a dashboard that silently goes wrong
the next time a provider reprices, and somebody will budget against that number.
An empty spend panel means the gateway does not report cost, not that the agent
is free.

### Why the MCP servers export their own

An external agent — Claude Code, an IDE, ChatOps — reaches the tools through
agentgateway and never touches the runtime. Instrumenting only the runtime would
leave the entire outward MCP surface invisible. Each domain server counts its own
calls, and the ServiceMonitor relabels `adhar.io/mcp-domain` onto every series so
a slow domain is attributable rather than averaged away.

### Cardinality

Tool names, domains, outcomes and autonomy stages are closed sets. **The tenant
is deliberately not a label** — it is unbounded, and an agentic platform with a
label per user is a Prometheus outage waiting for a busy afternoon. Per-tenant
spend is in the audit stream, which is built for high cardinality.

### Tracing

Optional, off unless `ADHAR_AI_OTLP_ENDPOINT` is set and the `tracing` extra is
installed. Spans use the **OTel GenAI semantic conventions**
(`gen_ai.request.model`, `gen_ai.usage.input_tokens`) because ADR-0025 has
agentgateway emitting exactly those — a private vocabulary here would produce two
naming schemes for one request path and a dashboard that shows half of it.

`GET /healthz` reports `"tracing": "on" | "off"`, so an empty Tempo is
diagnosable rather than mysterious.

---

## 2. Resilience

An agent run is a chain of network calls where any single failure throws away
everything done so far. A completion that 503s on step four of six costs the five
tool calls before it, the tokens they consumed, and the user's patience.

### Retry

Exponential backoff with **full jitter**, only on failures a retry can fix: a
timeout, a connection error, 408, 409, 425, 429, 5xx. A 400 or a 401 is retried
zero times — the second attempt fails identically and the only thing it adds is
latency.

Jitter is not a detail. Without it every replica backs off from the same provider
outage at the same instant, turning one outage into a synchronised herd at 1s, 2s
and 4s.

> A **429 from the gateway is not retried**. That is a budget decision, not
> congestion, and retrying it spends the caller's remaining allowance on requests
> meant to be refused.

### Circuit breaking

Retries make a *transient* failure survivable and a *sustained* one worse: every
step politely retries a backend that has been down for ten minutes. After five
consecutive failures a dependency's circuit opens and calls fail immediately with
a message naming it. One probe is let through after 30s to find out whether it is
back.

Breakers are per dependency, so a dead Prometheus does not stop the agent reading
Kubernetes. `GET /healthz` lists any that are not closed, and
`adhar_ai_circuit_state` alerts on it.

---

## 3. Admission control

An agent run is expensive and slow in a way an ordinary HTTP handler is not, so
three ordinary web concerns are unusually sharp.

### Duplicate delivery

**Alertmanager retries. So does ArgoCD notifications.** Both re-send on any
non-2xx and on their own restart, and a retried alert is indistinguishable from a
new one — same labels, same annotations. Without deduplication one flapping alert
becomes N agent runs and, above `read-only`, N near-identical pull requests for
one problem.

Operator events are keyed on a hash of the operator plus the event body, with
volatile fields (`startsAt`, `fingerprint`, `groupKey`) stripped first — including
them would make every retry look new, which is the failure this prevents. A
repeat returns the first run's finding with `"replayed": true`.

### Rate

agentgateway holds per-group token budgets, which is the right place for spend.
It does not stop one caller starting fifty concurrent runs, each cheap at step
one and expensive by step six. `ADHAR_AI_RATE_LIMIT` caps runs *started* per
caller per window, and returns `429` with `Retry-After`.

### Draining

`ADHAR_AI_SHUTDOWN_GRACE_SECONDS` lets in-flight runs finish on SIGTERM while new
ones get `503` with `Retry-After`. A run has already spent its tokens and may
have opened a branch; dropping it on a rollout wastes the spend and can leave a
half-made proposal.

---

## 4. Safety controls

ADR-0025 puts prompt guards and budgets in agentgateway, and that is the right
place: one enforcement point in front of every AI request. These two cover what
the data plane cannot.

### Outbound credential masking

The agent assembles prompts from tool output it did not write: pod environment
blocks, log lines, ConfigMap contents, PR diffs. A Secret mounted as an env var
and echoed into a crash log becomes part of a prompt automatically, and no amount
of care in the system prompt prevents it.

Masked **as the tool result enters the conversation**, not at the HTTP boundary.
Scrubbing on the way out would keep a credential out of the provider's logs and
leave it in the transcript, the audit record, and every subsequent turn.

Patterns are anchored on structure rather than on the word "password", because
the dangerous case is a value with no label around it: PEM blocks, JWTs, AWS and
GitHub and OpenAI key shapes, URLs carrying inline credentials, `Authorization`
headers.

The audit records the **kind** of credential and never the value. An audit trail
that leaks the secret it is reporting is worse than no audit trail.

### Model allow-list

Under agentgateway the caller names a model and the gateway routes on it, so a
request body chooses how much a run costs. `ADHAR_AI_ALLOWED_MODELS` accepts
exact names and `claude-*` prefixes.

**Empty by default**, deliberately. An allow-list that blocks the platform's own
default model on upgrade day is worse than none.

---

## 5. Lifecycle

| Signal | Behaviour |
|---|---|
| `GET /healthz` | readiness and posture. Ungated — it is the probe, and presents no credential |
| `GET /metrics` | Prometheus exposition. Ungated — aggregate counters, no tenant label, no prompt, no finding |
| SIGTERM | refuse new work, drain in-flight runs, then exit |

Every other route is gated, so `ADHAR_AI_REQUIRE_AUTH=true` closes everything
that carries cluster detail.

---

## 6. Quality gates

Four, all in CI:

```bash
uv run pytest -q                          # 366 unit tests
uv run pytest tests/test_red_team.py -q   # adversarial, behavioural
uv run pytest tests/evals -q              # scenario suite, scripted
./hack/e2e-local.sh                       # real processes, real HTTP
```

### Red team

Drives the real loop with a model that has been **fully compromised** — it does
exactly what the injected text asks — and asserts it still cannot exceed its
authority. The guarantee has to hold when the model is wrong, because a guarantee
that depends on the model being right is not a guarantee.

Covers: unlocking a write at `read-only`, widening the write scope, path
traversal inside an allowed repo, `scoped` with no configured scope, inventing a
tool, and exfiltrating a credential through a tool result.

### Evals

Every scenario runs twice over. **Scripted** in CI with no key, grading the
system: was the tool offered, did grounding reach the prompt, was the stage
respected, did the write become a pull request. **Live** on demand, grading a
real model's judgement:

```bash
ADHAR_AI_EVAL_GATEWAY=http://127.0.0.1:8081 \
ADHAR_AI_EVAL_MODEL=claude-sonnet-5 \
  uv run pytest tests/evals -q -m eval -s
```

The floor is configurable (`ADHAR_AI_EVAL_FLOOR`, default 0.7) because it is a
property of the **model**, not the system — a small free model scores well below
a frontier one on identical code.

> The live suite is what found the empty-turn bug: a reasoning model returning
> neither content nor a tool call was being reported as a successful answer with
> empty text. It is now an error that names the `finish_reason` and what to do.

---

## 7. Configuration reference

### Observability

| Variable | Default | What it does |
|---|---|---|
| `ADHAR_AI_OTLP_ENDPOINT` | `""` | OTLP gRPC endpoint. Unset means tracing off |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `""` | standard fallback for the above |
| `ADHAR_AI_TRACING_DISABLED` | `false` | force tracing off even when an endpoint is set |

### Resilience

Retry and breaker settings are code defaults rather than environment variables,
on purpose: they are a property of the call being made, not of the deployment,
and an operator tuning them per cluster is a sign something else is wrong.

| Setting | Default |
|---|---|
| LLM retry attempts | 3 |
| Base backoff / ceiling | 0.5s / 8s, full jitter |
| LLM request timeout | 300s |
| Circuit threshold / cooldown | 5 consecutive failures / 30s |

### Admission

| Variable | Default | What it does |
|---|---|---|
| `ADHAR_AI_RATE_LIMIT` | `20` | runs one caller may start per window. `0` disables |
| `ADHAR_AI_RATE_WINDOW_SECONDS` | `60` | the window |
| `ADHAR_AI_IDEMPOTENCY_TTL_SECONDS` | `600` | how long a repeated event replays |
| `ADHAR_AI_SHUTDOWN_GRACE_SECONDS` | `30` | how long SIGTERM waits for in-flight runs |

### Safety

| Variable | Default | What it does |
|---|---|---|
| `ADHAR_AI_ALLOWED_MODELS` | `""` | comma-separated allow-list. Empty permits everything |
| `ADHAR_AI_REQUIRE_AUTH` | `false` | refuse unauthenticated callers instead of pinning them to `read-only` |
| `ADHAR_AI_WRITE_GROUPS` | `platform-admin` | Keycloak groups that may drive a write |

### Evals

| Variable | Default |
|---|---|
| `ADHAR_AI_EVAL_GATEWAY` | `""` — unset skips the live suite |
| `ADHAR_AI_EVAL_MODEL` | gateway default |
| `ADHAR_AI_EVAL_FLOOR` | `0.7` |

---

## 8. Alerts worth having

```promql
# The agent is failing rather than answering.
sum(rate(adhar_ai_agent_runs_total{outcome="error"}[10m]))
  / sum(rate(adhar_ai_agent_runs_total[10m])) > 0.2

# A domain's tools have gone missing.
min(adhar_ai_mcp_servers_connected) < 7

# A dependency is failing fast.
max by (target) (adhar_ai_circuit_state) >= 2

# Something is repeatedly trying to exceed the agent's authority.
sum(rate(adhar_ai_denied_total{reason!="rate-limit"}[15m])) > 0.1

# Spend is running ahead of plan.
sum(increase(adhar_ai_cost_usd_total[1h])) > 5

# The knowledge base has stopped being re-derived.
min(adhar_ai_knowledge_chunks) == 0
```

The denial alert excludes `rate-limit` on purpose: that one fires on ordinary
enthusiasm, while the others fire on something trying to make the agent do what
it must not.

---

<div align="center">
<sub>See also: <a href="SECURITY.md">Security</a> · <a href="OPERATIONS.md">Operations</a> · <a href="KNOWLEDGE.md">Knowledge base</a></sub><br>
<sub>Adhar • Built with ❤️ for developers!</sub>
</div>
