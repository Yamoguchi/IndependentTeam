"""openJiuwen task harness with a shared proxy budget."""

import argparse
import asyncio
import logging
import os
import signal
import sys
from contextlib import ExitStack
from dataclasses import asdict
from importlib.metadata import version
from math import isfinite
from pathlib import Path
from tempfile import mkdtemp
from urllib.parse import urlsplit

from dotenv import dotenv_values
from openjiuwen.core.common.logging.log_config import configure_log_config

from contest_harness.agent.generation import (
    profile_limits,
)
from contest_harness.agent.options import SolverOptions, add_solver_options
from contest_harness.planning.forecast import PlanningHistory
from contest_harness.planning.routing import (
    DEFAULT_CHEAP_STEPS,
    DEFAULT_LAMBDA,
    TaskRouter,
    load_profile,
)
from contest_harness.provenance import ROOT, runtime_hashes
from contest_harness.scheduler import run
from contest_harness.settings import COMPACT_MODES, MAX_SESSIONS, MAX_STEPS, MODES, RUN_TIMEOUT
from contest_harness.telemetry.trace import RunTrace
from contest_harness.telemetry.usage import budget_tenths


async def run_interruptible(awaitable):
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    terminated = False

    def terminate():
        nonlocal terminated
        if not terminated:
            terminated = True
            task.cancel()

    loop.add_signal_handler(signal.SIGTERM, terminate)
    try:
        return await awaitable
    except asyncio.CancelledError:
        if not terminated:
            raise
        return 128 + signal.SIGTERM
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=MODES, help="Select an experiment. Omit for the competition configuration.")
    parser.add_argument("--routing", action="store_true", help="Enable the task router.")
    parser.add_argument("--cascade", action="store_true", help="Allow at most one calibrated escalation.")
    parser.add_argument("--routing-profile", type=Path, help="Use independently measured per-type model evidence.")
    parser.add_argument("--planner", choices=("control", "static", "F"),
                        help="Select directory order (control), one pre-run ranking (static), or legacy planner F.")
    parser.add_argument("--early-stop", action="store_true",
                        help="Enable evidence-based stopping and deferred retries (G over F).")
    parser.add_argument("--sessions", type=int, choices=range(1, MAX_SESSIONS + 1), default=1,
                        help="Concurrent task sessions. More than one selects planner F.")
    parser.add_argument("--run-timeout", type=float, default=RUN_TIMEOUT, metavar="SECONDS",
                        help="Run deadline, at most 3570 seconds.")
    parser.add_argument("--planning-history", type=Path, help="Use independent JSONL cost observations.")
    parser.add_argument("--cascade-lambda", type=float, default=DEFAULT_LAMBDA, metavar="POINTS_PER_TOKEN")
    parser.add_argument("--cheap-steps", type=int, default=DEFAULT_CHEAP_STEPS, metavar="STEPS",
                        help="Step limit for an initial LLM attempt with a calibrated alternative.")
    parser.add_argument(
        "--direct-publish", action="store_true",
        help="Publish an independently verified result without a separate submit_answer call.",
    )
    parser.add_argument(
        "--generation-limit", action="append", default=[], metavar="PROFILE=TOKENS",
        help="Override a minimal or B+C+D limit for short, analysis, structured, code or debug.",
    )
    parser.add_argument(
        "--local-budget", type=budget_tenths, metavar="TOKENS",
        help="Limit shared output-equivalent tokens, with at most one decimal place.",
    )
    parser.add_argument(
        "--trace-file",
        metavar="PATH",
        help="Write detailed JSONL to a new file or to stderr with '-'.",
    )
    parser.add_argument("--results-file", type=Path, help="Append task results to a JSONL file (or set OUT).")
    add_solver_options(parser)
    args = parser.parse_args()
    competition = args.mode is None
    if competition:
        args.direct_publish = True
        legacy_requested = (
            args.sessions > 1 or args.early_stop or args.routing or args.cascade
            or args.routing_profile or args.planning_history or args.planner == "F"
        )
        if legacy_requested:
            args.mode = "B+C+D"
            args.routing = True
            if args.planner != "control":
                args.planner = "F"
                args.early_stop = True
        else:
            args.mode = "minimal"
            args.planner = args.planner or "static"
    options = None
    if args.mode == "minimal":
        if (args.routing or args.cascade or args.routing_profile or args.planning_history
                or args.early_stop or args.sessions != 1 or args.planner == "F"):
            parser.error("Minimal mode uses one model and session. Legacy experiments require an explicit legacy --mode.")
        try:
            options = SolverOptions.parse(args)
        except ValueError as error:
            parser.error(str(error))
    elif (args.planner == "static" or args.feature or args.max_iterations or args.repeat_limit is not None
          or args.sequence_limit is not None or args.cache_entries is not None):
        parser.error("Static ordering and solver optimization switches require --mode minimal.")
    if args.sessions > 1 or args.early_stop:
        if args.planner == "control":
            parser.error("Multiple sessions and early stopping require planner F.")
        args.planner = "F"
    if not 0 < args.run_timeout <= RUN_TIMEOUT:
        parser.error(f"--run-timeout must be greater than 0 and at most {RUN_TIMEOUT} seconds.")
    config = {
        **dotenv_values(ROOT / ".env", interpolate=False),
        **os.environ,
    }
    if args.local_budget is None and config.get("HARNESS_TOKEN_BUDGET"):
        try:
            args.local_budget = budget_tenths(config["HARNESS_TOKEN_BUDGET"])
        except ValueError as error:
            parser.error(f"HARNESS_TOKEN_BUDGET: {error}")
    base_url = config.get("LLM_BASE_URL") or ""
    key = config.get("LLM_API_KEY") or ""
    model_name = config.get("LLM_MODEL") or "qwen3.8-27b-noreason"
    url = urlsplit(base_url)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or not key
    ):
        parser.error("Set LLM_BASE_URL and LLM_API_KEY in the environment or .env.")
    root = args.tasks_dir.resolve()
    if not root.is_dir():
        parser.error("--tasks-dir must be a directory.")

    # SDK logs can contain request and response bodies. Emit only our metrics.
    # Configure logging before importing the SDK model or ReAct modules.
    logging.disable(logging.CRITICAL)
    configure_log_config(
        {"output": [], "interface_output": [], "performance_output": []}
    )
    from contest_harness.agent.model import TracedModel
    from contest_harness.agent.model_profiles import PROFILES, REQUEST_TIMEOUT

    if model_name not in PROFILES:
        parser.error("LLM_MODEL must be one of " + ", ".join(sorted(PROFILES)))
    output_control = args.mode == "B+C+D" or (options is not None and options.features["output_control_enabled"])
    if args.generation_limit and not output_control:
        parser.error("--generation-limit requires enabled output control.")
    if (args.cascade or args.routing_profile or args.cascade_lambda != DEFAULT_LAMBDA
            or args.cheap_steps != DEFAULT_CHEAP_STEPS) and not args.routing:
        parser.error("Routing options require --routing.")
    if args.cascade and not args.routing_profile:
        parser.error("--cascade requires --routing-profile with model measurements.")
    if not isfinite(args.cascade_lambda) or args.cascade_lambda < 0:
        parser.error("--cascade-lambda must be finite and nonnegative.")
    if not 1 <= args.cheap_steps <= MAX_STEPS:
        parser.error(f"--cheap-steps must be between 1 and {MAX_STEPS}.")
    if args.routing_profile and args.routing_profile.resolve().is_relative_to(root):
        parser.error("--routing-profile must be outside --tasks-dir.")
    if args.planning_history and (
        not args.planner or args.planning_history.resolve().is_relative_to(root)
    ):
        parser.error("--planning-history requires --planner and must be outside --tasks-dir.")
    try:
        profiles = load_profile(args.routing_profile, model_name, set(PROFILES))
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        parser.error(f"Cannot load routing profile ({type(error).__name__}).")
    try:
        history = PlanningHistory.load(args.planning_history)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        parser.error(f"Cannot load planning history ({type(error).__name__}).")
    router = (
        TaskRouter(model_name, profiles, args.cascade, args.cascade_lambda, args.cheap_steps) if args.routing else None
    )
    generation_limits = None
    if output_control:
        try:
            generation_limits = profile_limits(args.generation_limit, PROFILES[model_name][0])
        except ValueError as error:
            parser.error(str(error))
    with ExitStack() as stack:
        result_stream = None
        results_path = args.results_file or (Path(config["OUT"]) if config.get("OUT") else None)
        if results_path:
            results_path = results_path.resolve()
            if results_path.is_relative_to(root) and results_path.parent != root:
                parser.error("--results-file/OUT cannot write inside a task directory.")
            try:
                result_stream = stack.enter_context(results_path.open("a", encoding="utf-8"))
            except OSError as error:
                parser.error(f"Cannot create results file ({type(error).__name__}).")
        stream = None
        if competition and args.trace_file is None:
            args.trace_file = str(Path(mkdtemp(prefix="openjiuwen-run-", dir="/tmp")) / "trace.jsonl")
            print(f"Trace: {args.trace_file}", file=sys.stderr, flush=True)
        if args.trace_file == "-":
            stream = sys.stderr
        elif args.trace_file:
            trace_path = Path(args.trace_file).resolve()
            if trace_path.is_relative_to(root):
                parser.error("--trace-file must be outside --tasks-dir.")
            try:
                stream = stack.enter_context(
                    trace_path.open("x", encoding="utf-8", buffering=1)
                )
            except OSError as error:
                parser.error(f"Cannot create --trace-file ({type(error).__name__}).")
        trace = RunTrace(stream, redact=(key, base_url), limit_tenths=args.local_budget)
        model = TracedModel(trace, model_name, base_url, key)
        models = {model_name: model}
        for alternative in sorted({name for group in profiles.values() for name in group} - {model_name}):
            models[alternative] = TracedModel(trace, alternative, base_url, key)
        if trace.enabled:
            trace.emit(
                "run.started",
                model=model_name,
                sdk_version=version("openjiuwen"),
                python=sys.version.split()[0],
                platform=sys.platform,
                max_steps=MAX_STEPS if options is None else None,
                cheap_max_steps=args.cheap_steps if args.routing else None,
                task_order="priority" if args.planner in {"F", "static"} else "directory_name",
                planner=args.planner,
                early_stop=args.early_stop,
                sessions=args.sessions,
                run_timeout_seconds=args.run_timeout,
                mode=args.mode,
                context_policy="task_state" if (args.mode == "B" or args.mode in COMPACT_MODES
                    or options and options.features["context_management_enabled"]) else "full_history",
                tool_output_policy=(
                    "by_task_type" if options and options.features["tool_output_reduction_enabled"] is None else
                    "compact" if (args.mode in COMPACT_MODES or options and options.compact_output("data_analysis")) else
                    "original"
                ),
                solver_options=asdict(options) if options is not None else None,
                generation_limits=generation_limits,
                direct_publish=args.direct_publish,
                routing=args.routing,
                cascade=args.cascade,
                cascade_lambda=args.cascade_lambda if args.routing else None,
                routing_models=sorted(models),
                max_retries=0,
                timeout_seconds=REQUEST_TIMEOUT,
                local_limit_tenths=trace.limit_tenths,
                reservation_policy="wire_utf8_bytes_plus_model_and_item_overhead",
                output_profiles={
                    name: {"max_tokens": cap, "reasoning_margin": reasoning, "input_margin": template}
                    for name, (cap, reasoning, template) in PROFILES.items()
                },
                code_sha256=runtime_hashes(),
            )
        try:
            return asyncio.run(run_interruptible(run(
                model, model_name, root, trace, args.mode, generation_limits,
                direct_publish=args.direct_publish, router=router, models=models,
                planner=args.planner, history=history, sessions=args.sessions, run_timeout=args.run_timeout,
                early_stop=args.early_stop, result_stream=result_stream, options=options,
            )))
        except ValueError as error:
            parser.error(str(error))
