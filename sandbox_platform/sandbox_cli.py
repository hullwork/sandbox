"""User-facing CLI for the Sandbox SDK facade."""

from __future__ import annotations

import argparse
import json
import sys

from sandbox_platform.sandbox_client import MANAGER, ControlPlaneError, Sandbox


def _command(values: list[str]) -> tuple[str, list[str]]:
    if values and values[0] == "--":
        values = values[1:]
    if not values:
        raise ValueError("a command is required after --")
    return values[0], values[1:]


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _run(sandbox: Sandbox, values: list[str], timeout: int) -> int:
    command, args = _command(values)
    result = sandbox.run_command(command, args, timeout_seconds=timeout)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sandbox",
        description="Create and use Sandbox Workspaces and Runtimes",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    create = subparsers.add_parser("create", help="create or resume a sandbox")
    create.add_argument(
        "name",
        nargs="?",
        help="Workspace name; omit for a throwaway one. Only `run` spells this "
        "as --name, because its own positional is the command to execute",
    )
    create.add_argument("--template", help="runtime template id (see `sandboxctl templates`)")
    create.add_argument("--json", action="store_true", help="output raw JSON")

    run = subparsers.add_parser("run", help="create/resume and run a command")
    run.add_argument(
        "--name",
        help="Workspace name; omit for a throwaway one. A flag here, and a "
        "positional in create/exec/stop, because the positional is the command",
    )
    run.add_argument("--template", help="runtime template id (see `sandboxctl templates`)")
    run.add_argument("--timeout", type=int, default=30, help="seconds to wait for the command")
    run.add_argument(
        "--stop",
        action="store_true",
        help="release the Runtime afterwards; the Workspace and its files stay",
    )
    run.add_argument("command", nargs=argparse.REMAINDER, help="command to run, after --")

    execute = subparsers.add_parser("exec", help="run in an existing sandbox")
    execute.add_argument("name", help="Workspace name, positional (not --name)")
    execute.add_argument("--timeout", type=int, default=30, help="seconds to wait for the command")
    execute.add_argument("command", nargs=argparse.REMAINDER, help="command to run, after --")

    stop = subparsers.add_parser("stop", help="stop a sandbox Runtime")
    stop.add_argument("name", help="Workspace name, positional (not --name)")
    stop.add_argument("--json", action="store_true", help="output raw JSON")

    listing = subparsers.add_parser("list", help="list active Runtimes")
    listing.add_argument("--json", action="store_true", help="output raw JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "create":
            sandbox = Sandbox.create(args.name, template=args.template)
            value = {
                "name": sandbox.name,
                "workspace_id": sandbox.workspace_id,
                "sandbox_id": sandbox.sandbox_id,
            }
            if args.json:
                _print_json(value)
            else:
                print(f"{sandbox.name}\t{sandbox.sandbox_id}\t{sandbox.workspace_id}")
            return 0
        if args.action == "run":
            _command(args.command)
            sandbox = (
                Sandbox.get_or_create(args.name, template=args.template)
                if args.name
                else Sandbox.create(template=args.template)
            )
            try:
                return _run(sandbox, args.command, args.timeout)
            finally:
                if args.stop:
                    try:
                        sandbox.stop()
                    except ControlPlaneError as exc:
                        # The command's exit code is what the caller asked
                        # for. A failed stop is said on stderr and must not
                        # replace it - the output above already went out.
                        print(f"sandbox: stop failed: {exc}", file=sys.stderr)
        if args.action == "exec":
            _command(args.command)
            return _run(Sandbox.get(args.name, resume=True), args.command, args.timeout)
        if args.action == "stop":
            result = Sandbox.get(args.name).stop()
            _print_json(result) if args.json else print(f"stopped {args.name}")
            return 0
        if args.action == "list":
            runtimes = MANAGER.list_runtimes()
            if args.json:
                _print_json({"sandboxes": runtimes})
            else:
                for runtime in runtimes:
                    print(
                        "\t".join(
                            str(runtime.get(key, ""))
                            for key in ("id", "workspace_id", "status", "template")
                        )
                    )
            return 0
    except (ControlPlaneError, RuntimeError, TypeError, ValueError) as exc:
        print(f"sandbox: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
