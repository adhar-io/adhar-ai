"""`adhar-ai` entrypoint.

Argument shapes match what the platform package's Deployments pass verbatim:

    mcp      --domain=<domain> --listen=:8080     (mcp-servers.yaml)
    gateway  --listen=:8080                       (llm-gateway.yaml)
    runtime  --config=/etc/adhar-ai/config.yaml --listen=:8080   (agent-runtime.yaml)

One image serves all nine published names; the subcommand selects the role.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence

from .config import DOMAINS, LLMConfig, MCPConfig, RuntimeEnv, parse_listen

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="adhar-ai", description="Adhar AI (ADR-0024)")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "info"))
    sub = parser.add_subparsers(dest="command", required=True)

    mcp = sub.add_parser("mcp", help="run one MCP tool server")
    mcp.add_argument("--domain", default=None, choices=[*DOMAINS, None])
    mcp.add_argument("--listen", default=":8080")

    gateway = sub.add_parser("gateway", help="run the provider-agnostic LLM gateway")
    gateway.add_argument("--listen", default=":8080")

    runtime = sub.add_parser("runtime", help="run the agent runtime")
    runtime.add_argument("--config", default=os.environ.get("ADHAR_AI_CONFIG"))
    runtime.add_argument("--listen", default=":8080")

    index = sub.add_parser("index", help="re-index the docs tree into pgvector (RAG)")
    index.add_argument("--docs", default=None)
    index.add_argument("--dsn", default=None)

    sub.add_parser("tools", help="print the tool inventory of every domain and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format=LOG_FORMAT, stream=sys.stderr)

    if args.command == "tools":
        return _print_tools()
    if args.command == "index":
        return _reindex(args.docs, args.dsn)

    import uvicorn

    host, port = parse_listen(args.listen)

    if args.command == "mcp":
        from .mcp.server import build_app

        mcp_cfg = MCPConfig.from_env(args.domain)
        logging.getLogger("adhar_ai").info(
            "mcp domain=%s write_enabled=%s listening on %s:%d/mcp",
            mcp_cfg.domain,
            mcp_cfg.gitea.write_enabled,
            host,
            port,
        )
        uvicorn.run(build_app(mcp_cfg), host=host, port=port, log_level=args.log_level)
        return 0

    if args.command == "gateway":
        from .gateway.app import create_app

        llm_cfg = LLMConfig.from_env()
        logging.getLogger("adhar_ai").info(
            "gateway provider=%s model=%s keyed=%s listening on %s:%d",
            llm_cfg.provider,
            llm_cfg.model,
            llm_cfg.keyed,
            host,
            port,
        )
        uvicorn.run(create_app(llm_cfg), host=host, port=port, log_level=args.log_level)
        return 0

    from .runtime.app import create_app as create_runtime_app
    from .runtime.autonomy import RuntimeConfig

    if args.config:
        os.environ["ADHAR_AI_CONFIG"] = args.config
    runtime_cfg = RuntimeConfig.load(args.config)
    logging.getLogger("adhar_ai").info(
        "runtime autonomy=%s operators=%s listening on %s:%d",
        runtime_cfg.default_autonomy,
        sorted(runtime_cfg.operators),
        host,
        port,
    )
    uvicorn.run(create_runtime_app(runtime_cfg), host=host, port=port, log_level=args.log_level)
    return 0


def _print_tools() -> int:
    import asyncio
    import json

    from .mcp.server import build_server, tool_access

    inventory: dict[str, list[dict[str, str]]] = {}
    for domain in DOMAINS:
        server = build_server(MCPConfig.from_env(domain))
        tools = asyncio.run(server.list_tools())
        inventory[domain] = [
            {"name": t.name, "access": tool_access(t)}
            for t in tools
        ]
    print(json.dumps(inventory, indent=2))
    return 0


def _reindex(docs: str | None, dsn: str | None) -> int:
    import asyncio

    from .rag import ingest_path, load_embeddings
    from .runtime.autonomy import RuntimeConfig

    environment = RuntimeEnv.from_env()
    cfg = RuntimeConfig.load(os.environ.get("ADHAR_AI_CONFIG"))
    target_dsn = dsn or environment.rag_dsn
    target_docs = docs or environment.docs_path
    if not target_dsn:
        print("no RAG DSN: set ADHAR_AI_RAG_DSN or pass --dsn", file=sys.stderr)
        return 2

    async def go() -> int:
        embeddings = await load_embeddings(environment.llm_gateway_url)
        count = await ingest_path(target_dsn, target_docs, embeddings, table=cfg.rag_table)
        print(f"indexed {count} chunks from {target_docs} using {embeddings.name} embeddings")
        return 0

    return asyncio.run(go())


if __name__ == "__main__":
    raise SystemExit(main())
