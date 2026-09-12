#!/usr/bin/env bash
#
# e2e-local.sh — stand the whole agentic layer up locally and assert it works.
#
# The unit suite drives tools in-process and fakes the toolbox, which is fast and
# misses exactly the class of defect that only appears once real processes talk
# to each other over HTTP: the MCP transport's Host-header guard, the client's
# result unwrapping, and whether an unconfigured backend's error text survives
# the trip to the model. This script exercises that seam.
#
# It needs no cluster, no LLM key and no database. Everything it asserts is
# either true offline or honestly reported as unavailable — which is itself the
# property being tested.
#
#   ./hack/e2e-local.sh              # uses ../adhar/docs for grounding if present
#   DOCS=/path/to/docs ./hack/e2e-local.sh
#
set -euo pipefail

BASE_PORT="${BASE_PORT:-18301}"
RUNTIME_PORT="${RUNTIME_PORT:-18310}"
DOCS="${DOCS:-../adhar/docs}"
WORK="$(mktemp -d)"
DOMAINS=(cluster gitops provision observability security cost catalog)

pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
step() { printf '\n\033[1m%s\033[0m\n' "$1"; }
FAILURES=0

cleanup() {
  [[ -f "${WORK}/pids" ]] && while read -r pid; do kill "${pid}" 2>/dev/null || true; done < "${WORK}/pids"
  rm -rf "${WORK}"
}
trap cleanup EXIT

wait_for() {
  local url="$1" tries="${2:-60}"
  for _ in $(seq 1 "${tries}"); do
    curl -fsS "${url}" >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  return 1
}

# --------------------------------------------------------------- MCP servers --
step "Starting the seven MCP servers"
port="${BASE_PORT}"
for d in "${DOMAINS[@]}"; do
  uv run adhar-ai mcp --domain "${d}" --listen=":${port}" >"${WORK}/mcp-${d}.log" 2>&1 &
  echo $! >> "${WORK}/pids"
  port=$((port + 1))
done

port="${BASE_PORT}"
for d in "${DOMAINS[@]}"; do
  if wait_for "http://127.0.0.1:${port}/healthz"; then
    pass "mcp-${d} is serving on :${port}"
  else
    fail "mcp-${d} never became healthy (see ${WORK}/mcp-${d}.log)"
  fi
  port=$((port + 1))
done

# -------------------------------------------------------------- Host headers --
step "MCP transport accepts the Host headers it receives in a cluster"
# agentgateway federates these through a Service backendRef, so the Host is the
# Service DNS name or a Pod IP. The SDK's transport defaults to a localhost-only
# DNS-rebinding allow-list, which answers all of these 421 and silently takes out
# the entire federated tool surface. A health probe cannot catch it: the probe is
# the one caller that legitimately uses localhost.
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"e2e","version":"1"}}}'
for host in \
  "adhar-ai-mcp-cluster.adhar-system.svc.cluster.local:8080" \
  "adhar-ai-mcp-cluster.adhar-system.svc:8080" \
  "10.244.1.7:8080" \
  "mcp.adhar.localtest.me"
do
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:${BASE_PORT}/mcp" \
    -H "Host: ${host}" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d "${INIT}")
  [[ "${code}" == "200" ]] && pass "Host: ${host} -> 200" || fail "Host: ${host} -> ${code}"
done

# ------------------------------------------------------------------ runtime --
step "Starting the agent runtime against all seven"
{
  echo "autonomy: {default: suggest}"
  echo "limits: {maxSteps: 8, maxToolCallsPerOp: 20}"
  echo "rag: {enabled: true}"
  echo "mcpServers:"
  port="${BASE_PORT}"
  for d in "${DOMAINS[@]}"; do
    echo "  ${d}: http://127.0.0.1:${port}"
    port=$((port + 1))
  done
} > "${WORK}/config.yaml"

ADHAR_AI_DOCS_PATH="${DOCS}" \
LLM_GATEWAY_URL="http://127.0.0.1:1" \
  uv run adhar-ai runtime --config "${WORK}/config.yaml" --listen=":${RUNTIME_PORT}" \
  >"${WORK}/runtime.log" 2>&1 &
