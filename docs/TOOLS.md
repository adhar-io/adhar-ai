# 🧰 Tool Reference

Adhar AI exposes **27 tools across 7 domains**. Each domain is its own MCP
server — one container image, seven Deployments, differing only by
`ADHAR_AI_MCP_DOMAIN`.

| Domain | Server name | Read tools | Write tools |
|---|---|---|---|
| `cluster` | `adhar-cluster` | `list_pods`, `describe`, `get_events`, `logs`, `resource_health` | — |
| `gitops` | `adhar-gitops` | `app_status`, `sync_status`, `app_diff` | `propose_change` |
| `provision` | `adhar-provision` | `list_xrs`, `xr_status` | `propose_xr` |
| `observability` | `adhar-observability` | `promql`, `logql`, `traceql`, `slo_burn`, `correlate` | — |
| `security` | `adhar-security` | `findings`, `policy_explain`, `posture` | `propose_exception` |
| `cost` | `adhar-cost` | `cost_by`, `budget_status`, `showback` | — |
| `catalog` | `adhar-catalog` | `search_packages`, `template_params` | `scaffold` |

**23 reads, 4 writes.**

Read tools call the real platform APIs: Kubernetes, ArgoCD, Gitea, Prometheus,
Loki, Tempo, Kyverno PolicyReports, OpenCost.

Write tools do exactly one thing: **create a branch in Gitea, commit the files,
and open a pull request**. They never apply anything. There is no
`kubectl_apply`, `argo_sync`, `helm_install` or cloud-mutation tool anywhere in
`src/adhar_ai/mcp/`, and `src/adhar_ai/clients/kube.py` exposes no
create/patch/delete surface at all. A human merges the PR; ArgoCD reconciles
afterwards, exactly as it would for a human contribution.

Every tool carries an `adhar/access` tag (`read` or `write`) in its MCP `_meta`,
plus the standard `readOnlyHint` annotation so generic MCP clients see the same
distinction. `destructive_hint` is `False` on both kinds — a write opens a PR, it
destroys nothing.

The machine-readable contract is printed by the CLI and checked in:

```bash
uv run adhar-ai tools                                   # JSON: {domain: [{name, access}]}
uv run adhar-ai tools | diff -u contract/tools.json -   # CI fails on any drift
```

`adhar-ai tools` builds each of the seven servers in-process and lists their
tools, so it needs no cluster and no credentials.

---

## 🔌 How to connect

### The federated endpoint

