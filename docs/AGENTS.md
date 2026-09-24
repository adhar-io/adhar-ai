# Agents, tasks and automation

> Who answers, how work outlives a request, what happens unattended, and where
> the agent turns up.

Everything in this document sits on top of the guarantee the rest of the
platform rests on, and none of it relaxes that guarantee:

> **Read tools read. Write tools open a pull request. Nothing applies to a
> cluster.**

Six specialized agents do not make six ways to change the platform. They make
six ways to *investigate* it, all of which end at the same pull request a human
merges.

---

## 1. Two routes, one roster

`POST /chat` and `POST /tasks` run the **same** orchestrator over the **same**
roster. Both route to a specialist, apply that agent's ceiling, offer only its
tools and ground it only on the kinds it should read.

The one thing that differs is the lifetime, which is the one thing that should:

| | `/chat` | `/tasks` |
|---|---|---|
| Answers | inside the request | behind it, poll for the result |
| Survives a restart | no | yes, where a database is configured |
| Leaves a task row | no | yes |
| Can wait for an approval | no | yes |

A chat run writes no task row on purpose. One row per message would fill a
table with 30-day retention at chat volume and bury the long-running work
`/tasks` exists to show. The audit stream is that run's record.

So use `/chat` for a question and `/tasks` for work: an investigation that needs
twenty minutes, a plan that waits for approval, a job that outlives the rollout
that interrupts it.

## 2. The task

```bash
curl -sS $RUNTIME/tasks -H 'content-type: application/json' \
  -d '{"prompt":"why has checkout been OutOfSync since Tuesday?"}'
# {"id":"task-4f1c…","state":"queued","agent":"","autonomy":"read-only", …}

curl -sS $RUNTIME/tasks/task-4f1c…
# {"state":"done","agent":"release","result":"…","lineage":["incident"], …}
```

A task moves through a state machine that refuses illegal moves rather than
logging them:

```
queued ──► planning ──► awaiting_approval ──► running ──► done
   │           │                │                │
   └───────────┴────────────────┴────────────────┼──► failed
                                                 ├──► blocked ──► running
                                                 └──► cancelled
```

`done`, `failed` and `cancelled` are terminal and have no outgoing edge. A task
that could go from `done` back to `running` because one code path forgot to
check is a task whose history nobody can trust, so the transition raises.

**Durability.** With a database configured (the same CNPG instance pgvector
uses) tasks survive a restart, and work interrupted mid-flight is re-enqueued at
start-up with `error` set to say it was resumed. Without one they live in
memory, and `/healthz` says so:

```json
"tasks": { "storage": "in-memory (no database)", "durable": false, "workers": 3 }
```

Workers are bounded. An agent run is expensive, so an unbounded pool turns a
burst of queued questions into a burst of concurrent inference spend.

---

## 3. The roster

Seven agents ship. Each is a role, a narrow tool list, an autonomy **ceiling**
and a closed list of colleagues it may hand work to.

| Agent | Answers | Ceiling |
|---|---|---|
| `incident` | alerts, crashloops, outages, "why is this broken?" | `suggest` |
| `cost` | spend, budgets, showback, right-sizing | `suggest` |
| `security` | policy, findings, CVEs, compliance evidence | `approve-to-apply` |
| `platform` | scaffolding, packages, golden paths, Crossplane | `suggest` |
| `release` | promotion, drift, sync failures, rollback | `approve-to-apply` |
| `guide` | how-to, onboarding, conventions, documentation | `read-only` |
| `generalist` | everything else — routing always terminates here | `suggest` |

Each agent also declares the **kinds of knowledge it should read** — the
security agent grounds on ADRs, runbooks and incidents; the guide on
documentation and packages. The topology from the knowledge graph is never
narrowed: what a thing connects to is true regardless of who is asking.

**The ceiling narrows and never widens.** A `read-only` agent stays read-only
for a platform administrator at `scoped`, because the narrowing belongs to the
role rather than to the request. Authority is applied in one order, and every
step can only reduce it:

```
ConfigMap default  ─►  agent ceiling  ─►  what the caller asked for  ─►  what their credential permits
```

### Routing

Lexical, not a model call:

```bash
curl -sS $RUNTIME/agents/route -H 'content-type: application/json' \
  -d '{"prompt":"the checkout pod is crashlooping"}'
# {"agent":"incident","confidence":0.62,"ceiling":"suggest","tools":[…]}
```

Spending a completion to decide who should spend a completion doubles latency
and cost on every request and misroutes in ways that are hard to debug. A
keyword miss is fixed by adding a word to a ConfigMap. Keywords match on
**prefix**, because platform vocabulary inflects constantly and a whole-word
match on `crashloop` misses `crashlooping`, which is how people actually write.

Confidence is damped. An undamped `best / total` reports `1.0` whenever exactly
one agent scored at all, including on the flimsiest possible evidence.

### Handoff

An agent may pass a task on. Four rules make that safe:

1. **Only to a declared colleague.** `escalatesTo` is a closed list; an agent
   cannot invent a specialist, and a target missing from the roster is rejected
   at start-up rather than at three in the morning.
2. **With a stated reason**, so the chain reads afterwards to somebody who was
   not there.
3. **Never in a loop.** A chain longer than four hops is refused.
4. **As a new run, not a continuation.** The receiving agent gets the task and
   the question, with its own tools, its own ceiling and its own scope.
   Continuing the same message history would carry the previous agent's tool
   results into a session that was never allowed to call those tools.

A refused handoff is **not** a failure. The task goes to the generalist, which
can answer anything, rather than dying because one agent wanted to forward it
somewhere it was not permitted to.

The chain is recorded on the task:

```json
"lineage": ["incident"],
"agent": "security",
"plan": [{"description":"handed from incident to security: the restart follows a policy denial"}]
```

### Configuring the roster

The `agents:` block in `adhar-ai-config` **overlays** the shipped roster. Naming
one agent leaves the other six as they ship, and an agent added in a later
release arrives with its defaults rather than absent.

```yaml
agents:
  dba:
    role: Postgres and CNPG questions.
    instructions: Prefer the cluster's own CNPG conventions over general advice.
    tools: [list_pods, describe, get_events, logs, promql]
    ceiling: read-only
    keywords: [postgres, cnpg, database, replica, wal]
    escalatesTo: [platform]
```

---

## 4. Plan first, then approve

At `read-only` and `suggest` the run is already reviewable: nothing lands
without a pull request somebody merges, so a plan step would be ceremony that
costs a completion.

At `approve-to-apply` and `scoped` the plan **is** the artifact a human
approves. The agent writes it without acting, the task stops at
`awaiting_approval`, and it stays there until somebody releases it:

```bash
curl -sS -X POST $RUNTIME/tasks/task-4f1c…/approve \
  -H "authorization: Bearer $TOKEN" -d '{"approve":true}'
```

Approval requires a **write-capable credential** — the same `platform-admin`
group the PR-opening tools need. A task at `awaiting_approval` is one whose
stage can change the platform, so releasing it is exactly as privileged as
writing, and letting an unauthenticated caller do it would make the gate
decorative. A rejection is recorded on the plan with who rejected it and why.

---

## 5. Chores

Scheduled, narrow, individually enabled automation. This is where an agentic
platform earns its keep and where it does the most damage when wrong: a chore
that opens twelve pull requests on a Monday morning gets the whole layer
switched off, and it deserves to.

Seven ship, **all off, all in dry-run**:

| Chore | Looks for |
|---|---|
| `certificate-expiry` | certificates expiring within 21 days |
| `drift-reconciliation` | applications OutOfSync long enough to be deliberate |
| `orphaned-resources` | PVCs, Services and Secrets nothing references |
| `failing-scorecards` | the one criterion that would lift the worst grade |
| `cost-outliers` | namespaces that moved sharply against their own baseline |
| `security-findings` | violations and findings older than a week, grouped by cause |
| `runbook-rot` | procedures referencing things that no longer exist |

Enabling and going live are **two separate decisions**:

```yaml
chores:
  certificate-expiry:
    enabled: true     # it runs, and REPORTS what it would do
    dryRun: true      # it still proposes nothing
  drift-reconciliation:
    enabled: true
    dryRun: false     # it may now open pull requests
    maxProposals: 2   # at most two from one run
```

You should be able to watch a chore be right for a fortnight before it is
allowed to act. `maxProposals` is the difference between a helpful Monday and an
unreviewable flood. A name that is not in the catalogue is ignored with a
warning rather than created, so a typo cannot bring unreviewed automation into
existence.

