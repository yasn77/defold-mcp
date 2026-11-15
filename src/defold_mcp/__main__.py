"""Command-line entry point for the ``defold-mcp`` server.

``--transport stdio`` (the default) serves JSON-RPC over the process's
stdin/stdout, which is what every MCP harness expects; ``streamable-http``
serves the same server over HTTP for remote use. All diagnostics are
written to stderr — under the stdio transport stdout carries nothing but
JSON-RPC frames.
"""

from __future__ import annotations

import argparse
import logging
import sys

from defold_mcp.server import build_server

DEFAULT_PORT = 9810


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="defold-mcp",
        description="Model Context Protocol server for the Defold game engine.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="transport to serve on (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="host to bind for --transport streamable-http (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"port to bind for --transport streamable-http (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="stderr logging level (default: INFO)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and serve the Defold MCP server."""
    args = _build_parser().parse_args(argv)

    # stdout belongs to the JSON-RPC transport; diagnostics go to stderr.
    logging.basicConfig(
        stream=sys.stderr,
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    server = build_server()
    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(
            transport="streamable-http", host=args.host, port=args.port
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