In the platform, the seven servers sit behind
[agentgateway](https://agentgateway.dev) as **one** MCP endpoint:

```
https://mcp.<host>/mcp
```

agentgateway multiplexes all seven tool lists, validates the Keycloak token once
in front of them, and authorizes per tool — read tools require
`platform-developer`, the PR-opening tools require `platform-admin`.

Because the gateway is configured with `prefixMode: Always`, every tool name
arrives namespaced by its server:

```
cluster_list_pods      gitops_propose_change      security_findings
cluster_logs           gitops_sync_status         cost_cost_by
```

That prefixed name is what the gateway's authorization rules key on, so do not
strip it. The bare name (`list_pods`) is what the server itself registers and
what `contract/tools.json` records.

The gateway sets `preserveToken: true`, so the caller's bearer token still
reaches the server and RBAC-scoped reads run as the user.

### A single server, directly

For local work, run one domain and talk to it over streamable HTTP:

```bash
uv run adhar-ai mcp --domain cluster --listen=:8080   # :8080/mcp
```

`--listen` defaults to `:8080`. Tool names are **unprefixed** here — there is no
gateway to add one. Two extra routes sit on the same port:

```bash
curl -s localhost:8080/healthz
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

`/readyz` returns `{"status": "ok", "domain": "cluster"}`.

`docker compose up` brings up all seven on fixed host ports: `8090` cluster,
`8091` gitops, `8092` provision, `8093` observability, `8094` security, `8095`
cost, `8096` catalog.

### Transport security

The MCP SDK's DNS-rebinding guard defaults to a localhost-only allow-list, which
rejects every real caller in a Pod. `MCPConfig.allowed_hosts` therefore defaults
to `("*",)`, which turns the guard off; the real boundary is agentgateway in
front. Set `ADHAR_AI_MCP_ALLOWED_HOSTS` (or `MCP_ALLOWED_HOSTS`) to a
comma-separated host list to switch it back on when a server is exposed some
other way.

### Backends and their environment variables

Every setting has an `ADHAR_AI_`-prefixed name and an unprefixed fallback; the
unprefixed name is read first.

| Backend | Variables | Used by |
|---|---|---|
| Kubernetes | in-cluster ServiceAccount, else local kubeconfig. `ADHAR_AI_KUBE_DISABLED=1` forces the failure | `cluster`, `provision`, `security`, `gitops` fallback, `catalog` fallback |
| ArgoCD | `ARGOCD_URL`, plus `ARGOCD_AUTH_TOKEN`/`ARGOCD_TOKEN` **or** `ARGOCD_PASSWORD`/`ARGOCD_ADMIN_PASSWORD` (`ARGOCD_USERNAME` defaults to `admin`, `ARGOCD_VERIFY_TLS` defaults to `false`) | `gitops`, `catalog` |
| Gitea | `GITEA_API_URL`, `GITEA_ORG` (default `adhar`), `GITEA_BOT_TOKEN`, `GITEA_WRITE_ENABLED`, `GITEA_WRITE_REPOS` | `catalog` reads, all four writes |
| Prometheus | `PROMETHEUS_URL` | `observability` |
| Loki | `LOKI_URL` | `observability` |
| Tempo | `TEMPO_URL` | `observability` |
| OpenCost | `OPENCOST_URL` | `cost` |

ArgoCD counts as configured only when `url` **and** (`token` or `password`) are
set. When only a password is present the client mints a session token via
`POST /api/v1/session` on first use.

---

## ☸️ cluster

**Backend:** the Kubernetes API, through the `adhar-ai-readonly` ClusterRole
(`get`/`list`/`watch` only).

**Unconfigured:** the client tries in-cluster config, then the local kubeconfig.
If neither works, or `ADHAR_AI_KUBE_DISABLED=1` is set, every tool on this server
fails with `BackendNotConfigured: Kubernetes is not configured for this Adhar AI
deployment (...)`.

Objects are sanitised before they reach the model: `managedFields` and the
`kubectl.kubernetes.io/last-applied-configuration` annotation are stripped.

This server has **no write tool**.

### `list_pods`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `namespace` | `str \| None` | `None` | Restrict to one namespace; omit for cluster-wide |
| `label_selector` | `str \| None` | `None` | Standard Kubernetes selector, e.g. `app.kubernetes.io/part-of=adhar-ai` |

Returns `{"count": int, "pods": [...]}`. Each pod is summarised as `name`,
`namespace`, `phase`, `node`, `pod_ip`, `start_time`, `restarts` (summed across
containers), `ready` (`"1/2"`), `labels`, and a `containers` list of
`{name, ready, restarts, state, reason, image}` — where `state` is the single key
of the container's state map and `reason` falls back to `lastState.terminated.reason`.

```json
{
  "count": 2,
  "pods": [
    {
      "name": "adhar-ai-runtime-5c9f7d6b4-2xq9z",
      "namespace": "adhar-system",
      "phase": "Running",
      "node": "adhar-worker-1",
      "pod_ip": "10.244.2.17",
      "start_time": "2026-09-12T07:41:03Z",
      "restarts": 0,
      "ready": "1/1",
      "containers": [
        {"name": "runtime", "ready": true, "restarts": 0, "state": "running",
         "reason": null, "image": "ghcr.io/adhar-io/adhar-ai:0.1.0"}
      ],
      "labels": {"app.kubernetes.io/part-of": "adhar-ai"}
    }
  ]
}
```

### `describe`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `namespace` | `str` | required | Pod namespace |
| `name` | `str` | required | Pod name |

Returns the full sanitised pod object (spec and status), not a summary. Use it
after `list_pods` has narrowed the field; it is the most token-expensive tool
here.

### `get_events`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `namespace` | `str \| None` | `None` | Restrict to one namespace |
| `involved_object` | `str \| None` | `None` | Filter to one object name; becomes the field selector `involvedObject.name=<value>` |

Returns `{"count": int, "events": [...]}` with each event flattened to `type`,
`reason`, `message`, `count`, `last_timestamp` (falling back to `eventTime`),
`namespace`, and `object` rendered as `Kind/name`.

### `logs`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `namespace` | `str` | required | Pod namespace |
| `name` | `str` | required | Pod name |
| `container` | `str \| None` | `None` | Container name; omit for the pod's default container |
| `tail_lines` | `int` | `200` | Lines from the end of the log |

Returns `{"pod": "<namespace>/<name>", "container": <container>, "lines": [...]}`.
`container` is echoed exactly as passed, so it is `null` when omitted.

**Failure mode:** a multi-container pod with no `container` argument fails in the
Kubernetes client, not here — and that failure is not one of the anticipated
types, so the model sees only a generic crash. Name the container.

### `resource_health`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `namespace` | `str \| None` | `None` | Restrict to one namespace |

Lists Deployments and pods and correlates them. Returns:

```json
{
  "workloads_total": 41,
  "workloads_degraded": [
    {"name": "backstage", "namespace": "adhar-system",
     "replicas_desired": 2, "replicas_ready": 1, "replicas_available": 1,
     "updated": 2, "conditions": [{"type": "Available", "status": "False", "reason": "MinimumReplicasUnavailable"}]}
  ],
  "pods_unhealthy": [],
  "healthy": false
}
```

A workload is **degraded** when `replicas_desired > replicas_ready`. A pod is
**unhealthy** when its phase is neither `Running` nor `Succeeded`, **or** its
restart count is greater than zero — so a pod that recovered after one crash
still shows up here. `healthy` is true only when both lists are empty.

---

## 🔁 gitops

**Backend:** the ArgoCD REST API when it is keyed, the Kubernetes API otherwise.

**Unconfigured (graceful degradation):** when `ARGOCD_URL` plus a credential is
missing, `app_status` and `sync_status` fall back to reading the
`argoproj.io/v1alpha1` `Application` CRs directly from the **`adhar-system`**
namespace — the read-only ClusterRole grants `get`/`list`/`watch` on them. The
REST payload and the CR have identical shapes, so the tool output is the same
either way. `app_diff` cannot degrade and says so rather than guessing.

### `app_status`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `app` | `str` | required | ArgoCD Application name |

Returns the flattened application summary: `name`, `namespace`, `project`,
`sync_status`, `health_status`, `health_message`, `revision`, `repo_url`, `path`,
`target_revision`, `destination`, `last_operation_phase`,
`last_operation_message`, `conditions`. `repo_url`/`path`/`target_revision` come
from `spec.source`, falling back to the first entry of `spec.sources`.

### `sync_status`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `only_unhealthy` | `bool` | `False` | Return only applications that are not `Synced` or not `Healthy` |

Returns the fleet inventory. Note that `count` reflects the list **after**
filtering:

```json
{
  "count": 2,
  "out_of_sync": 2,
  "degraded": 1,
  "applications": [
    {"name": "vault", "sync_status": "OutOfSync", "health_status": "Healthy", "...": "..."},
    {"name": "backstage", "sync_status": "OutOfSync", "health_status": "Degraded", "...": "..."}
  ]
}
```

`degraded` counts applications whose `health_status` is neither `Healthy` nor
`null`, so an application with no health block yet is not counted as degraded.

### `app_diff`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `app` | `str` | required | ArgoCD Application name |

Calls ArgoCD's `/managed-resources` feed and returns only the entries that carry
a `diff`:

```json
{
  "app": "vault",
  "diff_available": true,
  "count": 1,
  "changed": [
    {"kind": "ConfigMap", "name": "vault-config", "namespace": "adhar-system", "diff": "..."}
  ]
}
```

**When ArgoCD is unkeyed it reports the gap honestly instead of falling back:**

```json
{
  "app": "vault",
  "diff_available": false,
  "reason": "app_diff needs the ArgoCD REST API: set ARGOCD_URL and a credential (ARGOCD_AUTH_TOKEN, or ARGOCD_PASSWORD from the argocd-credentials secret)"
}
```

This is a normal successful result, not an error — the caller must check
`diff_available` before reading `changed`.

### `propose_change` ✍️

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `repo` | `str` | required | `"packages"` or `"environments"` |
| `changes` | `list[dict[str, str]]` | required | `[{"path": "...", "content": "..."}]` — **full file contents**, not a patch |
| `title` | `str` | required | Short PR title; `[adhar-ai]` is prefixed automatically |
| `why` | `str` | required | Rationale, recorded verbatim in the PR body and in every commit message |

The generic write tool: it writes whatever files you give it, subject to the
policy gate below. Returns the PR reference:

```json
{
  "repo": "packages",
  "number": 412,
  "url": "https://gitea.example.com/adhar/packages/pulls/412",
  "branch": "adhar-ai/raise-vault-memory-limit-9f3c1a",
  "files": ["security/vault/manifests/values.yaml"]
}
```

Missing keys are tolerated: a change with no `path` is checked as `""` (and
rejected by `normalize_path`), and a change with no `content` writes an empty
file.

---

## 🏗️ provision

**Backend:** the Kubernetes API only. Crossplane v2 composite resources are
namespaced, in the group `platform.adhar.io/v1alpha1`.

**Unconfigured:** same as `cluster` — no Kubernetes access means
`BackendNotConfigured: Kubernetes ...` on every tool.

The plural for a kind is derived mechanically from its lowercased name: a name
ending in `s` gets `es`, a name ending in `y` becomes `ies`, everything else gets
`s`. So `CompositeCluster` → `compositeclusters`. A kind whose real plural does
not follow that rule will 404 at the API server.

### `list_xrs`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `kind` | `str` | required | e.g. `CompositeCluster`, `CompositeDatabase`, `CompositeApplication` |
| `namespace` | `str \| None` | `None` | Omit for all namespaces |

Returns `{"kind": ..., "count": int, "items": [...]}` where each item is
`kind`, `name`, `namespace`, `ready`, `synced`, `conditions`, `created`.
`ready` and `synced` are pulled from the `Ready` and `Synced` conditions and
default to the string `"Unknown"` when absent.

### `xr_status`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `kind` | `str` | required | Composite kind |
| `name` | `str` | required | Resource name |
| `namespace` | `str` | `"adhar-system"` | Namespace of the XR |

Returns the same summary plus `spec` and `resource_refs` (from
`status.resourceRefs`).

### `propose_xr` ✍️

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `kind` | `str` | required | Composite kind, used verbatim as the manifest's `kind` |
| `name` | `str` | required | `metadata.name` |
| `spec` | `dict[str, Any]` | required | The XR's `spec`, passed through unmodified |
| `why` | `str` | required | Rationale for the PR body |
| `namespace` | `str` | `"adhar-system"` | `metadata.namespace` |
| `repo` | `str` | `"packages"` | Target repo |
| `path` | `str \| None` | `None` | Override the file path |

Renders:

```yaml
apiVersion: platform.adhar.io/v1alpha1
kind: CompositeDatabase
metadata:
  name: orders-db
  namespace: adhar-system
  labels:
    adhar.io/origin: adhar-ai
spec:
  size: small
```

and opens it as a PR. The default path is
`infrastructure/crossplane-xrs/manifests/<name>-<kind lowercased>.yaml`. The PR
title is `provision <kind> <name>` before the `[adhar-ai]` prefix is added.

Nothing is applied. ArgoCD creates the XR only after a human merges.

---

## 📈 observability

**Backend:** Prometheus (`/api/v1/query`, `/api/v1/query_range`,
`/api/v1/alerts`), Loki (`/loki/api/v1/query_range`), Tempo (`/api/search`).

**Unconfigured:** each backend is resolved lazily by URL. A missing URL raises
`BackendNotConfigured: Prometheus is not configured for this Adhar AI deployment
(set PROMETHEUS_URL)` — and likewise `LOKI_URL`, `TEMPO_URL`. Nothing is
invented to fill the gap. The one exception is `correlate`, which catches per-
backend failures so a partial answer still comes back.

This server has **no write tool**.

### `promql`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `query` | `str` | required | PromQL expression |
| `range_minutes` | `int` | `0` | `0` for an instant query; `>0` for a range query over the last N minutes ending now |
| `step` | `str` | `"60s"` | Range-query resolution (ignored for instant queries) |

Returns `{"query": ..., "status": ..., "series": [...]}` where `status` is
Prometheus's own `status` field and each series is
`{"metric": {...}, "value": [ts, "v"], "values": [[ts, "v"], ...]}` — `value` is
populated for instant queries, `values` for range queries, and the other is
`null`.

```json
{
  "query": "sum(rate(http_requests_total[5m]))",
  "status": "success",
  "series": [{"metric": {}, "value": [1789000000, "42.7"], "values": null}]
}
```

### `logql`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `query` | `str` | required | LogQL, e.g. `{namespace="adhar-system"} \|= "error"` |
| `minutes` | `int` | `60` | Look-back window ending now |
| `limit` | `int` | `100` | Entries per stream; also passed to Loki as its `limit` |

The query runs `direction=backward`. Returns
`{"query", "stream_count", "streams": [{"labels": {...}, "entries": [...]}]}`.
`limit` is applied twice: Loki caps the response, and each stream's entries are
sliced to `limit` again on the way out.

### `traceql`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `query` | `str` | required | TraceQL, e.g. `{ duration > 2s }` |
| `limit` | `int` | `20` | Maximum traces |

Returns `{"query": ..., "traces": [...]}`, where `traces` is Tempo's `traces`
field, falling back to `metrics`, falling back to `[]`.

### `slo_burn`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `slo_metric` | `str` | required | A PromQL **ratio** expression yielding the GOOD-event rate in `[0,1]`. The literal token `WINDOW` is substituted per window |
| `objective` | `float` | `0.99` | The SLO target |
| `windows` | `str` | `"5m,1h,6h"` | Comma-separated windows; blanks are dropped and each entry is stripped |

One instant query is issued per window. `error_budget` is `1 - objective` and
`burn_rate` is `(1 - good_ratio) / error_budget`, rounded to 3 decimals.

```json
{
  "objective": 0.99,
  "error_budget": 0.01,
  "windows": [
    {"window": "5m", "good_ratio": 0.972, "burn_rate": 2.8,
     "query": "sum(rate(http_requests_total{code!~\"5..\"}[5m])) / sum(rate(http_requests_total[5m]))"}
  ]
}
```

`good_ratio` and `burn_rate` are `null` when the query returned no usable value
(no series, or a non-numeric sample). `burn_rate` is also `null` when
`objective` is `1.0` or higher, because the budget is then zero.

An expression without the `WINDOW` token is still valid — it is simply queried
unchanged once per window, giving identical rows.

### `correlate`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `namespace` | `str` | required | Namespace of the workload |
| `workload` | `str` | required | Workload name; matched as the pod prefix `<workload>.*` and as a log substring |
| `minutes` | `int` | `30` | Look-back for all three sources |

The usual first call in an alert triage. It runs three queries and packs them
into one object:

- `restarts` — `sum by (pod) (increase(kube_pod_container_status_restarts_total{namespace="<ns>",pod=~"<workload>.*"}[<minutes>m]))`
- `logs` — the raw Loki payload for `{namespace="<ns>"} |= "<workload>"`, limit `50`
- `alerts` — Prometheus alerts, then filtered to those whose label `namespace`
  matches, or whose labels mention the workload anywhere

**Each source degrades independently.** A backend that is unconfigured or
erroring produces `{"unavailable": "BackendNotConfigured: Loki is not configured
for this Adhar AI deployment (set LOKI_URL)"}` under its own key, and the other
two still return:

```json
{
  "namespace": "adhar-system",
  "workload": "backstage",
  "minutes": 30,
  "restarts": {"status": "success", "data": {"...": "..."}},
  "logs": {"unavailable": "BackendNotConfigured: Loki is not configured for this Adhar AI deployment (set LOKI_URL)"},
  "alerts": [{"labels": {"alertname": "KubePodCrashLooping", "namespace": "adhar-system"}, "...": "..."}]
}
```

Note that `restarts` and `logs` here are the **raw** backend payloads, not the
shapes `promql`/`logql` return, and `alerts` is a list only when the Prometheus
call succeeded — otherwise it is the `unavailable` object.

---

## 🛡️ security

**Backend:** the Kubernetes API — `wgpolicyk8s.io/v1alpha2` `PolicyReport` and
`ClusterPolicyReport`, and `kyverno.io/v1` `ClusterPolicy`.

**Unconfigured:** same as `cluster`. There is no separate Kyverno endpoint to
configure; if the API server is reachable and the ClusterRole grants the reads,
these tools work.

When `namespace` is omitted, both namespaced `PolicyReport`s **and**
cluster-scoped `ClusterPolicyReport`s are read. When a namespace is given, only
the namespaced reports in it are read.

Each report result is flattened to `policy`, `rule`, `result`, `severity` (from
`properties.severity`, falling back to a top-level `severity`), `message`,
`category`, `report` (the report's own name), `namespace`, and `resource`
rendered as `Kind/name` from the **first** entry of the result's `resources`.

### `findings`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `namespace` | `str \| None` | `None` | Restrict to one namespace |
| `result` | `str` | `"fail"` | `"fail"`, `"warn"`, `"pass"`, `"error"`, or `"all"` |
| `limit` | `int` | `100` | Truncates the returned `findings` list |

```json
{
  "count": 137,
  "by_policy": {"require-run-as-nonroot": 61, "disallow-privileged-containers": 44},
  "findings": [
    {"policy": "require-run-as-nonroot", "rule": "check-containers", "result": "fail",
     "severity": "medium", "message": "runAsNonRoot must be true",
     "category": "Pod Security", "report": "cpol-require-run-as-nonroot",
     "namespace": "team-a", "resource": "Deployment/api"}
  ]
}
```

`count` is the number of matching results **before** truncation, and `by_policy`
is likewise computed over all of them, keeping the 20 most common policies. Only
`findings` is sliced to `limit` — so `count > len(findings)` is normal and not a
bug.

Any `result` value other than `"all"` is used as an exact match, so a typo such
as `"failed"` silently returns zero findings rather than an error.

### `policy_explain`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `policy` | `str` | required | ClusterPolicy name |

Reads the cluster-scoped `ClusterPolicy` and cross-references it against every
report in the cluster. Returns `policy`, `title` and `description` (from the
`policies.kyverno.io/title` and `.../description` annotations),
`validation_failure_action`, `background`, a `rules` list of
`{name, match, exclude, message}`, `current_violations` (a full count of `fail`
results for this policy) and `sample_violations` (the first 10).

**Failure mode:** a policy name that does not exist raises the Kubernetes
client's `ApiException`, which is *not* an anticipated failure — the model sees
only a generic execution error, not a 404 message. List policies with
`findings`/`posture` first if the name is uncertain.

### `posture`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `namespace` | `str \| None` | `None` | Restrict to one namespace |

```json
{
  "scope": "cluster-wide",
  "total_results": 1482,
  "by_result": {"pass": 1301, "fail": 137, "warn": 44},
  "by_severity": {"medium": 98, "high": 39},
  "failing_policies": {"require-run-as-nonroot": 61}
}
```

`scope` is the namespace string, or the literal `"cluster-wide"` when omitted.
`by_result` counts every result; `by_severity` and `failing_policies` count only
`fail` results, and `failing_policies` keeps the top 20.

### `propose_exception` ✍️

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `policy` | `str` | required | ClusterPolicy to except |
| `rules` | `list[str]` | required | Rule names within that policy |
| `namespace` | `str` | required | Namespace of the exception and of the matched resources |
| `match_kinds` | `list[str]` | required | `spec.match.any[0].resources.kinds` |
| `match_names` | `list[str]` | required | `spec.match.any[0].resources.names` |
| `why` | `str` | required | Rationale; also stored as an annotation on the manifest |
| `ttl_days` | `int` | `30` | Days until the recorded expiry date |
| `repo` | `str` | `"packages"` | Target repo |
| `path` | `str \| None` | `None` | Override the file path |

Renders a `kyverno.io/v2` `PolicyException` with an **explicit expiry**, so an
exception cannot silently become permanent:

```yaml
apiVersion: kyverno.io/v2
kind: PolicyException
metadata:
  name: adhar-ai-require-run-as-nonroot-team-a
  namespace: team-a
  labels:
    adhar.io/origin: adhar-ai
  annotations:
    adhar.io/exception-expires: '2026-10-13'
    adhar.io/exception-rationale: legacy image, rebuild tracked in ADHAR-1187
spec:
  exceptions:
  - policyName: require-run-as-nonroot
    ruleNames:
    - check-containers
  match:
    any:
    - resources:
        kinds:
        - Deployment
        names:
        - legacy-api
        namespaces:
        - team-a
```

The name is `adhar-ai-<policy>-<namespace>`, truncated to 63 characters and
stripped of trailing hyphens. The default path is
`security/kyverno-policies/manifests/exceptions/<name>.yaml`. The expiry date is
computed in UTC as `now + ttl_days` and appears in the PR title as well.

**The expiry is a recorded date, not an enforcement mechanism.** Kyverno does not
delete the exception when the date passes; something must reap it.

---

## 💰 cost

**Backend:** OpenCost's HTTP API, `/allocation/compute` with `accumulate=true`.

**Unconfigured:** `OPENCOST_URL` unset raises `BackendNotConfigured: OpenCost is
not configured for this Adhar AI deployment (set OPENCOST_URL)` at request time.

OpenCost returns `data: [ {key: {...costs}} ]`. All three tools flatten that into
a ranked list of rows: `name`, `total_cost`, `cpuCost`, `ramCost`, `gpuCost`,
`pvCost`, `networkCost` (each summed across accumulation windows) and
`efficiency` (OpenCost's `totalEfficiency` for the last window seen), sorted by
`total_cost` descending.

This server has **no write tool**.

### `cost_by`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `dimension` | `str` | `"namespace"` | Aggregation dimension |
| `window` | `str` | `"7d"` | OpenCost window, e.g. `"24h"`, `"7d"`, `"30d"` |
| `top` | `int` | `20` | Rows returned |

Valid dimensions are `namespace`, `controller`, `pod`, `node`, `cluster`,
`service`, or any `label:<key>`. The dimension is validated **in-process before
the HTTP call**, so a typo becomes a readable `ValueError: unknown cost dimension
'namesapce'; expected one of ['namespace', 'controller', 'pod', 'node',
'cluster', 'service'] or 'label:<key>'` rather than an OpenCost 400 that the
model misreads as "the cost backend is broken". (The tool's own docstring omits
`service` from that list; the validator accepts it.)

```json
{
  "dimension": "namespace",
  "window": "7d",
  "total_cost": 184.3172,
  "rows": [
    {"name": "adhar-system", "total_cost": 97.21, "cpuCost": 41.0, "ramCost": 38.6,
     "gpuCost": 0.0, "pvCost": 17.6, "networkCost": 0.01, "efficiency": 0.42}
  ]
}
```

`total_cost` is the sum of the **returned** rows only. With the default `top=20`
it is the top-20 subtotal, not the cluster total.

### `budget_status`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `monthly_budget` | `float` | required | The budget to compare against. Adhar stores no budgets, so the caller supplies it |
| `window` | `str` | `"30d"` | Observation window |
| `dimension` | `str` | `"namespace"` | Aggregation dimension |

This tool does **not** validate `dimension` — an invalid value reaches OpenCost.

Internally it fetches the top 200 rows, so `observed_cost` is a much closer
approximation of the true total than `cost_by`'s.

```json
{
  "window": "30d",
  "observed_cost": 812.4401,
  "projected_monthly": 812.4401,
  "monthly_budget": 1000.0,
  "over_budget": false,
  "pct_of_budget": 81.2,
  "top_contributors": [{"name": "adhar-system", "total_cost": 410.9, "...": "..."}]
}
```

The projection is `(observed / days) * 30`, where `days` is parsed from the
window suffix: `d` as days, `h` as hours/24, `m` as **minutes**/1440. A window
with any other suffix (including OpenCost's own `month` or an RFC3339 range)
yields `0` days, and then `projected_monthly`, `pct_of_budget` are `null` and
`over_budget` is `false`. `pct_of_budget` is also `null` when `monthly_budget` is
`0`.

### `showback`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `window` | `str` | `"30d"` | Observation window |
| `label` | `str` | `"app.kubernetes.io/part-of"` | Kubernetes label to aggregate by; sent as `label:<label>` |

Fetches the top 100 rows and adds `share_pct` to each — its percentage of the
returned total, rounded to 2 decimals, or `0.0` when the total is zero. Returns
`{"window", "label", "total_cost", "rows"}`.

---

## 📚 catalog

**Backend:** Gitea (the `packages` repo tree) joined with the ArgoCD Application
inventory. Nothing is hard-coded from a stale list.

**Unconfigured (graceful degradation):** `search_packages` never fails outright.
Each source is attempted in its own `try`; a failure is recorded under a
`partial` key and the tool still returns whatever it got. ArgoCD falls back to
the Application CRs in `adhar-system` when unkeyed, exactly as in `gitops`.

### `search_packages`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `query` | `str` | `""` | Case-insensitive substring match on the package name; empty means no filter |
| `category` | `str` | `""` | Restrict to one category directory; empty lists every top-level directory in the repo |

The `packages` repo has its category directories at the **root**
(`security/vault/manifests`, not `packages/security/...`), so a category is a
first-level directory and a package is a second-level directory inside it. Each
package is then joined against the deployed ArgoCD applications **by name**.

```json
{
  "count": 2,
  "packages": [
    {"name": "vault", "category": "security", "deployed": true,
     "sync_status": "Synced", "health_status": "Healthy"},
    {"name": "minio", "category": "data", "deployed": false}
  ]
}
```

`sync_status` and `health_status` are present only when a matching ArgoCD
application was found; `deployed` is always present.

With a backend down, the shape gains a `partial` key and the affected data is
simply absent — an ArgoCD outage means every package reports `deployed: false`:

```json
{
  "count": 12,
  "packages": [{"name": "vault", "category": "security", "deployed": false}],
  "partial": {"argocd": "BackendNotConfigured: Kubernetes is not configured for this Adhar AI deployment (disabled via ADHAR_AI_KUBE_DISABLED=1)"}
}
```

Treat `deployed: false` as unknown whenever `partial.argocd` is set.

### `template_params`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `golden_path` | `str` | `""` | Name of a golden path; empty lists them all |

Three golden paths ship today:

| Golden path | Renders | Required params | Optional params |
|---|---|---|---|
| `go-service` | Deployment + Service + HTTPRoute | `name`, `image`, `hostname` | `namespace`, `port` (default 8080) |
| `python-service` | the same, FastAPI/uvicorn defaults | `name`, `image`, `hostname` | `namespace`, `port` (default 8000) |
| `cnpg-database` | a CloudNativePG `Cluster` | `name`, `database`, `owner` | `namespace`, `instances` (default 1), `storage` (default `5Gi`) |

With no argument it returns `{"golden_paths": [{"name", "description"}, ...]}`.
With a known name it returns `{"golden_path": ..., "description": ..., "params":
{...}, "required": [...]}`.

An unknown name returns a **successful result carrying an error field**, not an
exception:

```json
{"error": "unknown golden path 'rust-service'", "available": ["cnpg-database", "go-service", "python-service"]}
```

### `scaffold` ✍️

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `golden_path` | `str` | required | One of the three names above |
| `params` | `dict[str, Any]` | required | Parameters for the template |
| `why` | `str` | required | Rationale for the PR body |
| `repo` | `str` | `"packages"` | Target repo |

Validation happens before anything else, and both failures return an object
rather than raising:

```json
{"error": "missing required params: ['hostname']", "golden_path": "go-service"}
```

A param is "missing" if falsy, so `port: 0` counts as absent — which is harmless,
since `port` is not required and falls back to the template default.

`namespace` defaults to the service's own `name`. Every rendered object carries
`app.kubernetes.io/name: <name>` and `adhar.io/origin: adhar-ai`.

The service paths render three files under
`application/<name>/manifests/`: `deployment.yaml`, `service.yaml`,
`httproute.yaml`. The Deployment is hardened by default — `runAsNonRoot`,
`runAsUser: 65532`, `seccompProfile: RuntimeDefault`,
`allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`,
`capabilities.drop: [ALL]`, requests `50m`/`128Mi`, limits `500m`/`512Mi`, and a
readiness probe on `/healthz`. The HTTPRoute attaches to the
`adhar-gateway` Gateway in `adhar-system` with a `PathPrefix: /` rule.

`cnpg-database` renders one file, `data/<name>/manifests/cluster.yaml`, with
`monitoring.enablePodMonitor: true` and the
`argocd.argoproj.io/sync-options: ServerSideApply=true` annotation — CNPG
Clusters overflow client-side apply's last-applied annotation, and without SSA
ArgoCD force-fails and drops the database.

```json
{
  "repo": "packages",
  "number": 418,
  "url": "https://gitea.example.com/adhar/packages/pulls/418",
  "branch": "adhar-ai/scaffold-go-service-orders-api-4b1e77",
  "files": [
    "application/orders-api/manifests/deployment.yaml",
    "application/orders-api/manifests/service.yaml",
    "application/orders-api/manifests/httproute.yaml"
  ]
}
```

---

## ✍️ The four write tools

`gitops_propose_change`, `provision_propose_xr`, `security_propose_exception` and
`catalog_scaffold` differ only in what they render. All four funnel into one
function, `open_pr` in `src/adhar_ai/mcp/common/pr.py`, and that function is the
entire mutating surface of the codebase.

### The path

1. **Policy gate** — `guard_write` validates the repo and every path.
2. **Identifiers** — an audit id `aud-<16 hex>` is minted, and the branch name
   and PR title are derived from `title`.
3. **Branch** — `POST /repos/<org>/<repo>/branches`, from `main`.
4. **Files** — one `put_file` per change. Each probes the path first: an existing
   file is updated with its blob SHA (`PUT`), a `404` falls through to a create
   (`POST`). Any other HTTP status propagates.
5. **Pull request** — `POST /repos/<org>/<repo>/pulls`, head = the new branch,
   base = `main`.
6. **Label** — the `adhar-ai` label is applied by id, creating it (colour
   `#7c3aed`) if the repo does not have it. This step is **best-effort**: an
   `HTTPError`, `KeyError` or `ValueError` here is swallowed, because a label
   failure must never lose an otherwise-good proposal.
7. **Audit** — one `open_pr` event is emitted with the PR number, URL and files.

Every write targets `main` as the base branch, and the base is not configurable
from a tool argument.

### Naming and provenance

| Marker | Value |
|---|---|
| Branch | `adhar-ai/<slug of title, ≤48 chars>-<6 hex>` |
| PR title | `[adhar-ai] <title>` (not doubled if the title already starts with the prefix) |
| PR label | `adhar-ai` |
| Manifest label | `adhar.io/origin: adhar-ai` on everything rendered |
| Commit message | `<full title>` + blank line + `<why>` + blank line + trailer |

The slug lowercases the title and replaces every run of non-`[a-z0-9]`
characters with `-`; an empty result becomes `change`. The random suffix makes
each branch unique, so re-running the same proposal does not collide.

The commit trailer is:

```
Proposed-by: adhar-ai (model=claude-sonnet-5)
Audit-Id: aud-3f7a9b1c2d4e5f60
Requested-by: alice@example.com
adhar.io/origin: adhar-ai
```

`Requested-by` is omitted when no user is known, and `model=unset` when no model
is known. **Both are `None` for every MCP tool call**: `open_pr` takes `model`
and `user` keyword arguments, and none of the four tools passes them. They are
populated only when `open_pr` is called with them supplied. So a PR opened
directly through MCP shows `model=unset` and `Requested by: _operator
(event-driven)_`; attribution of the human comes from the Keycloak identity at
the gateway and from the audit stream, not from the commit trailer.

The PR body always carries a "Why" section (your `why`, verbatim), the file list,
a provenance table (origin, tool, model, requested-by, audit id) and a closing
paragraph restating that opening a PR is the agent's only write path.

### The policy gate

`guard_write` in `src/adhar_ai/mcp/common/policy.py` runs **before the Gitea
client is even constructed**, so a denial makes no network call of any kind. The
checks run in this order, and the first failure raises `WriteNotPermitted`:

| # | Check | Refusal message |
|---|---|---|
| 1 | `GITEA_WRITE_ENABLED` is true | `this MCP server is read-only (GITEA_WRITE_ENABLED=false); no write tool is available here` |
| 2 | A bot token is configured | `no Gitea bot token configured (the adhar-ai-bot secret is unset), so no pull request can be opened` |
| 3 | `repo` is in the allowed set | `repo 'infra' is not in the allowed set ['packages', 'environments']` |
| 4 | At least one file | `a proposal must change at least one file` |
| 5 | Each path normalizes safely | `illegal file path '../../etc/passwd'` |
| 6 | Each path is under an allowed prefix | `path 'x.yaml' in repo 'packages' is outside the allowed prefixes [...]` |

Notes on each:

- **Allowed repos** default to `("packages", "environments")` and are overridden
  wholesale by `GITEA_WRITE_REPOS` (or `ADHAR_AI_GITEA_WRITE_REPOS`), comma
  separated.
- **Normalization** strips whitespace, strips leading `/`, then runs
  `posixpath.normpath`. A path that resolves to `.`, `""`, `..`, or anything
  starting `../` is rejected. Traversal *inside* the tree is collapsed rather
  than refused: `packages/a/../b.yaml` normalizes to `packages/b.yaml` and is
  allowed. The normalized path is what gets committed.
- **Prefixes** are checked **repo-qualified**: the path `security/vault/x.yaml`
  in repo `packages` is tested as `packages/security/vault/x.yaml` against the
  prefix list, which defaults to `("packages/", "environments/")`. With the
  default prefixes and the default repo list, this check therefore passes for
  any non-traversing path in an allowed repo — it becomes a real narrowing only
  when a tighter prefix list is supplied. `guard_write` accepts a `prefixes`
  argument for that; `open_pr` always calls it with the default.
- **Registration is unconditional.** The `propose_*` and `scaffold` tools are
  registered on their servers whether or not writes are enabled, so they appear
  in `tools/list` on a read-only server and refuse at call time with check 1.
  That is deliberate: the model gets a sentence explaining why, instead of a
  missing tool it cannot reason about.

### The layers around it

This gate is defence in depth, not the only control:

- **The runtime**, when it drives these tools, applies the `writePolicy` from the
  `adhar-ai-config` ConfigMap first, keyed on the session's autonomy stage
  (`read-only` withholds write tools entirely; `scoped` uses a separate,
  empty-by-default allow-list). It inspects the `repo`, `changes[].path` and
  `path` arguments — so for `scaffold`, whose paths are rendered server-side, the
  runtime can only check the repo, and `guard_write` is what checks the rendered
  paths.
- **agentgateway** requires `platform-admin` on these four tool names.
- **Gitea branch protection** can key on the `adhar-ai/` branch prefix.
- **Kyverno's `adhar-ai-guardrails` ClusterPolicy** audits for the
  `adhar.io/origin: adhar-ai` label in the cluster.

### Audit

Every tool call — read or write, success or failure — emits one JSON line on
stdout, which Alloy scrapes into Loki. The decorator's event is:

```json
{"ts": 1789000000.12, "adhar.io/origin": "adhar-ai", "audit_id": "aud-3f7a9b1c2d4e5f60",
 "tool": "propose_change", "access": "write", "domain": "gitops",
 "args": {"repo": "packages", "title": "raise vault memory limit"},
 "decision": "ok", "duration_ms": 812.4}
```

A failure emits the same record with `"decision": "error"` and an `error` field
of `"<ExceptionType>: <message>"`. `open_pr` emits a second event with
`"action": "open_pr"`, `"decision": "proposed"`, and the `repo`, `pr`, `url`,
`files`, `user` and `model`.

Arguments are redacted before they are written: any key matching `token`,
`password`, `apikey`, `api_key`, `secret`, `authorization` or `credential`
(case-insensitive, hyphens normalised to underscores) becomes `***`, strings
longer than 200 characters are truncated, lists are cut to 20 entries, and
nesting deeper than 6 levels becomes `…`.

---

## ⚠️ Error semantics

The MCP SDK splits tool failures in two. A `ToolError` is an expected outcome and
its message is returned to the client. **Every other exception is treated as a
crash and its text is withheld** — the model sees only `Error executing tool
<name>`.

That default loses the one property `clients/errors.py` exists to provide: the
agent could not distinguish "Prometheus is not configured here" from "the tool
broke". So the audit decorator, which wraps *every* tool, re-raises anticipated
failures as `ToolError`.

`EXPECTED_FAILURES` in `src/adhar_ai/mcp/common/audit.py` is exactly:

| Exception | Raised when |
|---|---|
| `BackendNotConfigured` | A backend URL or credential is unset — `PROMETHEUS_URL`, `LOKI_URL`, `TEMPO_URL`, `OPENCOST_URL`, `GITEA_API_URL`, `ARGOCD_URL`, or no Kubernetes access at all |
| `WriteNotPermitted` | Any of the six policy checks refused the write. It subclasses `PermissionError` |
| `ValueError` | In-process argument validation, e.g. `cost_by`'s unknown dimension |
| `KeyError` | A required key was missing from a payload |

`_expected` passes an existing `ToolError` through unchanged, wraps anything in
that tuple as `ToolError(f"{type(exc).__name__}: {exc}")`, and returns everything
else untouched so it stays a crash. The message that reaches the model therefore
reads:

```
BackendNotConfigured: Prometheus is not configured for this Adhar AI deployment (set PROMETHEUS_URL)
WriteNotPermitted: repo 'infra' is not in the allowed set ['packages', 'environments']
ValueError: unknown cost dimension 'namesapce'; expected one of [...] or 'label:<key>'
```

**What is *not* in the list matters just as much.** `httpx.HTTPStatusError` from a
backend 500, and `kubernetes.client.rest.ApiException` from a 404 or an RBAC
`Forbidden`, are both masked. A missing ClusterPolicy in `policy_explain`, or a
pod name that does not exist in `describe`, reaches the model as a bare execution
error. If you are debugging one of those, read the audit line — it always carries
the full `"<Type>: <message>"` in its `error` field, even when the model was not
shown it.

Three tools deliberately return a **successful result describing a problem**
rather than raising, and callers must inspect the body:

| Tool | Field to check |
|---|---|
| `gitops.app_diff` | `diff_available: false` plus `reason` |
| `catalog.search_packages` | `partial: {gitea: ..., argocd: ...}` |
| `catalog.template_params`, `catalog.scaffold` | `error` plus `available` / `golden_path` |

And `observability.correlate` marks each failed source inline as
`{"unavailable": "<Type>: <message>"}` while returning the rest.

The system prompt instructs the model to say plainly when a backend is
unavailable and never to present unretrieved data as if it were retrieved. These
error shapes are what make that instruction actionable.

---

## 🤝 Adding a tool

See **[CONTRIBUTING.md](../CONTRIBUTING.md)**. In short: add the function to its
domain module under `src/adhar_ai/mcp/`, decorate it with the `read` or `write`
decorator returned by `access_tools`, regenerate the contract with
`uv run adhar-ai tools > contract/tools.json`, and add a test. A write tool must
reach `open_pr` and nothing else — a source-level test asserts that every write
tool's body contains no `apply`, `kubectl`, `create_namespaced`, `patch_` or
`delete_` call, and that the PR module imports no Kubernetes, AWS, Azure or GCP
client.
