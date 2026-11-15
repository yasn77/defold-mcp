"""Command-line entry point for the ``defold-mcp`` server and its helpers.

``--transport stdio`` (the default) serves JSON-RPC over the process's
stdin/stdout, which is what every MCP harness expects; ``streamable-http``
serves the same server over HTTP for remote use. All diagnostics are
written to stderr — under the stdio transport stdout carries nothing but
JSON-RPC frames.

Two subcommands sit alongside the server and leave the serve path untouched:
``skill install`` copies the portable agent skill into the harness skill
directories, and ``integration install`` merges the server registration into a
harness configuration.
"""

from __future__ import annotations

import argparse
import logging
import shlex
import sys
from pathlib import Path

from defold_mcp.integrations import HARNESSES
from defold_mcp.server import build_server

DEFAULT_PORT = 9810


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="defold-mcp",
        description="Model Context Protocol server for the Defold game engine.",
    )
    # Serve options stay at the top level so a bare `defold-mcp --transport …`
    # keeps working exactly as before; they apply only when no subcommand runs.
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

    subcommands = parser.add_subparsers(dest="command")

    skill = subcommands.add_parser("skill", help="manage the portable agent skill")
    skill_commands = skill.add_subparsers(dest="skill_command", required=True)
    skill_install = skill_commands.add_parser(
        "install", help="copy the skill into every detected harness directory"
    )
    skill_install.add_argument(
        "--target",
        type=Path,
        default=None,
        help="install into this directory only, instead of auto-detecting harnesses",
    )

    integration = subcommands.add_parser(
        "integration", help="register the server with a harness"
    )
    integration_commands = integration.add_subparsers(
        dest="integration_command", required=True
    )
    integration_install = integration_commands.add_parser(
        "install", help="merge the defold entry into a harness configuration"
    )
    integration_install.add_argument(
        "harness",
        choices=(*HARNESSES, "all"),
        help="harness to register, or 'all' to attempt every harness config",
    )
    integration_install.add_argument(
        "--command",
        dest="command_override",
        default=None,
        metavar="COMMAND",
        help=(
            "override the registered server command as one shell string, "
            'split with shlex (e.g. --command "python -m defold_mcp")'
        ),
    )
    integration_install.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resulting configuration without writing it",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Dispatch to the server or one of its subcommands."""
    args = _build_parser().parse_args(argv)
    if args.command == "skill":
        return _install_skill_command(args)
    if args.command == "integration":
        return _install_integration_command(args)
    return _serve(args)


def _serve(args: argparse.Namespace) -> int:
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


def _install_skill_command(args: argparse.Namespace) -> int:
    from defold_mcp.skill import (
        DEFAULT_SKILL_TARGET,
        detected_targets,
        install_skill,
        installed_file_count,
    )

    if args.target is not None:
        targets = [("target", Path(args.target))]
    else:
        targets = detected_targets()
        if not targets:
            targets = [("default", DEFAULT_SKILL_TARGET)]

    status = 0
    for label, target in targets:
        try:
            installed = install_skill(target)
        except (OSError, RuntimeError) as exc:
            print(f"{label}: failed: {exc}", file=sys.stderr)
            status = 1
            continue
        print(f"{label}: installed {installed_file_count(installed)} files into {installed.parent}")
    return status


def _install_integration_command(args: argparse.Namespace) -> int:
    from defold_mcp.integrations import default_env, register, resolved_command

    # --command is one shell string; an empty or whitespace-only value falls
    # back to the resolved executable rather than registering nothing.
    command = shlex.split(args.command_override) if args.command_override else []
    if not command:
        command = resolved_command()
    env = default_env()
    keys = list(HARNESSES) if args.harness == "all" else [args.harness]

    status = 0
    for key in keys:
        result = register(key, command=command, env=env, dry_run=args.dry_run)
        label = result["harness"]
        if result.get("error"):
            print(f"{label}: skipped: {result['error']}")
            if args.harness != "all":
                status = 1
        elif result.get("dry_run"):
            verb = "would replace the existing entry in" if result["replaced"] else "would write"
            print(f"{label}: dry run, {verb} {result['path']}")
            print(result["preview"], end="")
        elif result["installed"]:
            backup = f", backup {result['backup']}" if result.get("backup") else ""
            verb = "replaced existing entry in" if result["replaced"] else "registered in"
            print(f"{label}: {verb} {result['path']}{backup}")
        else:
            print(f"{label}: already registered in {result['path']} (unchanged)")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
