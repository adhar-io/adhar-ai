# Architecture

How Adhar AI is put together, and why it is put together that way.

This document is for an engineer joining the project or an architect evaluating
it. It assumes Kubernetes, GitOps and LLM agents in general; it assumes nothing
about this codebase. Every claim here is traceable to source in `src/adhar_ai/`,
to the platform package manifests in `ai/adhar-ai` and `ai/agentgateway`, or to
[ADR-0024](https://github.com/adhar-io/adhar/blob/main/docs/adr/0024-agentic-ai-platform.md)
and [ADR-0025](https://github.com/adhar-io/adhar/blob/main/docs/adr/0025-ai-gateway-agentgateway.md).

---

## 🧭 1. The shape of the problem

An internal developer platform is a lot of surfaces. Kubernetes, ArgoCD, Gitea,
Crossplane, Keycloak, Vault, the LGTM stack, Kyverno, OpenCost. Operating one
demands fluency in all of them at once, and most of the work is correlation:
this pod is restarting, that Application is OutOfSync, this policy is failing,
and the three facts are the same fact. That is work an agent is genuinely good
at, because it is tool use over live state rather than recall.

ADR-0024 is explicit that the hard question was never model access. It was
**safety and placement**. An agent holding `kubectl apply` and a cluster-admin
token is a catastrophe waiting for a prompt injection, and Adhar's whole thesis
is GitOps-first, policy-enforced, RBAC-scoped, reviewable change. An AI layer
that bypassed that thesis would be a regression, not a feature. The ADR
considered and rejected two shapes for exactly that reason:

- **A chatbot on the Console calling the Kubernetes API directly.** Fast to
  demo. It is then either read-only and of limited value, or it holds broad
  write credentials and sits outside GitOps — its changes bypass review, policy
  and drift detection.
- **A hosted SaaS copilot.** Conflicts with the platform's open-source,
  no-phone-home, you-own-it principle, and leaks platform state to a third
  party.

What it chose instead was an MCP-native agent layer that acts *through* the
platform's existing control surfaces: reads via RBAC-scoped tools, writes only
by opening a Gitea pull request, governed by the same policy and audit as a
human contributor. The agent is a very capable **contributor and investigator**,
not a root shell.

That single decision is what shapes the rest of this document. The tool
inventory, the autonomy ladder, the layered write gates, the provenance markers
— all of it falls out of "the agent's authority must be exactly a contributor's,
and it must not be raisable by a setting".

ADR-0025 then answered the question that followed: *who governs the traffic?*
That answer is [§5](#-5-control-plane--data-plane).

---

## 🧩 2. The three components and one image

Three roles ship from **one container image**. The role is selected by the
container `args` the platform manifests already pass — there is no per-role
image, no per-role build, and no drift between them.

| Role | What it is | `args` |
|---|---|---|
| 🔌 MCP tool servers | seven per-domain servers, streamable HTTP at `/mcp` on `:8080` | `mcp --domain=<domain> --listen=:8080` |
| 🧠 Agent runtime | the plan–act–observe loop, its operators and its HTTP surface | `runtime --config=/etc/adhar-ai/config.yaml --listen=:8080` |
| 🚪 LLM gateway | provider-agnostic, OpenAI-compatible — **local development only** | `gateway --listen=:8080` |

`src/adhar_ai/cli.py` is the whole of that dispatch. CI pushes the single build
under **eight published names** (`adhar-ai-runtime` plus
`adhar-ai-mcp-<domain>` for each of the seven domains). There is deliberately no
`adhar-ai-gateway` name: ADR-0025 retired that Deployment in favour of upstream
agentgateway, and the `gateway` subcommand survives only for `docker compose`
and a bare laptop run.

Two more entrypoints exist and deploy nothing: `adhar-ai tools` prints the tool
inventory as JSON (the Go-CLI contract, diffed against `contract/tools.json` in
CI) and `adhar-ai index` re-indexes the docs tree into pgvector.

### The seven MCP domains

One Deployment and Service per domain, all running the same image with a
different `--domain`. The split is not organisational — it is the unit of
authorization. `GITEA_WRITE_ENABLED` is true on exactly the four domains that
carry a PR-authoring tool, and agentgateway's authorization rules key on the
same four names.

| Domain | Read tools | Write tool | Backend |
|---|---|---|---|
| `cluster` | `list_pods`, `describe`, `get_events`, `logs`, `resource_health` | — | Kubernetes API |
| `gitops` | `app_status`, `sync_status`, `app_diff` | `propose_change` | ArgoCD REST, Kubernetes fallback |
| `provision` | `list_xrs`, `xr_status` | `propose_xr` | Crossplane XRs via Kubernetes |
| `observability` | `promql`, `logql`, `traceql`, `slo_burn`, `correlate` | — | Prometheus, Loki, Tempo |
| `security` | `findings`, `policy_explain`, `posture` | `propose_exception` | Kyverno + PolicyReports |
| `cost` | `cost_by`, `budget_status`, `showback` | — | OpenCost |
| `catalog` | `search_packages`, `template_params` | `scaffold` | Gitea tree + ArgoCD inventory |

Twenty-seven tools, four of them writes. Full argument-level reference:
[TOOLS.md](TOOLS.md).

### Why Python, when the Adhar core is Go

ADR-0024 §1 states the trade plainly: the agent, MCP and LLM ecosystem is
overwhelmingly Python — the official `mcp` SDK, the provider SDKs, the embedding
stack — while Adhar's core is Go. Cramming an agent runtime into the Go binary
would fight the ecosystem and bloat a core whose bootstrap boundary is
deliberately narrow (ADR-0006).

So Adhar AI is a separate repository that integrates through the platform's
*normal* contracts rather than through shared code: it ships as a standard
package delivered by the ApplicationSet (ADR-0004), wired to SSO (ADR-0008),
secrets (ADR-0009) and the Gateway like any other service. The Go CLI (`adhar
ai`) is a thin HTTP client over `/chat` with no LLM logic in it. The stable
contract between the two repositories is **the MCP tool schemas plus the `/chat`
API**, not a library.

It runs on the **control plane** only (ADR-0023). Data planes need no AI
components.

---

## 🔀 3. Request paths

Three callers, three paths. What differs between them is where identity is
checked and who calls the LLM.

### (a) An interactive question through `/chat`

```
  Console chat  /  `adhar ai "why is the console app degraded?"`
        │  Authorization: Bearer <Keycloak JWT>
        │  POST https://agent.<host>/chat   {"prompt": ..., "autonomy"?, "model"?}
        ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ adhar-gateway  (Cilium edge, terminates TLS, *.<host> wildcard cert)     │
  └──────────────────────────────────────────────────────────────────────────┘
        │  backendRef: Service adhar-ai-runtime:8080
        ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ agent runtime  —  runtime/app.py                                         │
  │                                                                          │
  │  1. AuthPolicy.principal(request)        ◄══ IDENTITY CHECKED HERE       │
  │       JWT verified against the realm JWKS; `groups` -> write_allowed     │
  │       no credential -> anonymous Principal, write_allowed = False        │
  │                                                                          │
  │  2. Retriever.grounding(prompt, k=5)     pgvector cosine, else BM25      │
  │                                                                          │
  │  3. autonomy = principal.ceiling(lower_of(request, ConfigMap default))   │
  │                                                                          │
  │  4. loop.run()  — plan / act / observe, <= maxSteps, <= maxToolCalls     │
  │       │                                                                  │
  │       ├── LLM ──► POST $LLM_GATEWAY_URL/chat/completions                 │
  │       │      body NAMES a model; X-Adhar-Tenant + Authorization          │
  │       │                        ▲                                         │
  │       │                        ╚══ LLM CALLED HERE (key never in-proc)   │
  │       │                                                                  │
  │       └── tools ─► MCPToolbox: streamable HTTP /mcp, one session per     │
  │                    domain Service, held for the process lifetime         │
  └──────────────────────────────────────────────────────────────────────────┘
        ▼
   {"kind": "answer" | "proposed" | "budget_exhausted" | "error",
    "grounded_on": [...], "principal": {...}, "autonomy": "..."}
```

Note the runtime reaches the seven MCP servers **directly** by ClusterIP, using
the `mcpServers` map in the `adhar-ai-config` ConfigMap — not through the
federated endpoint. Federation exists for external clients; the runtime is
already inside the trust boundary and would only add a hop.

The LLM hop goes to whatever `LLM_GATEWAY_URL` names. In the platform that is
`http://adhar-ai-gateway.adhar-system.svc.cluster.local:8080/v1` — the
agentgateway proxy, which holds the provider key server-side. Locally it is this
repo's own `adhar-ai gateway`. The wire format is identical, so the only
difference the client sees is the base URL, normalized by
`config.openai_v1_base()` to exactly one `/v1`. The loop always names a model,
because under agentgateway the model name *is* the routing key; a body with no
model lands on the gateway's unconditional fallback rule instead of being routed
deliberately.

That call carries `X-Adhar-Tenant` **and an `Authorization` bearer**. It has to:
agentgateway runs `jwtAuthentication: Strict` across the whole Gateway, so a
completion request with no token is a 401 and the loop cannot run. The token is
the caller's own JWT where there is one — agentgateway meters per-group token
budgets from its `groups` claim, so a user's run must be billed to the user —
and otherwise the runtime's Keycloak service-account token, minted through the
`adhar-ai` client's client-credentials grant and cached until shortly before it
expires. Against the bundled local-dev gateway, which requires no token, the
header is simply absent.

### (b) An external agent (Claude Code, an IDE) through the federated `/mcp`

```
  Claude Code / IDE assistant / ChatOps
        │  Authorization: Bearer <Keycloak JWT>
        │  https://mcp.<host>/mcp          (MCP, streamable HTTP)
        ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ adhar-gateway  (Cilium edge, TLS)                                        │
  │   /mcp  -> forwarded unchanged     /  -> URLRewrite to /mcp              │
  └──────────────────────────────────────────────────────────────────────────┘
        │  backendRef: Service adhar-ai-gateway:8080
        ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ agentgateway  —  the AI data plane (ADR-0025)                            │
  │                                                                          │
  │   jwtAuthentication: Strict          ◄══ IDENTITY CHECKED HERE, ONCE,    │
  │     issuer = public realm URL            for every AI route              │
  │     JWKS  = in-cluster keycloak:8080                                     │
  │     preserveToken: true                                                  │
  │                                                                          │
  │   authorization: two merged Allow rules over `jwt.groups`                │
  │     rule 1  developer|admin  AND  mcp.tool.target NOT in the 4 write     │
  │     rule 2  admin            (unconditional — readmits the other 4)      │
  │                                                                          │
  │   AgentgatewayBackend `adhar-mcp`:  prefixMode Always, FailOpen,         │
  │     Stateful session routing — seven tool lists merged into one          │
  └──────────────────────────────────────────────────────────────────────────┘
        │              │              │            │           │
        ▼              ▼              ▼            ▼           ▼
   mcp-cluster   mcp-observability  mcp-cost   mcp-gitops  mcp-{provision,
     :8080/mcp        :8080/mcp     :8080/mcp   :8080/mcp    security,catalog}

   NO LLM IS CALLED ON THIS PATH.  The model lives in the external client;
   Adhar contributes only governed tools.
```

Tool names arrive prefixed by their server (`gitops_propose_change`) because
`prefixMode: Always` makes the name stable regardless of how many targets are
healthy — which is what the authorization CEL keys on. The MCP servers
themselves validate nothing: ADR-0025 item 6 removed per-server OIDC, so the
manifests no longer set `OIDC_ISSUER_URL` on them. They still *receive* the
caller's token (`preserveToken: true`) for the RBAC-scoped read exchange.

This path is the point of ADR-0024 §7: a developer's own agent drives Adhar
through the identical governed tools, with one URL and one token, and no
Adhar-specific client code.

### (c) An Alertmanager alert reaching an operator

```
  Alertmanager                     (http_config.authorization.credentials)
        │  Authorization: Bearer <shared webhook token>
        │  POST https://agent.<host>/operators/alert-triage/event
        ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ adhar-gateway (Cilium edge, TLS)  ->  Service adhar-ai-runtime:8080      │
  └──────────────────────────────────────────────────────────────────────────┘
        ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ agent runtime                                                            │
  │                                                                          │
  │  AuthPolicy.principal(request, allow_webhook_token=True)                 │
  │      hmac.compare_digest against the shared token   ◄══ IDENTITY HERE    │
  │      no credential -> answered, but pinned to read-only                  │
  │                                                                          │
  │  AlertTriage(ctx).handle(event, principal)                               │
  │      autonomy = principal.ceiling(                                       │
  │                   lower_of(operator policy, ConfigMap default))          │
  │      grounding over the runbooks, then loop.run()                        │
  │           ├── LLM ──► $LLM_GATEWAY_URL      ◄══ LLM CALLED HERE          │
  │           └── tools ─► correlate, promql, logql, app_status,             │
  │                        propose_change (only if the stage permits)        │
  │                                                                          │
  │  -> Finding  ->  in-memory deque (200)  +  FindingStore (Postgres)       │
  └──────────────────────────────────────────────────────────────────────────┘
```

Two more operators are driven by internal pollers rather than by a webhook, so
no identity is involved at all: `_drift_poller` calls the `sync_status` tool
every 300s and hands fresh OutOfSync applications to **drift-explain**;
`_cost_poller` calls `cost_by` daily and hands the snapshot to **cost-advisor**.
Both reach the platform through the MCP tools rather than through a second set
of credentials. **upgrade-preflight** is manual.

Whether a finding becomes a pull request is the stage's decision, never the
model's — see [§6](#-6-staged-autonomy).

---

### (d) A task that outlives its request, and changes hands

`/chat` answers inside the request. `/tasks` does not: it returns an id and the
work continues behind a bounded worker pool.

```
  Console / Slack / a pull request / a chore
        │  POST /tasks {"prompt": "…"}
        ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ agent runtime                                                            │
  │                                                                          │
  │  AuthPolicy.principal(request)   ◄══ IDENTITY HERE                       │
  │  autonomy = principal.ceiling(lower_of(request, ConfigMap))              │
  │                                                                          │
  │  stage in (approve-to-apply, scoped)?                                    │
  │      └─ plan_first():  read-only run, writes a numbered plan, ACTS NOT   │
  │            -> state=awaiting_approval, NOT enqueued  ◄══ HUMAN GATE      │
  │                                                                          │
  │  TaskStore.save()  ──►  agent_task (Postgres)   or memory + a warning    │
  │  TaskQueue.submit() ──►  asyncio.Queue, 3 workers                        │
  └───────────────────────────────┬──────────────────────────────────────────┘
        202-ish: {"id": "task-…"} │      (the caller is done here)
                                  ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ worker  ->  Orchestrator.execute(task)                                   │
  │                                                                          │
  │  AgentRegistry.route(prompt)        lexical, NO model call               │
  │  stage = agent.ceiling_for(stage)   ◄══ NARROWS AGAIN, never widens      │
  │  grounding = KnowledgeBase.grounding(prompt)                             │
  │  history  = ConversationStore.open(task.session)    (a thread remembers) │
  │                                                                          │
  │  loop.run(session with agent.tools only)                                 │
  │       ├── LLM ──► $LLM_GATEWAY_URL                                       │
  │       └── tools ─► only what this agent declared                         │
  │                                                                          │
  │  answer starts "HANDOFF: <agent> — <reason>"?                            │
  │       ├─ check_handoff(): declared colleague? reason given? depth < 4?   │
  │       │     └─ refused -> generalist, task.error records why             │
  │       └─ accepted -> task.hand_to(); LOOP AGAIN as a NEW run             │
  │                                                                          │
  │  CoverageLog.observe_run()   -> a gap if it went badly                   │
  │  TaskStore.save()            -> done | failed                            │
  └──────────────────────────────────────────────────────────────────────────┘
```

Three properties are worth stating because each is a deliberate cost:

**The worker pool is bounded.** An agent run is expensive, so an unbounded pool
turns a burst of queued questions into a burst of concurrent inference spend —
which is the first thing an operator would cap anyway.

**A plan-gated task is not enqueued.** It is saved and left alone. Enqueueing it
would have a worker pick it up and run the plan nobody approved.

**Interrupted work is resumed, not resurrected.** At start-up, tasks left in
`queued`, `planning` or `running` are re-enqueued with `error` set to say they
were interrupted. The run starts over; what it already did is in the audit
stream, which is the record.

---

## ✍️ 4. The write path

There is exactly one. Every write tool in every domain — `propose_change`,
`propose_xr`, `propose_exception`, `scaffold` — renders its change into a list
of `{path, content}` and hands it to `mcp/common/pr.py::open_pr`. Nothing else
in the codebase mutates anything.

```
  model emits a tool call: propose_change(repo, changes, title, why)
        │
        ▼
  ┌── runtime/loop.py::_invoke ──────────────────────────────────────────────┐
  │  1. is this a write tool, and does this session's autonomy permit        │
  │     one?   no -> {"error": "denied: ... read-only"}                      │
  │           audited, and NOT dispatched                                    │
  │  2. session.write_refusal(args)                                          │
  │       -> WritePolicy.refusal(level, repo, paths)                         │
  │     out of scope -> {"error": "denied by writePolicy: ..."}              │
  │           audited, and NOT dispatched                                    │
  └──────────────────────────────────────────────────────────────────────────┘
        │  dispatched over MCP streamable HTTP
        ▼
  ┌── mcp/common/pr.py::open_pr ─────────────────────────────────────────────┐
  │  3. guard_write(cfg, repo, paths)   BEFORE ANY NETWORK CALL              │
  │       GITEA_WRITE_ENABLED?  bot token present?  repo allowed?            │
  │       >= 1 file?  normalize_path (no `..`, no absolute)?                 │
  │       repo-qualified path inside allowedPathPrefixes?                    │
  │       any failure -> WriteNotPermitted -> ToolError the model reads      │
  │                                                                          │
  │  4. Gitea REST, and only Gitea REST:                                     │
  │       create_branch  adhar-ai/<slug>-<short-id>  from main               │
  │       put_file       per change, commit trailer carries                  │
  │                      Proposed-by / Audit-Id / Requested-by / origin      │
  │       create_pull_request  "[adhar-ai] <title>"  label `adhar-ai`        │
  │                                                                          │
  │  5. emit(decision="proposed", audit_id, repo, pr, url, files, user)      │
  └──────────────────────────────────────────────────────────────────────────┘
        │  PRRef {repo, number, url, branch, files}
        ▼
  human reviews and merges  ->  ArgoCD reconciles  ->  the change is live
```

The PR body states the safety property in every single pull request: that
opening a PR is the agent's only write path, that it holds no cluster-mutating
credential, and that it never calls `kubectl apply`, `argocd app sync` or a
cloud mutation API.

### Four layers, and why the guarantee is structural

| Layer | Where it runs | What it stops |
|---|---|---|
| `writePolicy` check | `runtime/loop.py::_invoke`, before dispatch | an out-of-scope repo or path, refused where the ConfigMap is actually read |
| `guard_write` | `mcp/common/pr.py`, before any HTTP request | the same thing again, in the process that holds the Gitea token |
| read-only ServiceAccount | `adhar-ai-readonly` ClusterRole | any cluster mutation at all — the role has `get`/`list`/`watch` and nothing else |
| Kyverno `adhar-ai-guardrails` | admission, control plane | the `adhar-ai` SA creating or updating a platform workload directly; unlabelled AI-created objects |

The two policy checks are duplicated **on purpose**, and the comment in
`runtime/autonomy.py` says why: the MCP server needs its own copy because it is
the process holding the credential, and the runtime needs one because that is
where the ConfigMap is read — without it, editing `writePolicy` in
`adhar-ai-config` would change what `GET /config` prints and nothing else.

But the layers are defence in depth around a property that does not depend on
them. The guarantee is **structural**:

- No mutating tool exists. There is no `kubectl_apply`, `argo_sync`,
  `helm_install`, `scale`, `patch`, `rollback` or `exec` tool in any of the
  seven domains, so there is no apply path for a prompt injection to hijack.
- `clients/kube.py` exposes no create/patch/delete surface at all. The read
  client is the only Kubernetes client in the codebase.
- `mcp/common/pr.py` imports no Kubernetes, AWS, Azure or GCP client.
- The ServiceAccount could not apply anything even if a tool tried. The one
  namespaced `Role` the agent holds (`adhar-ai-self`) covers two named
  ConfigMaps, read-only access to its own two secrets, and event creation.

A setting cannot raise this, because there is no setting. Turning every knob to
its most permissive value still leaves an agent whose only write verb is "open a
pull request".

**The test that asserts it** is `tests/test_tool_registry.py`, singled out by
the design doc (§9, §12) — if it fails, the agent has gained a write path that
does not go through a pull request:

- `test_no_mutating_tool_exists_anywhere` — a forbidden-name list is absent from
  every domain.
- `test_every_write_tool_routes_through_open_pr` — a **source-level** assertion:
  each write tool's body is read with `inspect.getsource`, must contain
  `open_pr(`, and must not contain `apply`, `kubectl`, `create_namespaced`,
  `patch_` or `delete_`.
- `test_open_pr_only_touches_gitea` — the write module's source contains no
  `kubernetes`, `boto3`, `azure`, `google.cloud` or `kubectl`.
- `test_access_tags_match_the_manifests` — the set of write tools per domain
  agrees with `GITEA_WRITE_ENABLED` in `mcp-servers.yaml`.

Threat model in full: [SECURITY.md](SECURITY.md).

---

## 🏛️ 5. Control plane / data plane

ADR-0024 answered *how does an agent act safely?* ADR-0025 answered the question
that followed it: *who governs the traffic?* The original design put token
validation, group checks and budget accounting in seven Python services, and left
the controls that matter most — content filtering, per-identity token budgets,
MCP multiplexing, OTel GenAI spans, a model cost catalog — unbuilt, as a
multi-quarter project that is not differentiated platform work.

So those controls moved out of this repository and into
[agentgateway](https://agentgateway.dev), a Rust data plane configured through
Gateway API plus four CRDs.

| Moved to the data plane (`ai/agentgateway`) | Stayed here (`adhar-ai`) |
|---|---|
| Keycloak JWT validation, once, for every AI route | the plan–act–observe loop and its budgets on steps and tool calls |
| Per-tool authorization over `jwt.groups` | the four operators and their triggers |
| Prompt/response guardrails (credential masking, PII audit) | the staged-autonomy ladder and the `writePolicy` gate |
| Per-group request and token budgets | grounding: chunking, embedding, retrieval |
| OTel GenAI telemetry to Tempo, metrics to Grafana | the 27 tools and their backends |
| MCP federation: seven servers, one `/mcp` endpoint | the single `open_pr` write path and its provenance |
| Provider keys and model-name routing | structured audit events on every tool call |

The alternatives ADR-0025 rejected are worth knowing: finishing the bespoke
gateway (a permanent maintenance burden on a security-critical component
tracking a spec that revised twice in 2026), LiteLLM (an LLM proxy only — no MCP
federation, so the MCP half would need a second component and a second policy
language), Envoy AI Gateway (weaker MCP story, and a second Envoy control plane
alongside Cilium's), and putting the controls in Cilium's Envoy (protocol-
agnostic: it cannot count tokens, parse a `tools/call`, or run a prompt guard).

### The authorization table

Group names come from the platform Keycloak realm and are the same groups that
drive Gitea teams and Kubernetes RBAC, so AI authority and cluster authority
cannot drift apart.

| Group | LLM completions | MCP read tools (`cluster`, `observability`, `cost`) | MCP write tools (`gitops`, `provision`, `security`, `catalog`) |
|---|---|---|---|
| `platform-developer` | ✅ | ✅ | ❌ |
| `platform-admin` | ✅ | ✅ | ✅ |
| `platform-viewer` / none | ❌ | ❌ | ❌ |

`platform-viewer` is intentionally ungranted: an LLM call spends real money,
which is more than a viewer should be able to do. "Write" still means *opens a
Gitea PR* — ADR-0024's guarantee is untouched and is now defended a second time
at the gateway. The rules are `Allow`-only, so the default is deny and an
erroring CEL expression can lock people out but cannot let anyone in.

agentgateway sits **behind** the Cilium edge and does not terminate TLS: one
certificate lifecycle, one host-port mapping, one place to look, and nothing
lost, because its value is protocol awareness and that works identically one hop
in.

### The runtime is the exception

`https://agent.<host>/` does **not** pass through the data plane. The runtime
publishes its own hostname so the Console and `adhar ai` can reach `/chat`
directly, and so Alertmanager and ArgoCD notifications can reach
`/operators/{name}/event` — neither of which holds an OIDC client.

`runtime/auth.py` is what keeps the runtime from being the hole ADR-0025 would
otherwise leave behind. It authenticates for itself, against the same Keycloak
`adhar-ai` client whose `groups` mapper the gateway's CEL rules read:

| Caller | Credential | Route |
|---|---|---|
| People (Console, `adhar ai`) | Keycloak JWT, verified against the realm JWKS | `/chat` |
| Machines (Alertmanager, ArgoCD notifications) | shared webhook bearer, compared with `hmac.compare_digest` | `/operators/{name}/event` |

The issuer is checked against the **public** realm URL byte-for-byte while the
JWKS may be fetched from the **in-cluster** Keycloak Service — the same split
ADR-0025 specifies for agentgateway, so key retrieval never depends on the
platform's own ingress being up.

The design choice that matters most here is what happens with **no credential**.
Refusing outright would break `docker compose up` and every local run; serving
normally would leave an unauthenticated path to an LLM run that opens real PRs
at the shipped `suggest` stage. So:

> an unauthenticated caller is answered, but is pinned to `read-only`.

Investigation stays open to anyone who can reach the port; *authority* requires a
credential. `ADHAR_AI_REQUIRE_AUTH=true` refuses instead, which is the right
posture once the platform's own clients are wired up.

---

## 🪜 6. Staged autonomy

The ladder lives in `runtime/autonomy.py` and is read from the `adhar-ai-config`
ConfigMap. Each rung differs from the one below it in a way you can **observe**,
not merely in its name.

| Stage | Concrete behavioural difference |
|---|---|
| `read-only` | `toolbox.specs(include_writes=False)` withholds write tools from the model entirely, and `_invoke` refuses one anyway if it is somehow called |
| `suggest` *(shipped default)* | write tools are offered; `Session.stop_after_write` is true, so the **first** pull request ends the run |
| `approve-to-apply` | the run **continues** after a pull request, so the agent can verify its own proposal and propose a coherent set; a human still merges every one |
| `scoped` | runs unattended, and is therefore confined to the **narrower** `writePolicy.scoped` allow-list |

`stop_after_write` is the whole difference between the middle two rungs. In
`loop.run`, as soon as a tool result yields a pull request, a `suggest` session
sets `kind = "proposed"` and breaks out of the loop. The reasoning in the source
is that a human being asked to review should be shown **one** proposal, not
whatever the model decided to chain onto it. Above that rung the run continues so
the agent can check its own work.

`scoped` fails closed. `WritePolicy.scope_for(level)` returns the *scoped* repo
and prefix lists at that stage, and both ship **empty**, so `refusal()` returns:

> autonomy `scoped` has no configured scope on this runtime, so it permits no
> write at all; set writePolicy.scoped in adhar-ai-config

Unattended PRs are opt-in by enumeration. Raising the stage without naming the
scope refuses every write and says which field to set, rather than silently
inheriting the broad allow-list.

### Authority only narrows

Four inputs decide the stage a run executes at, and they combine with `min`,
never with "last writer wins":

```
   ConfigMap `autonomy.default`   ── the ceiling
              │
              ├─ operator policy (per operator, may be stricter)
              │        lower_of(policy.autonomy, cfg.default_autonomy)
              │
              ├─ the request may ask for LESS
              │        lower_of(body.autonomy, cfg.default_autonomy)
              │
              └─ the caller's principal pins the result
                       principal.ceiling(x)  ->  "read-only" unless write_allowed
                       ▼
                the stage the run actually executes at
```

`lower_of()` returns `LADDER[min(rank(l) for l in levels)]`. `Principal.ceiling()`
returns `read-only` for any caller that is unauthenticated *or* authenticated but
outside the write groups, whatever the ConfigMap said. Combining with `min` is
what stops a request body from widening its own authority. A typo is caught at
load time — `RuntimeConfig.from_mapping` calls `rank()` on every level so an
unknown stage raises rather than silently widening.

There is deliberately **no rung at which the model chooses its own scope**. The
allow-list is read from the ConfigMap, never from a tool argument, because an
argument is something a prompt injection can set. And every rung above
`read-only` still produces a Git pull request; none of them mutates a cluster,
because that property is structural rather than configured.

The `Finding` an operator emits records the stage the run **actually** executed
at, which a caller's ceiling may have lowered — not the one the ConfigMap asked
for.

---

## 📚 7. Grounding

Retrieval runs over the platform's own docs, ADRs, runbooks and incident notes,
mounted at `ADHAR_AI_DOCS_PATH`. `rag/index.py` chunks on Markdown headings (max
2000 chars, oversized sections hard-wrapped on paragraphs) and keeps the heading
path in each chunk's `source`, so a citation points at a section
(`docs/adr/0024-….md#Decision`) rather than at a file.

There are two retrieval paths, and `Retriever.search()` tries them in order.

**Vector — the primary path.** Chunks are embedded through the LLM gateway's
`/v1/embeddings` into the `kb_chunk` table on the `adhar-ai-rag` CNPG cluster,
which the package's `postInitApplicationSQL` creates with `vector(1536)` and an
HNSW index on `vector_cosine_ops`. A query embeds and runs
`embedding <=> %s::vector` ordered ascending — cosine distance, top-k. Used
whenever a DSN and an embedding backend are both available.

**Lexical — BM25, in process.** `rag/lexical.py` builds a BM25 index (K1=1.5,
B=0.75, Robertson/Sparck-Jones IDF floored at zero) over the *same* chunks, from
the same docs tree, holding it in memory. It is used when the vector path returns
nothing — and that includes the empty case, not just the exception case, because
an empty pgvector table and a broken one look identical from the caller's side.

### Why the lexical path exists at all

Because the alternative was a silent lie. The README promised that an unkeyed
platform degrades to lexical search rather than pretending to be unavailable; in
practice every failure path returned `[]`, so an unkeyed platform answered from
the model's prior with no Adhar grounding and **no sign that anything was
missing**. A silent `[]` is indistinguishable, to a caller, from "the docs say
nothing about this".

It covers exactly the situations vector search cannot:

- no LLM provider key, so there is no embedding endpoint to call;
- no CNPG database — which is every `docker compose` and every bare
  `adhar-ai runtime`;
- pgvector reachable but empty, mid-first-index;
- a database that has started failing, where silently ungrounded answers are the
  worst outcome.

BM25 rather than substring matching because the queries are natural-language
questions against prose, where term saturation and length normalization are most
of what makes ranking work. No stemmer: on technical text the terms that matter
are identifiers, and the tokenizer keeps `kube-system` and `app.kubernetes.io`
whole.

The bootstrap order reflects which path is more reliable. `_bootstrap_rag` builds
the lexical index first and publishes a retriever immediately — it needs neither
a key nor a database, so the runtime is grounded from the first request — then
upgrades that retriever in place if pgvector is available. If pgvector fails, the
lexical retriever stays and `/healthz` reports the downgrade rather than a loss.

Every `Hit` records which path produced it, `as_grounding()` states it in the
block the model sees (`### source (kind, lexical)`), and `GET /healthz` reports
the live mode. Operators run grounded too — a triage run needs the runbooks more
than an interactive question does, because nobody is there to supply the missing
context.

---

## 🗃️ 8. State

Most of the runtime is stateless. What state exists is worth knowing precisely,
because it determines the replica count.

| State | Where it lives | Lifetime |
|---|---|---|
| Findings | `deque(maxlen=200)` in `app.state.findings` | process |
| Findings | `finding` table, `adhar-ai-rag` CNPG database | durable, 30-day retention |
| Budget ledger | `gateway/budget.py`, in-process dict per tenant | process, per gateway replica |
| Response cache | `gateway/cache.py`, in-process TTL'd LRU | 300s, 256 entries, per replica |
| MCP sessions | `MCPToolbox`, one per domain in an `AsyncExitStack` | process |
| Lexical index | in memory, built from the docs tree at start-up | process |
| Tasks | `agent_task` table, `adhar-ai-rag` CNPG database | durable, 30-day retention |
| Tasks (no database) | `OrderedDict` in `TaskStore`, capped at 500 | process — and `/healthz` says `durable: false` |
| Conversations | `ConversationStore`, bounded LRU per replica | 4 turns, 30 minutes idle |
| Coverage gaps | `CoverageLog`, bounded to 300 | process |
| Chore schedule | last-run timestamps in `ChoreRegistry` | process |

### Findings: memory plus Postgres

A finding is often the only artifact a `read-only` operator produces, and it is
what an on-call engineer comes back to read. As a pure in-process deque, a
restart, a rollout or a second replica lost every one of them.

`runtime/store.py` reuses the CNPG database the RAG index already runs on, so
nothing new is provisioned. At start-up `prepare()` creates the table, prunes
rows older than 30 days (a finding is a point-in-time judgement about a cluster
that has since moved on), and the last 200 are loaded back into the deque
*before* serving — so a rollout does not look to an on-call engineer like
"nothing happened".

Persistence is an upgrade, never a dependency. With no DSN configured — the
default for `docker compose` and a bare `adhar-ai runtime` — every method is a
no-op and the deque is the whole story. Every failure degrades to memory and
`/healthz` reports which mode is live (`findings_store`), rather than pretending
findings are durable.

Note the read asymmetry: `GET /findings` serves the **in-memory deque**. The
database is the restart-survival mechanism, not a query surface.

### Budgets and cache are per replica

`BudgetLedger` holds per-tenant daily tokens and a rolling one-minute request
window in process. `ResponseCache` is a bounded TTL'd LRU keyed on a digest of
the whole request — model, every message, tool specs, tool choice — **per
tenant**, and only for `temperature == 0`, non-streaming requests. Anything else
is asking the provider for variation, and serving a stored answer would silently
take it away; sharing across tenants would turn a cache into a disclosure
channel, since a completion is derived from one caller's prompt.

The interfaces are deliberately narrow so moving either to Redis is a one-file
change.

### The consequence of more than one replica

Every manifest in the package declares `replicas: 1`, and that is load-bearing:

- **Budgets multiply.** Two gateway replicas mean a tenant gets two daily token
  allowances and two rate-limit windows. ADR-0025 notes the same property for
  agentgateway's own `conditional` local rate limits, which are per proxy
  replica; true per-identity budgets need an external rate-limit service the
  platform does not yet run.
- **Cache hit rate splits.** Two replicas, two caches, roughly half the hits.
- **Pollers duplicate.** `_drift_poller` and `_cost_poller` run in every replica
  with independent `seen` sets, so the same drift produces a finding — and, above
  `read-only`, potentially a pull request — once per replica.
- **`GET /findings` diverges.** Each replica serves its own deque, so the same
  request answered by a different replica shows a different list. The durable
  store reconciles them only at start-up.

Scaling the runtime out therefore needs leader election for the pollers and
shared state for the ledger; it is not a replica-count change. Operational
detail: [OPERATIONS.md](OPERATIONS.md).

---

## 🧱 9. Key design decisions

| Decision | Alternative rejected | Why |
|---|---|---|
| Writes are Gitea pull requests only | direct `kubectl apply` / `argocd app sync` from the agent | an agent with an apply path and a broad token is a prompt injection away from a catastrophe; PR-only makes the agent's authority exactly a contributor's, reviewable and revertible (ADR-0024 §3) |
| An MCP-native layer acting through existing control surfaces | a Console chatbot calling the Kubernetes API directly | the chatbot is either read-only and of limited value, or holds broad write credentials and sits outside GitOps, bypassing review, policy and drift detection (ADR-0024 Context) |
| Self-hosted, one configurable key | a hosted SaaS copilot | conflicts with the 100%-open-source, no-phone-home principle and leaks platform state to a third party (ADR-0024 Context) |
| A separate Python repository | an agent runtime inside the Go core | the MCP/agent/LLM ecosystem is Python; cramming it into Go would fight the ecosystem and bloat a core whose bootstrap boundary is deliberately narrow (ADR-0024 §1) |
| One image, role selected by `args` | one image per role | eight Deployments from one build: no drift between roles, no per-role pipeline, and the manifests already pass the args |
| Seven per-domain MCP servers | one server with all 27 tools | the domain is the unit of authorization — `GITEA_WRITE_ENABLED` and agentgateway's CEL rules both key on it |
| agentgateway as the AI data plane | finish building `adhar-ai-llm-gateway` | a multi-quarter build of undifferentiated proxy features, then permanent maintenance of a security-critical component tracking a spec that revised twice in 2026 (ADR-0025) |
| agentgateway over LiteLLM | LiteLLM as the LLM proxy | an LLM proxy only: no MCP federation, no A2A, no Gateway API integration, no tool-level authorization — half the problem and a second runtime (ADR-0025) |
| agentgateway behind the Cilium edge | agentgateway as a second terminating edge | one TLS story, one host port, one place to look; nothing is lost because protocol awareness works identically one hop in (ADR-0025 §7) |
| Model-name routing on stable Gateway API primitives | the native `AgentgatewayModel` CRD | upstream marks it experimental; a PreRouting policy plus header matches uses only stable primitives (ADR-0025 §2) |
| Policy enforced twice (runtime *and* MCP server) | enforce once, in the server holding the token | without the runtime check, editing `writePolicy` in the ConfigMap changed what `GET /config` printed and nothing else |
| `scoped` autonomy ships with an empty allow-list | inherit the broad `allowedRepos`/`allowedPathPrefixes` | `scoped` opens PRs unattended; inheriting would be a silent widening, so it fails closed and names the field to set |
| Unauthenticated callers answered, pinned to `read-only` | refuse outright / serve normally | refusing breaks every local run; serving normally leaves an unauthenticated path to real PRs at the `suggest` default |
| DNS-rebinding guard off by default on MCP | the SDK's localhost-only allow-list | agentgateway reaches the servers by Service `backendRef`, so the Host header is un-enumerable; the SDK default answers `421` to every real caller and takes out the whole federated tool surface. The real boundary is the gateway's JWT check |
| MCP federation `FailOpen`, `prefixMode: Always` | `FailClosed`, `Conditional` prefixes | one rolling Deployment among seven should cost you one domain's tools, not the session; and a tool name that changes with how many servers are up breaks both client caches and the authorization CEL |
| Lexical BM25 fallback for grounding | return `[]` when pgvector is unavailable | a silent `[]` is indistinguishable from "the docs say nothing"; an unkeyed platform answered ungrounded with no sign anything was missing |
| Findings persisted to the existing RAG database | a new datastore / memory only | reuses what is already provisioned; memory alone lost every finding on a restart, which is the wrong property for a `read-only` operator's only output |
| Response cache limited to `temperature == 0`, per tenant | cache everything | anything else asks the provider for variation, and cross-tenant sharing turns a cache into a disclosure channel |
| Audit events on stdout as JSON lines | a Loki write credential in-process | Alloy scrapes container stdout into Loki, so the audit trail is free and the agent holds no write credential for the observability stack |
| Unkeyed is a *reported* state | crash-loop without a key | the platform must run unaffected until an operator sets the key; `/healthz` says `keyed: false` and the gateway answers 503 on completions (ADR-0024 §9) |
| An agent's ceiling applied before the caller's | one autonomy stage per request | the narrowing belongs to the ROLE, not the request: a `read-only` guide must stay read-only for an administrator at `scoped`, or specialisation is cosmetic |
| Handoff is a new run, not a continuation | replay the message history to the receiving agent | continuing would carry the previous agent's tool results into a session that was never allowed to call those tools |
| Lexical routing between agents | ask a model which agent should answer | spending a completion to decide who spends a completion doubles latency and cost on every request, and misroutes in ways nobody can debug; a keyword miss is a one-line ConfigMap change |
| Keyword matching on PREFIX, not whole words | `\b…\b` word boundaries | platform vocabulary inflects constantly — crashloop/crashlooping, deploy/deployed — and a whole-word match on the stem misses most real questions |
| A refused handoff falls back to the generalist | fail the task | refusing is not failing; the generalist can answer anything, so one agent's over-eager forwarding should not kill the work |
| Tasks degrade to memory without a database | require Postgres for `/tasks` | `docker compose` and a bare `adhar-ai runtime` have no database, and a task that lives for the process is still far more useful than one that lives for the connection — the loss is stated in `/healthz`, not assumed |
| Plan-and-approve only above `suggest` | always write a plan first | at `read-only` and `suggest` nothing lands without a pull request a human merges, so the run is already reviewable and a plan step is ceremony that costs a completion |
| Approval needs a write-capable credential | any authenticated caller may approve | a task at `awaiting_approval` is one whose stage can change the platform; releasing it is exactly as privileged as writing, or the gate is decoration |
| Conversations in process, tasks in Postgres | persist both / persist neither | a conversation is short-lived working context worth a few minutes of latency saving, so a database round trip per turn costs more than it returns; a TASK is what must survive a restart |
| Chores ship individually disabled and in dry-run | a single `automation: enabled` flag | "turn on automation" is not a decision anyone can reason about; `certificate-expiry` is — and a chore that opens twelve pull requests on a Monday gets the whole layer switched off |
| An unknown chore name is ignored with a warning | create it from the config | a typo must not be able to bring unreviewed automation into existence |
| Coverage gaps classified mechanically | ask a model to grade its own answers | expensive, and unreliable in the one direction that matters — a model that produced a bad answer is not well placed to notice |
| Knowledge graph as recursive CTEs in Postgres | a dedicated graph database | one datastore, one backup, one credential; the queries a blast-radius answer needs are a bounded traversal, not a graph workload |
| Blast radius follows dependency relations only | traverse every edge | following everything returns the whole connected component, which for a platform is very nearly everything and therefore answers nothing |
| Graph entity resolution is exact-match | fuzzy name matching | a blast radius computed from the wrong node is confidently wrong, which is the one failure mode the module exists to avoid |
| Expected failures raised as MCP `ToolError` | let them surface as generic crashes | the SDK withholds a crash's text, so the model could not tell "Prometheus is not configured here" from "the tool crashed" — and the instruction to say so plainly had nothing to say it from |

---

## 📖 Where to go next

| Guide | What it covers |
|---|---|
| 🚀 [Getting Started](GETTING_STARTED.md) | From `uv sync` to a grounded answer to an opened pull request |
| 🤖 [Agents & Automation](AGENTS.md) | The specialist roster, durable tasks, approvals, chores and journeys |
| 🧰 [Tool Reference](TOOLS.md) | Every one of the 27 tools: arguments, backend, failure mode |
| ⚙️ [Operations](OPERATIONS.md) | Every setting, the health surface, and how to diagnose it |
| 🔐 [Security](SECURITY.md) | Threat model, the write path, authentication, autonomy, prompt injection |
