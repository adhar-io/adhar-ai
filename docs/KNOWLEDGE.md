# 🧠 The Knowledge Base

What Adhar AI knows about your platform, where it comes from, and how it keeps
learning.

**Sanskrit: अधार (Adhāra) – Foundation**

An agent that only knows Kubernetes in general is a search engine with extra
steps. One that knows *your* ADRs, *your* installed packages, *your* live
cluster and *your* last outage is a colleague. This is the machinery that makes
the second one possible.

---

## Contents

1. [What it holds](#1-what-it-holds)
2. [How it stays current](#2-how-it-stays-current)
3. [How retrieval works](#3-how-retrieval-works)
4. [How it learns](#4-how-it-learns)
5. [Degradation: what works without what](#5-degradation-what-works-without-what)
6. [The HTTP surface](#6-the-http-surface)
7. [Operating it](#7-operating-it)
8. [Design notes worth knowing](#8-design-notes-worth-knowing)

---

## 1. What it holds

Six sources, each turning one part of the platform into retrievable documents.
They are separate because they change at completely different rates and fail
independently: a source whose backend is down costs its own knowledge and
nothing else.

| Source | Origin | What it answers |
|---|---|---|
| 📘 Documentation | `docs` | "why is it built this way", "how do I do X" |
| 🧰 Tool inventory | `tools` | "what can you actually do for me" |
| 📦 Package catalogue | `packages` | "what is installed, what does it depend on" |
| ☸️ Live cluster | `cluster` | "what is running right now, and is it healthy" |
| 🔍 Operator findings | `findings` | "has the platform noticed this before" |
| 📝 Human notes | `notes` | "what did we decide, and what did we learn" |

`cluster` is the one that makes this a knowledge base rather than a snapshot of
a docs folder. It is re-derived from live state on every refresh, so the agent's
background knowledge moves with the platform.

`tools` is worth a word too. "What can you do?" is among the most common
questions put to a platform assistant, and the tool schemas in the model's
context answer it only for the tools offered in *that* request — a read-only
session cannot describe the write tools it was not given. Indexing the inventory
lets the agent describe its own capabilities and their limits accurately at any
autonomy stage.

### Kinds, and why they are weighted

Every document has a **kind**, and kind is used at retrieval time. A runbook and
a two-year-old meeting note are not equally authoritative about how to fix
something today.

| Kind | Weight | |
|---|---|---|
| `runbook` | 1.25 | written to be followed, under pressure |
| `incident` | 1.20 | what actually happened, and what fixed it |
| `finding` | 1.15 | the platform's own observation about itself |
| `adr`, `tool` | 1.10 | why the platform is this way; what the agent can do |
| `package`, `resource` | 1.05 | what is installed; live state |
| `doc` | 1.00 | the baseline |
| `qa`, `note` | 0.95 | precedent and asides, not sources of truth |

The weights are deliberately mild. They break ties between comparable matches
rather than override relevance.

---

## 2. How it stays current

Two schedules, same code path, same database:

- **In-process**, every `ADHAR_AI_KNOWLEDGE_REFRESH_SECONDS` (default 1800).
- **A nightly CronJob** (`adhar-ai-knowledge-reindex`, 02:15) for the case the
  in-process timer cannot cover — a refresh that must happen whether or not the
  runtime is up, healthy or mid-rollout, and one an operator can trigger by hand.

### Incremental, which is what makes a schedule affordable

Ingestion upserts by `(doc_id, chunk_index)` and **skips the embedding call
entirely when nothing changed**. A chunk is stale only if its text changed or if
it was embedded by a different model.

Measured on the Adhar platform's own corpus — 1,297 chunks across 170 documents:

| Pass | Chunks written | Chunks unchanged | Embedding calls |
|---|---|---|---|
| First | 1,297 | 0 | 3 |
| Second, nothing changed | 0 | 1,297 | 0 |

Rebuilding the whole index nightly would put keeping it current and keeping the
bill down in direct conflict. This removes that conflict.

### Deletions propagate

Each ingest reports which documents an origin still has; anything else from that
origin is removed. Without this a decommissioned package or a deleted runbook
stays retrievable forever and the agent confidently cites something that no
longer exists — worse than not knowing.

Pruning is scoped to one origin, so refreshing the docs never deletes your notes.

---

## 3. How retrieval works

**Hybrid**, not one or the other:

- **Vector** similarity finds things phrased differently from the question.
- **Postgres full-text** finds the exact identifier the user typed —
  `CreateContainerConfigError`, `adhar-ai-mcp-gitops`, `CompositeCluster` — that
  an embedding blurs away.

Platform questions contain a lot of exact identifiers, so the two are fused
rather than chosen between, using **reciprocal rank fusion**. RRF combines two
rankings without needing their scores to be comparable, which they are not: a
cosine distance and a `ts_rank_cd` share no scale. Only the order each retriever
produced is used. The result is then adjusted by kind weight and feedback.

A hit reports which half found it, so you can see the fusion working:

```
[vector+lexical] doc      ARCHITECTURE.md#10. The AI Layer
[vector        ] package  package ai/vllm#Package `ai/vllm`
```

---

## 4. How it learns

Three things flow back in as they happen.

### Notes — what people learn

```bash
curl -X POST https://agent.<host>/knowledge \
  -H "Authorization: Bearer $(adhar auth token)" \
  -H 'content-type: application/json' \
  -d '{
    "title": "Postmortem: gitea bot token rotation broke the write path",
    "kind": "incident",
    "tags": ["gitea", "auth"],
    "body": "Symptom: every propose_change failed with 401.\nCause: the adhar-ai-bot token expired.\nFix: reissue it and update the secret in Vault."
  }'
```

```json
{"id": "c73c5fd7...", "durable": true, "indexed": true, "retrievable": true, "kind": "incident"}
```

Indexed **on the spot**, not at the next refresh: somebody writing up an outage
at 02:00 should be able to ask about it at 02:01.

> **Writing knowledge is not a platform write.** It touches the agent's own
> database, never a cluster and never a repository, so it does not go through
> the pull-request path. The four MCP write tools remain the only things that
> change the platform, and they remain PR-only.
>
> For knowledge that should outlive the database — a runbook the whole team
> depends on — commit it to the docs tree. It is indexed from there like
> everything else, and it goes through review like everything else.

### Findings — what the platform learns about itself

Every operator finding is indexed as soon as it is produced, including the pull
request that was proposed. The next investigation of a similar alert retrieves
what the last one concluded. This is the tightest loop available and it needs no
human in it.

### Feedback — which grounding actually helped

Every `/chat` response carries `grounding_chunk_ids`. Post them back:

```bash
curl -X POST https://agent.<host>/feedback \
  -H 'content-type: application/json' \
  -d '{"chunk_ids": [561], "helpful": true}'
```

Repeatedly helpful chunks rank slightly higher; repeatedly misleading ones
slightly lower. **Bounded to ±30% on purpose** — one downvote must not bury the
only document that answers a question.

---

## 5. Degradation: what works without what

A deliberate ladder with no cliff:

| You have | You get |
|---|---|
| pgvector + an embedding endpoint | hybrid vector and lexical retrieval over everything |
| pgvector, no provider key | full-text retrieval over the same indexed corpus |
| no database | in-process BM25 over the docs tree |
| no docs either | honest: "no grounding is available" |

An unkeyed platform is therefore still grounded, which is the difference between
an assistant that knows Adhar and one that knows the internet. `GET /healthz`
reports which rung you are on:

```json
{"rag": "hybrid (pgvector + lexical), fallback over 1027 local chunks"}
```

---

## 6. The HTTP surface

| Endpoint | What it does |
|---|---|
| `GET /knowledge` | what the base holds, by origin and kind, plus embedding models in use |
| `POST /knowledge` | add a note, runbook or incident write-up |
| `POST /knowledge/search` | retrieve grounding without running the agent |
| `POST /knowledge/refresh` | re-derive now; pass `["cluster"]` to scope it |
| `POST /feedback` | say whether an answer's grounding helped |

`POST /knowledge/search` is worth knowing about on its own. It answers "what does
the platform know about X" for a fraction of the cost and latency of an agent
run, and it is how you check what the agent *would* have been given when an
answer disappoints.

```bash
curl -X POST localhost:8080/knowledge/search \
  -H 'content-type: application/json' \
  -d '{"query": "which component routes LLM traffic by model name", "k": 3}'
```

```
[vector] package  package ai/agentgateway#Package `ai/agentgateway`
[vector] package  package ai/vllm#Notes
[vector] doc      USER_GUIDE.md#Self-hosted inference (vLLM)
```

Filter by kind with `{"kinds": ["runbook", "incident"]}` when you want procedure
rather than rationale.

---

## 7. Operating it

### Configuration

| Variable | Default | What it does |
|---|---|---|
| `ADHAR_AI_RAG_DSN` | composed from `RAG_DB_*` | the pgvector database. Unset means in-process lexical only |
| `ADHAR_AI_DOCS_PATH` | `/etc/adhar-ai/docs` | the docs tree to index |
| `ADHAR_AI_PACKAGES_PATH` | `""` | a package tree whose `adhar-package.yaml` files to index |
| `ADHAR_AI_KNOWLEDGE_REFRESH_SECONDS` | `1800` | how often to re-derive |

The `rag.table` and `findings.table` keys in `adhar-ai-config` name the tables.

### Re-indexing by hand

```bash
adhar-ai index                      # every source
adhar-ai index --source cluster     # just the live inventory
adhar-ai index --docs ../adhar/docs --packages ../adhar/platform/stack/packages
```

In the cluster:

```bash
kubectl -n adhar-system create job --from=cronjob/adhar-ai-knowledge-reindex reindex-now
```

### Reading the stats

```bash
curl -s localhost:8080/knowledge | jq '{chunks, documents, embedModels, warning}'
```

If `warning` is present, read it. It means more than one embedding model is
present in the table, and retrieval is degraded until the next refresh re-embeds
the stale rows — see below for why that matters so much.

### Verifying a real deployment

```bash
docker run -d --name adhar-rag -p 15432:5432 \
  -e POSTGRES_USER=adhar_ai -e POSTGRES_PASSWORD=adhar_ai \
  -e POSTGRES_DB=adhar_ai_rag pgvector/pgvector:pg16

ADHAR_AI_RAG_DSN=postgresql://adhar_ai:adhar_ai@127.0.0.1:15432/adhar_ai_rag \
  uv run hack/verify-knowledge.py --docs ../adhar/docs \
  --packages ../adhar/platform/stack/packages
```

That script checks the schema, ingestion, incrementality, hybrid retrieval,
exact-identifier lookup, immediate note availability, feedback ranking and
deletion propagation against a real database.

---

## 8. Design notes worth knowing

### Two embedding models in one table is silent corruption

Vectors from different models share no space. Cosine distance between them is
noise, so a table holding two models returns whichever rows happen to share the
query's model and ignores the rest. **There is no error, no empty result and no
warning from the database** — just consistently wrong retrieval.

This is not hypothetical. `load_embeddings` falls back gateway → local → none,
so a cluster that loses its provider key starts writing vectors from a different
space into the same table.

So the store records the embedding model **per row**, treats a row embedded by a
different model as stale, and re-embeds it on the next refresh. `GET /knowledge`
reports the models present and warns when there is more than one. The index
repairs itself; you only have to notice.

### `doc_id` is stable across re-ingestion

It is how the store recognises that `adr/0024.md#Decision` is the same document
it saw yesterday. That is what lets a refresh update in place, delete what has
genuinely gone, and re-embed almost nothing.

### Chunks carry their heading into the citation

`ADR-0024 §Decision` sends a reader to a paragraph. `ADR-0024` sends them to a
document. The heading rides in the citation because that is what makes it
actionable.

### The live cluster source is a summary, not a dump

Object specs are large, change constantly, and would flood retrieval with noise.
The detail lives behind the read tools, which fetch it fresh when a question
actually needs it. The knowledge base holds the shape of the platform; the tools
hold its current contents.

---

<div align="center">
<sub>See also: <a href="ARCHITECTURE.md">Architecture</a> · <a href="OPERATIONS.md">Operations</a> · <a href="TOOLS.md">Tool reference</a></sub><br>
<sub>Adhar • Built with ❤️ for developers!</sub>
</div>
