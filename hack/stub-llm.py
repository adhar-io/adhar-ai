"""A scripted OpenAI-compatible chat-completions server.

Stands in for the LLM gateway in `e2e-local.sh`. The point is NOT to test the
model — it is to run the agent loop, the task queue and the orchestrator over
real HTTP, against a real MCP tool surface, which is where wiring bugs live and
where the in-process fakes cannot reach.

It answers by inspecting the prompt it is given, so one server serves every
scenario the script drives.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


def _message(content=None, tool=None, args=None):
    message = {"role": "assistant", "content": content}
    if tool:
        message["tool_calls"] = [
            {
                "id": f"call_{tool}",
                "type": "function",
                "function": {"name": tool, "arguments": json.dumps(args or {})},
            }
        ]
    return {"choices": [{"message": message, "finish_reason": "stop"}]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        return

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"] or 0)) or b"{}")
        messages = body.get("messages") or []
        tools = {t["function"]["name"] for t in (body.get("tools") or [])}
        called = [m for m in messages if m.get("role") == "tool"]

        # The NEWEST question, separated from everything before it. Matching a
        # marker anywhere in the request would match the replayed history too,
        # so every follow-up would be answered as though it were the first —
        # which is the exact bug these markers exist to detect.
        users = [m for m in messages if m.get("role") == "user"]
        asked = str(users[-1].get("content") or "") if users else ""
        earlier = "\n".join(
            str(m.get("content") or "") for m in messages if m is not (users[-1] if users else None)
        )

        if "FIRST-TURN" in asked:
            reply = _message("the-first-answer")
        elif "REMEMBER-TEST" in asked:
            # The ANSWER differs by what we were sent, so the assertion is on
            # the platform's behaviour rather than on this server's own log.
            reply = _message("yes-i-remember" if "the-first-answer" in earlier else "no-memory")
        elif "HANDOFF-TEST" in asked and "You are on call" in asked:
            # Only the incident agent forwards. Without narrowing it, the
            # receiving agent forwards too and the chain is refused as a loop.
            reply = _message("HANDOFF: security — this restart follows a policy denial")
        elif not called and "sync_status" in tools:
            reply = _message(tool="sync_status", args={})
        else:
            reply = _message("Answered, having checked the platform.")

        payload = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
