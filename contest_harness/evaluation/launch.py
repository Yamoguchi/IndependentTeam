"""Run the agent locally or in Docker and capture execution artifacts."""

import json
import os
import platform
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from time import monotonic
from uuid import uuid4

from contest_harness.evaluation.artifacts import write_json
from contest_harness.evaluation.inputs import actual_inventory, fingerprint, inventory_digest
from contest_harness.evaluation.manifest import ROOT, code_metadata, load_manifest, now
from contest_harness.evaluation.report import generate

DOCKER_LIMITS = (
    "--read-only",
    "--tmpfs",
    "/tmp:rw,size=2g",
    "-e",
    "HOME=/tmp",
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges",
    "--pids-limit",
    "512",
    "--cpus",
    "4",
    "--memory",
    "16g",
)


def image_id(image):
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise ValueError(f"Cannot inspect local Docker image {image!r}: {result.stderr.strip()}")
    return result.stdout.strip()


def local_versions():
    versions = {}
    for name in ("openjiuwen", "openai", "httpx", "python-dotenv"):
        try:
            versions[name] = version(name)
        except PackageNotFoundError:
            versions[name] = None
    return versions


def run(args):
    root = args.run_dir
    tasks = root / "tasks"
    manifest = load_manifest(root)
    if manifest["launch"]["status"] != "prepared":
        raise ValueError("Run requires a freshly prepared directory")
    if (
        not tasks.is_dir()
        or tasks.is_symlink()
        or (inventory_digest(actual_inventory(tasks)) != manifest["input"]["tree_sha256"])
    ):
        raise ValueError("Prepared inputs changed. Prepare a new directory before running")
    journal = root / "journal"
    profile = getattr(args, "routing_profile", None)
    if profile and (not profile.is_file() or profile.is_relative_to(tasks)):
        raise ValueError("Use a routing profile outside the task inputs")
    planner = getattr(args, "planner", None)
    early_stop = getattr(args, "early_stop", False)
    sessions = getattr(args, "sessions", 1)
    run_timeout = getattr(args, "run_timeout", None)
    if sessions > 1 or early_stop:
        if planner == "control":
            raise ValueError("Multiple sessions and early stopping require planner F.")
        planner = "F"
    history = getattr(args, "planning_history", None)
    if history and (not planner or not history.is_file() or history.is_relative_to(tasks)):
        raise ValueError("Planning history requires a planner and a file outside task inputs")
    container = None
    if args.image:
        image = image_id(args.image)
        container = f"openjiuwen-benchmark-{uuid4().hex}"
        command = [
            "docker",
            "run",
            "--rm",
            "--name",
            container,
            *DOCKER_LIMITS,
            "--pull",
            "never",
            "-e",
            "LLM_BASE_URL",
            "-e",
            "LLM_API_KEY",
            "-e",
            "LLM_MODEL",
        ]
        if args.network:
            command.extend(["--network", args.network])
        if profile:
            command.extend(["-v", f"{profile}:/app/router-profile.json:ro"])
        if history:
            command.extend(["-v", f"{history}:/app/planning-history.jsonl:ro"])
        command.extend(
            [
                "-v",
                f"{tasks}:/data/tasks:rw",
                "-v",
                f"{journal}:/data/journal:rw",
                image,
                "--tasks-dir",
                "/data/tasks",
                "--trace-file",
                "/data/journal/trace.jsonl",
            ]
        )
    else:
        image = None
        command = [
            sys.executable,
            str(ROOT / "run_agent.py"),
            "--tasks-dir",
            str(tasks),
            "--trace-file",
            str(journal / "trace.jsonl"),
        ]
    if args.baseline:
        load_manifest(args.baseline)
        json.loads((args.baseline / "summary.json").read_text())
    if args.local_budget is not None:
        whole, fraction = divmod(args.local_budget, 10)
        command.extend(["--local-budget", f"{whole}.{fraction}"])
    mode = getattr(args, "mode", "baseline")
    command.extend(["--mode", mode])
    direct_publish = getattr(args, "direct_publish", False)
    if direct_publish:
        command.append("--direct-publish")
    routing = getattr(args, "routing", False)
    cascade = getattr(args, "cascade", False)
    penalty = getattr(args, "cascade_lambda", None)
    cheap_steps = getattr(args, "cheap_steps", None)
    if routing:
        command.append("--routing")
    if cascade:
        command.append("--cascade")
    if profile:
        command.extend(["--routing-profile", "/app/router-profile.json" if args.image else str(profile)])
    if planner:
        command.extend(["--planner", planner])
    if early_stop:
        command.append("--early-stop")
    if sessions != 1:
        command.extend(["--sessions", str(sessions)])
    if run_timeout is not None:
        command.extend(["--run-timeout", str(run_timeout)])
    if history:
        command.extend(["--planning-history", "/app/planning-history.jsonl" if args.image else str(history)])
    if penalty is not None:
        command.extend(["--cascade-lambda", str(penalty)])
    if cheap_steps is not None:
        command.extend(["--cheap-steps", str(cheap_steps)])
    for entry in getattr(args, "generation_limit", ()):
        command.extend(["--generation-limit", entry])
    journal.mkdir(exist_ok=False)  # Exclusive launch, including when two runners race.
    manifest["code"] = code_metadata()
    manifest["report_code"] = manifest["code"]
    manifest["launch"] = {
        "status": "running",
        "mode": "baseline" if args.local_budget is None else "local_budget",
        "agent_mode": mode,
        "direct_publish": direct_publish,
        "routing": routing,
        "cascade": cascade,
        "cascade_lambda": penalty,
        "cheap_steps": cheap_steps,
        "routing_profile_sha256": fingerprint(profile) if profile else None,
        "planner": planner,
        "early_stop": early_stop,
        "sessions": sessions,
        "run_timeout_seconds": run_timeout,
        "planning_history_sha256": fingerprint(history) if history else None,
        "started_at": now(),
        "command": command,
        "backend": "docker" if args.image else "native",
        "model": os.environ.get("LLM_MODEL") or None,
        "image_id": image,
        "image_ref": args.image,
        "cpu_limit": 4 if args.image else None,
        "memory_limit": "16g" if args.image else None,
        "network": args.network or ("default" if args.image else "host"),
        "proxy_kind": args.proxy_kind,
        "platform": platform.platform(),
        "local_python": platform.python_version(),
        "local_packages": local_versions() if not args.image else None,
        "time_limit_seconds": args.timeout,
        "proxy_budget_output_equivalent": args.proxy_budget,
        "local_limit_tenths": args.local_budget,
        "budget_enforced_by_launcher": False,
    }
    write_json(root / "run-manifest.json", manifest)
    started = monotonic()
    code = None
    status = "interrupted"
    try:
        with (
            (root / "agent-stdout.jsonl").open("xb") as stdout,
            (root / "agent-stderr.log").open("xb") as stderr,
        ):
            result = subprocess.run(command, cwd=ROOT, stdout=stdout, stderr=stderr, timeout=args.timeout)
        code = result.returncode
        status = "exited"
    except subprocess.TimeoutExpired:
        code, status = 124, "timeout"
    except KeyboardInterrupt:
        code = 130
    except OSError:
        code, status = 1, "launch_error"
        raise
    finally:
        launch = manifest["launch"]
        launch.update(
            status=status, exit_code=code, finished_at=now(), wall_seconds=round(monotonic() - started, 3)
        )
        if container and status != "exited":
            try:
                cleanup = subprocess.run(
                    ["docker", "rm", "-f", container],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=20,
                    check=False,
                )
                launch["container_cleanup_exit_code"] = cleanup.returncode
            except (OSError, subprocess.TimeoutExpired) as error:
                launch["container_cleanup_error"] = type(error).__name__
        write_json(root / "run-manifest.json", manifest)
        generate(root, root, manifest, args.baseline)
    return code
