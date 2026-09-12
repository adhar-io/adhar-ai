# 🚀 Getting Started with Adhar AI

From a fresh clone to a grounded answer about a real cluster, and then to an
opened pull request. Every command here has been run; every output shown is real.

**Sanskrit: अधार (Adhāra) – Foundation**

> **Read tools read. Write tools open a pull request. Nothing applies to a
> cluster.** Keep that in mind as you go: nothing in this guide can change a
> running cluster, whatever you ask it to do.

---

## Contents

1. [Prerequisites](#1-prerequisites)
2. [Install and verify](#2-install-and-verify)
3. [Meet the tools, with no cluster at all](#3-meet-the-tools-with-no-cluster-at-all)
4. [Run one MCP server](#4-run-one-mcp-server)
5. [Run the whole stack with Docker Compose](#5-run-the-whole-stack-with-docker-compose)
6. [Run it by hand, seven servers and a runtime](#6-run-it-by-hand-seven-servers-and-a-runtime)
7. [Ask it something](#7-ask-it-something)
8. [Point it at a real cluster](#8-point-it-at-a-real-cluster)
9. [Turn on the LLM](#9-turn-on-the-llm)
10. [Let it open a pull request](#10-let-it-open-a-pull-request)
11. [Connect your own agent](#11-connect-your-own-agent)
12. [Deploy it on the platform](#12-deploy-it-on-the-platform)
13. [Where to go next](#13-where-to-go-next)

---

## 1. Prerequisites

| You need | Why | Check |
|---|---|---|
| Python 3.12+ | the agent and MCP ecosystem is Python | `python3 --version` |
| [uv](https://docs.astral.sh/uv/) | the project's package manager | `uv --version` |
| Docker *(optional)* | for the Compose stack and the image | `docker version` |
| An Adhar cluster *(optional)* | to read real state instead of reporting "not configured" | `kubectl get ns adhar-system` |
| An LLM API key *(optional)* | to run the agent loop rather than just the tools | — |

Only the first two are required. Everything below degrades honestly without the
rest: a tool whose backend is unconfigured **says so** and never invents a
result.

---

## 2. Install and verify

```bash
git clone https://github.com/adhar-io/adhar-ai
cd adhar-ai
uv sync --extra rag
```

The `rag` extra pulls in `psycopg` and `pgvector`. It is optional in principle,
but it is what CI installs, so install it and your local runs match CI.

Run everything CI runs:

```bash
uv run pytest -q                                       # 260 tests
uv run ruff check src tests
uv run mypy src
uv run adhar-ai tools | diff -u contract/tools.json -  # the Go-CLI contract
```

All four must pass on a clean checkout. The last one is not a formatting check:
`contract/tools.json` is the schema snapshot the Adhar Go CLI is written
against, so a diff there is a breaking change, not a refactor.

---

## 3. Meet the tools, with no cluster at all

The fastest way to see what Adhar AI can do needs nothing running:

```bash
uv run adhar-ai tools
```

```json
{
  "cluster": [
    {"name": "list_pods", "access": "read"},
    {"name": "describe", "access": "read"},
    {"name": "get_events", "access": "read"},
    {"name": "logs", "access": "read"},
    {"name": "resource_health", "access": "read"}
  ],
  "gitops": [
    {"name": "app_status", "access": "read"},
    {"name": "sync_status", "access": "read"},
    {"name": "app_diff", "access": "read"},
    {"name": "propose_change", "access": "write"}
  ]
}
```

Twenty-seven tools across seven domains, four of them writes. Count them:

```bash
uv run adhar-ai tools | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("domains:", len(d), " tools:", sum(len(v) for v in d.values()))
for k, v in d.items():
    writes = [t["name"] for t in v if t["access"] == "write"]
    print(f"  {k:14s} {len(v)} tools  writes={writes}")'
```

```
domains: 7  tools: 27
  cluster        5 tools  writes=[]
  gitops         4 tools  writes=['propose_change']
  provision      3 tools  writes=['propose_xr']
  observability  5 tools  writes=[]
  security       4 tools  writes=['propose_exception']
  cost           3 tools  writes=[]
  catalog        3 tools  writes=['scaffold']
```

Look for `kubectl_apply`, `argo_sync` or `helm_install` and you will not find
them. That is not an omission to be filled in later — a test asserts each of
those names is absent from every domain, and another asserts at the source level
that every write tool's body reaches `open_pr` and contains no `apply`,
`kubectl`, `patch_` or `delete_` call.

👉 Every tool's arguments and return shape: **[TOOLS.md](TOOLS.md)**

---

## 4. Run one MCP server

```bash
uv run adhar-ai mcp --domain cluster --listen=:8081
```

```bash
curl -s localhost:8081/healthz | jq
```

```json
{
  "status": "ok",
  "domain": "cluster",
  "server": "adhar-cluster",
  "write_enabled": false,
  "write_path": "gitea-pull-request-only"
}
```

`write_enabled: false` is correct here — `cluster` is a read-only domain, and
the platform only sets `GITEA_WRITE_ENABLED=true` on the four that carry a PR
tool.

The MCP endpoint itself is at `/mcp`, speaking streamable HTTP:

```bash
curl -s -X POST localhost:8081/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
        "protocolVersion":"2025-06-18","capabilities":{},
        "clientInfo":{"name":"curl","version":"1"}}}'
```

> **A note on the `Host` header.** These servers accept any `Host` by default,
> which is deliberate. The MCP SDK's transport ships a DNS-rebinding guard
> allowing localhost only — right for a laptop, wrong in a Pod, where
> agentgateway reaches the server by Service DNS name or Pod IP and every such
> request would be answered `421 Invalid Host header`. DNS rebinding is a
> browser attack against a loopback-bound server, and there is no browser on a
> Pod's loopback; the real boundary is agentgateway's Keycloak validation in
> front of all seven. Set `ADHAR_AI_MCP_ALLOWED_HOSTS` to a comma-separated list
> to switch the guard back on.

---

## 5. Run the whole stack with Docker Compose

One command brings up the local gateway, all seven MCP servers, the runtime and
a pgvector Postgres:

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # or leave unset for read-only mode
docker compose up --build
```

| Service | Port | What it is |
|---|---|---|
| runtime | 8080 | the agent: `/chat`, `/findings`, operator webhooks |
| gateway | 8081 | the local-dev LLM gateway |
| mcp-cluster … mcp-catalog | 8090–8096 | the seven tool servers |
| rag-db | 5432 | pgvector, for grounding |

```bash
curl -s localhost:8080/healthz | jq '{mcp_servers_connected, rag, auth}'
curl -s localhost:8081/healthz | jq
```

The runtime mounts `deploy/config.yaml` at exactly the path the platform
Deployment mounts its ConfigMap, so local behaviour matches the cluster. It also
mounts the Adhar platform docs for grounding; point `ADHAR_DOCS_PATH` at your
checkout if it is not a sibling directory.

---

## 6. Run it by hand, seven servers and a runtime

Useful when you are changing the code and do not want a rebuild between edits.

```bash
port=18101
for d in cluster gitops provision observability security cost catalog; do
  uv run adhar-ai mcp --domain "$d" --listen=":$port" &
  port=$((port+1))
done
```

Write a config pointing at them:

```yaml
# /tmp/adhar-ai/config.yaml
autonomy:
  default: suggest
limits:
  maxSteps: 8
  maxToolCallsPerOp: 20
writePolicy:
  allowedRepos: [packages, environments]
  allowedPathPrefixes: ["packages/", "environments/"]
mcpServers:
  cluster:       http://127.0.0.1:18101
  gitops:        http://127.0.0.1:18102
  provision:     http://127.0.0.1:18103
  observability: http://127.0.0.1:18104
  security:      http://127.0.0.1:18105
  cost:          http://127.0.0.1:18106
  catalog:       http://127.0.0.1:18107
rag:
  enabled: true
```

Start the runtime against it, pointing the grounding index at the platform docs:

```bash
ADHAR_AI_DOCS_PATH=../adhar/docs \
LLM_GATEWAY_URL=http://127.0.0.1:18100 \
  uv run adhar-ai runtime --config /tmp/adhar-ai/config.yaml --listen=:18110
```

```bash
curl -s localhost:18110/healthz | jq
```

```json
{
  "status": "ok",
  "autonomy_default": "suggest",
  "mcp_servers_connected": ["catalog","cluster","cost","gitops",
                            "observability","provision","security"],
  "mcp_servers_unreachable": {},
  "tools": ["app_diff","app_status","budget_status","correlate","cost_by",
            "describe","findings","get_events","list_pods","list_xrs","logql",
            "logs","policy_explain","posture","promql","propose_change",
            "propose_exception","propose_xr","resource_health","scaffold",
            "search_packages","showback","slo_burn","sync_status",
            "template_params","traceql","xr_status"],
  "rag": "lexical only (1051 chunks) — no embeddings or database configured",
  "auth": {
    "oidc": "disabled",
    "webhookToken": "unset",
    "requireAuth": false,
    "writeGroups": ["platform-admin"],
    "unauthenticated": "answered as read-only"
  },
  "findings_held": 0,
  "findings_store": "disabled (no database)",
  "adhar.io/origin": "adhar-ai"
}
```

Read that health block closely, because it tells you exactly what you have:

- **`mcp_servers_connected`** lists all seven. A server that is down appears in
  `mcp_servers_unreachable` with its error, and costs you only its own tools.
- **`rag`** says `lexical only`. With no key and no database, grounding still
  works: an in-process BM25 index over the docs tree answered with 1,051 real
  chunks. This is the path that makes an unkeyed platform useful rather than
  merely quiet.
- **`auth`** says `unauthenticated: answered as read-only`. Anyone who can reach
  this port can investigate. Nobody can make it open a pull request.
- **`findings_store`** says `disabled`. Operator findings live in memory and a
  restart loses them. Configure a database and it says `ready (finding)`.

---

## 7. Ask it something

```bash
curl -s localhost:18110/chat \
  -H 'content-type: application/json' \
  -d '{"prompt":"which applications are out of sync, and why?"}' | jq
```

Without a key configured the loop reports the gateway is unreachable rather than
guessing — that is the design. What you *can* exercise with no key is the tool
layer itself, which is where the platform knowledge lives. Call a tool directly
through the MCP client:

```bash
uv run python - <<'PY'
import anyio
from adhar_ai.runtime.toolbox import MCPToolbox

SERVERS = {"cluster": "http://127.0.0.1:18101",
           "observability": "http://127.0.0.1:18104"}

async def main():
    tb = MCPToolbox(SERVERS)
    await tb.connect()
    print(await tb.call("promql", {"query": "up"}))
    await tb.aclose()

anyio.run(main)
PY
```

```
{'error': 'Error executing tool promql: BackendNotConfigured: Prometheus is not
configured for this Adhar AI deployment (set PROMETHEUS_URL)'}
```

That message is the whole point. The tool did not return an empty series, or a
plausible-looking number, or a bare "error". It named the backend and the
environment variable that fixes it — so the agent can tell you that Prometheus
is not wired up, instead of hallucinating a metric.

---

## 8. Point it at a real cluster

Port-forward an Adhar cluster, or run this in one:

```bash
kubectl -n adhar-system port-forward svc/gitea-http 3000:3000 &
kubectl -n adhar-system port-forward svc/argo-cd-argocd-server 8090:80 &
kubectl -n adhar-system port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090 &
```

```bash
export ADHAR_AI_GITEA_API_URL=http://localhost:3000/api/v1
export ADHAR_AI_GITEA_ORG=adhar
export ADHAR_AI_ARGOCD_URL=http://localhost:8090
# ArgoCD credentials have no ADHAR_AI_-prefixed alias — these are the names.
export ARGOCD_USERNAME=admin
export ARGOCD_PASSWORD=...
export ADHAR_AI_PROMETHEUS_URL=http://localhost:9090
```

Restart the affected MCP servers and the same `promql` call now returns a real
series. Every setting has an `ADHAR_AI_`-prefixed name and an unprefixed
fallback, which is how the platform manifests inject them.

Two behaviours worth knowing:

- **`gitops` degrades sideways, not down.** With no ArgoCD credentials it reads
  the `Application` custom resources through the Kubernetes API instead. You
  still get status and health; you lose `app_diff`, which honestly reports
  `diff_available: false` with the reason rather than returning an empty diff.
- **Reads are RBAC-scoped.** The Kubernetes client has read verbs only — no
  create, patch or delete method exists on it — and object data is stripped of
  `managedFields` and the last-applied-configuration blob before it can reach a
  prompt.

---

## 9. Turn on the LLM

For local development, run the bundled gateway:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
uv run adhar-ai gateway --listen=:18100
```

```bash
curl -s localhost:18100/healthz | jq
curl -s localhost:18100/v1/models | jq '.data[].id'
```

Unkeyed, the gateway answers **503 with an actionable message** rather than
crash-looping. That is deliberate: an unkeyed platform should look unkeyed, not
broken.

Now the chat endpoint works end to end:

```bash
curl -s localhost:18110/chat \
  -H 'content-type: application/json' \
  -d '{"prompt":"why is the console application degraded?"}' | jq '{kind, autonomy, text, tool_calls: [.tool_calls[].tool], grounded_on}'
```

The reply carries `grounded_on` — the doc sections that were retrieved and put
in front of the model — and `tool_calls`, the tools it actually ran. Both are
there so you can check its work.

**In the platform this gateway is not deployed.** The AI data plane is
[agentgateway](https://agentgateway.dev), which holds the keys server-side,
routes on the model name and enforces per-group budgets. The wire format is
identical, so the only thing that changes is `LLM_GATEWAY_URL`.

---

## 10. Let it open a pull request

This is the only way Adhar AI changes anything. Give the `gitops` server a Gitea
bot token with commit rights and turn writes on:

```bash
export GITEA_WRITE_ENABLED=true
export GITEA_BOT_USER=adhar-ai-bot
export GITEA_BOT_TOKEN=...
export GITEA_WRITE_REPOS=packages,environments
uv run adhar-ai mcp --domain gitops --listen=:18102
```

Ask for a change:

```bash
curl -s localhost:18110/chat \
  -H 'content-type: application/json' \
  -d '{"prompt":"the console deployment has no memory limit. propose a fix."}' \
  | jq '{kind, autonomy, pull_requests}'
```

```json
{
  "kind": "proposed",
  "autonomy": "read-only",
  "pull_requests": []
}
```

**`autonomy: read-only`, and no pull request.** That is not a bug — you are an
unauthenticated caller. Investigation is open; authority is not. Give the
runtime a credential and the configured stage comes back:

```bash
export ADHAR_AI_WEBHOOK_TOKEN=$(openssl rand -hex 24)
# restart the runtime, then:
curl -s localhost:18110/operators/upgrade-preflight/event \
  -H "Authorization: Bearer $ADHAR_AI_WEBHOOK_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"target":"1.31"}' | jq '{autonomy, title, pull_request}'
```

In production the human callers present a Keycloak JWT instead, and only the
groups in `ADHAR_AI_WRITE_GROUPS` (default `platform-admin`) may drive a write.

### What the four stages actually do

| Stage | Write tools offered | After a pull request | Repo/path scope |
|---|---|---|---|
| `read-only` | no, and refused if called | — | — |
| `suggest` *(default)* | yes | **run stops** — one proposal for a human | `writePolicy` |
| `approve-to-apply` | yes | run **continues** — the agent can verify its own work | `writePolicy` |
| `scoped` | yes | run continues, unattended | `writePolicy.scoped`, **empty by default** |

Raising the stage to `scoped` without enumerating a scope does not widen
anything. It refuses every write and tells you which field to set:

```
denied by writePolicy: autonomy `scoped` has no configured scope on this
runtime, so it permits no write at all; set writePolicy.scoped in adhar-ai-config
```

Authority only ever narrows: the ConfigMap sets a ceiling, an operator policy
may be stricter, a request may ask for less, and an unauthenticated caller is
pinned to `read-only` regardless of all three.

---

## 11. Connect your own agent

The seven servers are ordinary MCP servers, so Claude Code, an IDE assistant or
a ChatOps bot drives Adhar through the identical governed tools. On a platform
with `ai/agentgateway` enabled, that is one URL and one token:

```json
{
  "mcpServers": {
    "adhar": {
      "type": "http",
      "url": "https://mcp.<your-host>/mcp",
      "headers": { "Authorization": "Bearer <keycloak-token>" }
    }
  }
}
```

Tool names arrive prefixed by their server — `gitops_propose_change`,
`cluster_list_pods` — because agentgateway federates with `prefixMode: Always`,
so a tool's name does not change depending on how many servers happen to be up.

Locally, point at a single server instead:

```json
{ "mcpServers": { "adhar-cluster": { "type": "http",
  "url": "http://localhost:18101/mcp" } } }
```

Your agent gets the same 27 tools, the same read/write tagging, and the same
guarantee: the only write it can perform is opening a pull request.

---

## 12. Deploy it on the platform

The `ai/adhar-ai` package is **opt-in and disabled by default**, so an Adhar
platform runs entirely unaffected until someone turns it on.

1. **Supply a key.** One secret is the whole switch:

   ```bash
   vault kv put secret/adhar-ai/llm \
     PROVIDER=anthropic \
     ANTHROPIC_API_KEY=sk-ant-... \
     MODEL=claude-sonnet-5
   ```

2. **Supply the bot identity** that opens pull requests:

   ```bash
   vault kv put secret/adhar-ai/bot \
     GITEA_BOT_USER=adhar-ai-bot \
     GITEA_BOT_TOKEN=<gitea token, repo scope>
   ```

3. **Enable the packages.** Set `ai/adhar-ai` and `ai/agentgateway` to enabled in
   your ApplicationSet. They are wired to the same flag on purpose: agentgateway
   is what serves the LLM and federated MCP endpoints.

4. **Wire Alertmanager** to the operator webhook. The token is generated for
   you, so read it back rather than inventing one:

   ```bash
   kubectl -n adhar-system get secret adhar-ai-webhook \
     -o jsonpath='{.data.WEBHOOK_TOKEN}' | base64 -d
   ```

You then have three endpoints:

| | URL |
|---|---|
| 🚪 LLM completions | `https://ai.<host>/v1/chat/completions` |
| 🔌 Federated MCP | `https://mcp.<host>/mcp` |
| 🧠 Agent runtime | `https://agent.<host>/` |

⏳ **One gate remains.** The container images are not yet published to GHCR. The
workflow that builds, signs and SBOMs them is committed and its smoke test
passes locally, but it has not been run, so the Deployments have nothing to
pull. See [Project status](../README.md#-project-status).

---

## 13. Where to go next

| Guide | Read it when |
|---|---|
| 🧰 **[TOOLS.md](TOOLS.md)** | you want a tool's exact arguments and failure modes |
| 🏛️ **[ARCHITECTURE.md](ARCHITECTURE.md)** | you want to know why it is shaped this way |
| ⚙️ **[OPERATIONS.md](OPERATIONS.md)** | you are configuring or debugging a deployment |
| 🔐 **[SECURITY.md](SECURITY.md)** | you are deciding whether to enable this for real |
| 🤝 **[CONTRIBUTING.md](../CONTRIBUTING.md)** | you are adding a tool or an operator |

Questions and design discussion happen in
[GitHub Discussions](https://github.com/adhar-io/adhar/discussions) and the
[Adhar Slack](https://join.slack.com/t/adharworkspace/shared_invite/zt-26586j9sx-QGrIejNigvzGJrnyH~IXww).

<div align="center">
<sub>Adhar • Built with ❤️ for developers!</sub>
</div>