```bash
curl -sS $RUNTIME/chores                          # the catalogue and its schedule
curl -sS -X POST $RUNTIME/chores/cost-outliers/run  # run one now, within its own limits
```

---

## 6. Journeys

An agent behind its own URL is a destination people have to remember to visit.
Adoption comes from it turning up where the work already is.

| Surface | Inbound | Rendered as |
|---|---|---|
| `slack` | event or slash command, mention stripped | blocks in the originating thread |
| `pull-request` | Gitea webhook | a review comment that declares itself |
| `ci` | failed pipeline with its log tail | blocks, cause first |
| `console` | — | structured JSON the UI lays out |

```bash
curl -sS $RUNTIME/journeys/slack -H 'content-type: application/json' -d @event.json
```

**A thread is a conversation.** The Slack thread, or the pull request, becomes
the session, so a follow-up continues rather than starting fresh. The agent is
also told which tools the earlier turns already called, because a model reading
a replayed answer cannot tell what was retrieved and what was reasoned, and
re-retrieves everything to be safe.

**Every inbound payload is attacker-influenced.** A pull-request body, a branch
name, a build log — anyone who can open a pull request can write anything in it.
They arrive framed explicitly as data, and the real defence is structural: a
review cannot change anything, whatever it is told to do.

A review comment says what it is:

> *Automated review. I can read platform state and open pull requests; I cannot
> merge, apply or deploy anything.*

A comment from an agent that does not say it is from an agent is a comment
people argue with.

---

## 7. Discovery, and a platform that teaches itself

### What can I ask?

```bash
curl -sS $RUNTIME/capabilities
```

Generated from the live tool inventory, the roster, the knowledge base and the
chore catalogue — never hand-written. A hand-written list of capabilities is
wrong within a release, and wrong in the direction that matters: it promises
things that no longer exist. A domain whose MCP server is down is reported
`available: false`, because an honest "not right now" is more useful than a list
of what it could do if it were up.

It also carries `tryAsking` — six questions, each demonstrating a different
capability, none of which can change anything.

### What could I not answer?

```bash
curl -sS $RUNTIME/coverage
```

Every run that ends badly is recorded as a gap with the question that caused it:

| Reason | What it means |
|---|---|
| `no-grounding` | the knowledge base had nothing — a documentation gap |
| `no-tools` | nothing was looked at — likely a routing or scope gap |
| `error` | the run failed outright |
| `empty-answer` | the model produced nothing usable |
| `unhelpful` | a human said so, through `POST /feedback` |

The classification is deliberately mechanical. A model grading its own answers
would be expensive and unreliable in the one direction that matters: a model
that produced a bad answer is not well placed to notice.

Gaps cluster by question, so the queue is ordered by how often something has
been asked for. **That queue is what somebody writes the missing runbook from**,
the knowledge base indexes it, and the next person asking gets an answer. A
platform assistant's real failure mode is not being wrong; it is being unhelpful
in a way nobody reports, over and over, until people stop asking.

---

## 8. The routes

| Route | What it does |
|---|---|
| `POST /chat` | one agent run, answered inside the request |
| `POST /tasks` | start work that outlives the request |
| `GET /tasks` · `GET /tasks/{id}` | what is running, and what one task did |
| `POST /tasks/{id}/approve` | release or reject a plan — needs a writer |
| `GET /agents` | the roster, with each agent's ceiling and tools |
| `POST /agents/route` | who would take this, without running anything |
| `POST /journeys/{surface}` | Slack, a pull request, a failed pipeline |
| `GET /chores` · `POST /chores/{name}/run` | the catalogue, and one run now |
| `GET /capabilities` | what the platform can do right now |
| `GET /coverage` | what it could not answer, clustered |

All of them are gated by the same policy as the rest of the runtime.
`ADHAR_AI_REQUIRE_AUTH=true` closes every one.

---

## See also

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — how the loop, the toolbox and the
  gateway fit together
- **[KNOWLEDGE.md](KNOWLEDGE.md)** — retrieval, the knowledge graph and the
  feedback loop
- **[SECURITY.md](SECURITY.md)** — the threat model these controls answer
- **[OPERATIONS.md](OPERATIONS.md)** — running it, and what to watch
