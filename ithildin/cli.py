"""Canonical public CLI for Ithildin."""

from __future__ import annotations

import argparse
from ithildin.providers import doj as doj_provider
from ithildin.providers import registry as registry_provider
from ithildin.publish.build import run_build
from ithildin.queue import cli as queue_cli
from ithildin.trackers import findings, leads


TRACKER_COMMANDS = {
    "lead": leads.run,
    "finding": findings.run,
    "queue": queue_cli.run,
}

QUERY_PROVIDERS = {
    "registry": registry_provider.run,
    "doj": doj_provider.run,
}


def cmd_passthrough(args: argparse.Namespace) -> int:
    return TRACKER_COMMANDS[args.command](list(args.args))


def cmd_query(args: argparse.Namespace) -> int:
    return QUERY_PROVIDERS[args.provider](list(args.args))


def cmd_build(args: argparse.Namespace) -> int:
    return run_build(args.target)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ithildin",
        description="Canonical CLI for Ithildin investigative tooling.",
    )
    sub = parser.add_subparsers(dest="command")

    for name in ("lead", "finding", "queue"):
        cmd = sub.add_parser(name, help=f"Run {name} workflows")
        cmd.add_argument("args", nargs=argparse.REMAINDER)
        cmd.set_defaults(func=cmd_passthrough)

    query = sub.add_parser("query", help="Run query providers")
    query.add_argument("provider", choices=sorted(QUERY_PROVIDERS))
    query.add_argument("args", nargs=argparse.REMAINDER)
    query.set_defaults(func=cmd_query)

    build = sub.add_parser("build", help="Run build/export tasks")
    build.add_argument(
        "target",
        choices=["demo", "web", "search-index", "preview-index"],
        nargs="?",
        default="web",
    )
    build.set_defaults(func=cmd_build)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
