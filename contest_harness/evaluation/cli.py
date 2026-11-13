"""Prepare, launch and score reproducible harness benchmarks."""

import argparse
from math import isfinite
from pathlib import Path
from uuid import uuid4

from contest_harness.agent.options import add_solver_options
from contest_harness.evaluation.history import planning_history, report
from contest_harness.evaluation.launch import run
from contest_harness.evaluation.manifest import prepare
from contest_harness.evaluation.scoring import check
from contest_harness.settings import MODES
from contest_harness.telemetry.usage import budget_tenths


def add_inputs(parser):
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--archive", type=resolved)
    sources.add_argument("--source-dir", type=resolved)


def resolved(value):
    return Path(value).resolve()


def positive_number(value):
    number = float(value)
    if not isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Expected a finite positive number")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for operation in (prepare, run, check, report):
        command = commands.add_parser(operation.__name__)
        command.add_argument("--run-dir", required=operation in (run, check), type=resolved)
        if operation in (prepare, check):
            add_inputs(command)
        if operation != prepare:
            command.add_argument("--baseline", type=resolved)
        if operation == run:
            command.add_argument("--mode", choices=MODES, default="baseline")
            command.add_argument("--direct-publish", action="store_true")
            command.add_argument("--routing", action="store_true")
            command.add_argument("--cascade", action="store_true")
            command.add_argument("--routing-profile", type=resolved)
            command.add_argument("--planner", choices=("control", "static", "F"))
            add_solver_options(command)
            command.add_argument("--early-stop", action="store_true")
            command.add_argument("--sessions", type=int, choices=range(1, 6), default=1)
            command.add_argument("--run-timeout", type=positive_number)
            command.add_argument("--planning-history", type=resolved)
            command.add_argument("--cascade-lambda", type=float)
            command.add_argument("--cheap-steps", type=int)
            command.add_argument("--generation-limit", action="append", default=[], metavar="PROFILE=TOKENS")
            command.add_argument("--image", help="Launch the agent in an existing local Docker image")
            command.add_argument("--network", help="Docker network to use with --image")
            command.add_argument("--timeout", type=positive_number, default=3600)
            command.add_argument(
                "--proxy-kind",
                choices=("organizer", "test-double"),
                default="organizer",
                help="Record whether the run uses the organizer proxy or a test fixture",
            )
            command.add_argument(
                "--proxy-budget",
                type=positive_number,
                help="Record the external proxy limit without enforcing it locally",
            )
            command.add_argument(
                "--local-budget", type=budget_tenths,
                help="Enforce a shared local limit in output-equivalent tokens",
            )
        if operation == report:
            command.add_argument("--source-run", required=True, type=resolved)
        command.set_defaults(operation=operation)
    history = commands.add_parser("planning-history")
    history.add_argument("--output", required=True, type=resolved)
    history.add_argument("run_dirs", type=resolved, nargs="+")
    history.set_defaults(operation=planning_history)
    args = parser.parse_args()
    if args.command == "run" and args.network and not args.image:
        parser.error("--network requires --image")
    try:
        generated = args.command in {"prepare", "report"} and args.run_dir is None
        if generated:
            args.run_dir = Path("/tmp") / f"openjiuwen-benchmark-{uuid4().hex}"
        status = args.operation(args)
        if generated:
            print(args.run_dir)
        return status
    except (OSError, ValueError, KeyError) as error:
        parser.exit(2, f"benchmark {args.command}: {error}\n")