echo $! >> "${WORK}/pids"

if wait_for "http://127.0.0.1:${RUNTIME_PORT}/healthz"; then
  pass "runtime is serving on :${RUNTIME_PORT}"
else
  fail "runtime never became healthy (see ${WORK}/runtime.log)"
  exit 1
fi

HEALTH="${WORK}/health.json"
curl -fsS "http://127.0.0.1:${RUNTIME_PORT}/healthz" > "${HEALTH}"

step "The runtime mounted every domain and every tool"
uv run python - "${HEALTH}" <<'PY' && pass "7 domains, 27 tools, none unreachable" || fail "tool inventory is wrong"
import json, sys
h = json.load(open(sys.argv[1]))
assert len(h["mcp_servers_connected"]) == 7, h["mcp_servers_connected"]
assert not h["mcp_servers_unreachable"], h["mcp_servers_unreachable"]
assert len(h["tools"]) == 27, len(h["tools"])
for w in ("propose_change", "propose_xr", "propose_exception", "scaffold"):
    assert w in h["tools"], w
PY

step "Grounding works with no key and no database"
uv run python - "${HEALTH}" <<'PY' && pass "lexical retrieval is live" || fail "no grounding available"
import json, sys
mode = json.load(open(sys.argv[1]))["rag"]
assert "lexical" in mode, mode
print(f"      {mode}")
PY

step "An unauthenticated caller is answered but cannot write"
uv run python - "${HEALTH}" <<'PY' && pass "pinned to read-only with no credential" || fail "auth posture is wrong"
import json, sys
auth = json.load(open(sys.argv[1]))["auth"]
assert auth["unauthenticated"] == "answered as read-only", auth
PY

CHAT="${WORK}/chat.json"
curl -fsS "http://127.0.0.1:${RUNTIME_PORT}/chat" \
  -H 'content-type: application/json' \
  -d '{"prompt":"which applications are out of sync?","autonomy":"scoped"}' > "${CHAT}" || true
uv run python - "${CHAT}" <<'PY' && pass "a request cannot raise its own autonomy" || fail "autonomy was widened by the request body"
import json, sys
body = json.load(open(sys.argv[1]))
assert body["autonomy"] == "read-only", body["autonomy"]
assert body["principal"]["authenticated"] is False
PY

# ------------------------------------------------------------ honest errors --
step "An unconfigured backend names itself instead of inventing data"
uv run python - <<PY && pass "tool errors reach the model with their message" || fail "tool errors are masked or mis-reported"
import anyio, sys
from adhar_ai.runtime.toolbox import MCPToolbox

SERVERS = {d: f"http://127.0.0.1:{${BASE_PORT} + i}" for i, d in enumerate(
    ["cluster","gitops","provision","observability","security","cost","catalog"])}

async def main():
    tb = MCPToolbox(SERVERS)
    await tb.connect()
    try:
        out = await tb.call("promql", {"query": "up"})
        assert "error" in out, f"a failed call must surface as an error, got {out}"
        assert "Prometheus is not configured" in out["error"], out["error"]
        assert "PROMETHEUS_URL" in out["error"], out["error"]
        print(f"      {out['error'][:88]}")

        out = await tb.call("propose_change", {
            "repo": "packages", "title": "t", "why": "w",
            "changes": [{"path": "ai/x.yaml", "content": "a: b"}]})
        assert "error" in out and "read-only" in out["error"], out
        print(f"      {out['error'][:88]}")
    finally:
        await tb.aclose()

anyio.run(main)
PY

# ------------------------------------------------------------------ verdict --
step "Result"
if [[ "${FAILURES}" -eq 0 ]]; then
  printf '  \033[32mAll end-to-end checks passed.\033[0m\n\n'
else
  printf '  \033[31m%d check(s) failed.\033[0m Logs in %s\n\n' "${FAILURES}" "${WORK}"
  trap - EXIT
  exit 1
fi
