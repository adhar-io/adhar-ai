# Connecting Claude Code and other AI clients to Adhar MCP

Adhar's seven MCP servers — cluster, gitops, provision, observability,
security, cost, catalog — are federated by agentgateway into **one** MCP
endpoint. Any client that speaks MCP over streamable HTTP can use it: Claude
Code, Claude Desktop, Cursor, VS Code Copilot, Windsurf, Zed, an OpenAI
Agents SDK app, your own agent. They all get the same 29 tools, the same
read/write tagging, and the same guarantee: the only write any of them can
perform is opening a pull request.

```
https://mcp.<your-host>/mcp
```

Everything below is this URL plus a bearer token.

## 1. Get a token

The endpoint takes a Keycloak JWT. The `adhar` CLI mints one from your login:

```bash
adhar auth login            # once; opens the browser
adhar auth token            # prints the bearer token (refreshes it when needed)
```

`adhar auth token` prints the bare token, so it drops into a shell
substitution. Tokens expire (one hour by default); the CLI refreshes the
session silently, so re-running the command is always enough.

What the token must carry: the `platform-developer` group for the read tools,
`platform-admin` for the two PR-opening tools (`gitops_propose_change`,
`provision_propose_xr`). A token without the group gets `403` on exactly those
tools and nothing else.

## 2. Claude Code

```bash
claude mcp add --transport http adhar https://mcp.<your-host>/mcp \
  --header "Authorization: Bearer $(adhar auth token)"
```

Then, in a session:

```
> /mcp                       # shows "adhar" connected and its 29 tools
> which pods are crash-looping in team-ml, and why?
```

Claude Code stores the header with the server definition, so the token it
holds is the one minted when you ran `claude mcp add`. When it expires
(`401` from every tool), re-add with a fresh token:

```bash
claude mcp remove adhar && claude mcp add --transport http adhar \
  https://mcp.<your-host>/mcp --header "Authorization: Bearer $(adhar auth token)"
```

Scope it to a project with `--scope project` to commit the server into
`.mcp.json` **without** the token — put the header in your user scope instead,
or teammates will share one identity.

## 3. Claude Desktop, Cursor, Windsurf, VS Code, Zed

All of these read the same JSON shape (file location varies: Claude Desktop
`claude_desktop_config.json`, Cursor `~/.cursor/mcp.json`, Windsurf
`~/.codeium/windsurf/mcp_config.json`, VS Code `.vscode/mcp.json`, Zed
`settings.json` under `context_servers`):

```json
{
  "mcpServers": {
    "adhar": {
      "type": "http",
      "url": "https://mcp.<your-host>/mcp",
      "headers": { "Authorization": "Bearer <paste: adhar auth token>" }
    }
  }
}
```

A client that supports only stdio servers can bridge with `mcp-remote`:

```json
{
  "mcpServers": {
    "adhar": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://mcp.<your-host>/mcp",
               "--header", "Authorization: Bearer ${ADHAR_TOKEN}"]
    }
  }
}
```

with `ADHAR_TOKEN="$(adhar auth token)"` in the client's environment.

## 4. Your own agent (any SDK)

Anything that can POST JSON-RPC over HTTP can use the endpoint. With the
official MCP Python SDK:

```python
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async with streamablehttp_client(
    "https://mcp.<your-host>/mcp",
    headers={"Authorization": f"Bearer {token}"},
) as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()             # 29 tools, prefixed
        result = await session.call_tool("cluster_list_pods", {"namespace": "team-ml"})
```

An OpenAI-compatible agent that wants a *model* rather than tools uses the
other door, `https://ai.<your-host>/v1`, with the same bearer token — see
[ARCHITECTURE.md](ARCHITECTURE.md) for how model names route.

## 5. What you will see

**Tool names are prefixed by their server** — `cluster_list_pods`,
`gitops_propose_change`, `security_findings` — because agentgateway federates
with `prefixMode: Always`. The gateway's authorization rules key on the
prefixed name, so a client must call it as listed. The bare names in
[TOOLS.md](TOOLS.md) and `contract/tools.json` are what each server registers
on its own.

**Reads run as you.** The gateway preserves your token to the server, which
exchanges it for a short-lived Kubernetes token with your RBAC; a namespace
you cannot read in `kubectl` is one your agent cannot read either.

**Writes are pull requests.** `gitops_propose_change` and
`provision_propose_xr` open a PR in Gitea against the environments or
packages repositories, under the platform's write policy, and return the PR
URL. Nothing a connected agent can do changes a cluster directly.

**Budgets apply.** Per-group token budgets and request rates are enforced at
the gateway; a `429` means the budget for your group is spent for the window,
and no client-side retry changes that.

## 6. Local development: one server, no gateway

```bash
uv run adhar-ai mcp --domain cluster --listen=:18101
```

```json
{ "mcpServers": { "adhar-cluster": { "type": "http", "url": "http://localhost:18101/mcp" } } }
```

Tool names are **unprefixed** here (`list_pods`), there is no token, and
`docker compose up` brings up all seven on ports `8090`–`8096`. See
[GETTING_STARTED.md §4–6](GETTING_STARTED.md#4-run-one-mcp-server).

## 7. When it does not work

| Symptom | Cause | Fix |
|---|---|---|
| `401 Unauthorized` on every call | token expired or missing | re-mint with `adhar auth token`; re-add the server (Claude Code) or paste the new token |
| `403` on `gitops_propose_change` / `provision_propose_xr` only | your token lacks `platform-admin` | ask for the group; reads keep working |
| `403` on everything | token lacks `platform-developer` | same, for the developer group |
| tool list is shorter than 29 | one MCP server is down; the gateway serves what is up | `kubectl -n adhar-system get pods -l app.kubernetes.io/part-of=adhar-ai`; `GET https://ai.<host>/healthz` lists `mcp_servers_unreachable` |
| `429` | group budget spent | wait for the window; budgets are in `ai/agentgateway` |
| works from `curl`, not from the client | the client sent no `Accept: text/event-stream` | upgrade the client; streamable HTTP requires it |
| TLS error | internal CA on a local/self-signed platform | trust the platform CA on the workstation (`adhar get ca`) or use the local single-server path |

A first probe without any client:

```bash
curl -s https://mcp.<your-host>/mcp \
  -H "Authorization: Bearer $(adhar auth token)" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | head -c 600
```
