"""Run stage 11 ablations and budget sweeps on fresh copies of each dataset."""

import argparse
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from compare_early_stop import CASES, PAIR, ROOT, build_source, score_run

from contest_harness.evaluation.artifacts import write_json
from contest_harness.evaluation.budget_curve import chart, retrospective
from contest_harness.evaluation.inputs import clean_inputs, source_files
from contest_harness.evaluation.launch import DOCKER_LIMITS, run
from contest_harness.evaluation.manifest import load_manifest, prepare
from contest_harness.evaluation.metrics import comparison_conditions
from contest_harness.evaluation.report import generate
from contest_harness.evaluation.scoring import check
from contest_harness.telemetry.usage import budget_tenths

PERCENTS = (10, 25, 50, 75, 90, 100)
ARTIFACTS = (
    "run-manifest.json", "summary.json", "metrics.json", "results-by-type.json",
    "results-by-task.jsonl", "tokens.jsonl", "trace.jsonl", "comparison.json", "report.md", "score.jsonl",
)
MODEL = "qwen3.8-27b-noreason"
E2 = {"mode": "B+C+D", "routing": True}
E3 = {**E2, "cascade": True}
F = {**E3, "planner": "F"}
G = {**F, "early_stop": True}
ABLATIONS = {
    "baseline": {}, "A": {"mode": "A"}, "B": {"mode": "B"}, "C": {"mode": "B+C"},
    "D": {"mode": "B+C+D"}, "E1": {"mode": "B+C+D", "direct_publish": True},
    "E2": E2, "E3": E3, "E": E3, "control": {**E3, "planner": "control"},
    "F": F, "G": G, "F-5": {**F, "sessions": 5},
}
CORE_MODES = ("baseline", "A", "B", "C", "D", "E", "F", "G")
PREVIOUS = {
    "A": "baseline", "B": "A", "C": "B", "D": "C", "E1": "D", "E2": "D",
    "E3": "E2", "E": "D", "control": "E", "F": "E", "G": "F", "F-5": "F",
}


def report_budget_series(output, rows):
    chart(output / "score-vs-budget.svg", {"Сравнение режимов": rows})
    report = {}
    for mode in dict.fromkeys(row["mode"] for row in rows):
        points = [row for row in rows if row["mode"] == mode]
        caps = [row["limit_tenths"] for row in points]
        oracle, sample_size = retrospective(output / points[-1]["name"], caps)
        series = []
        for point, bound in zip(points, oracle, strict=True):
            metrics = json.loads((output / point["name"] / "metrics.json").read_text())
            remaining = metrics["budget"]["remaining_tenths"]
            series.append({
                "limit": point["limit_tenths"] / 10, "score": point["score"], "correct": point["correct"],
                "cost": point["output_equivalent"],
                "fraction": point["exact_tenths"] / point["limit_tenths"] if point["limit_tenths"] else None,
                "remaining": remaining / 10 if remaining is not None else None,
                "execution_order": metrics["execution_order"], "oracle": bound, "oracle_sample_size": sample_size,
            })
        report[mode] = series
        for point in points:
            directory = output / point["name"]
            manifest = load_manifest(directory)
            generate(directory, directory, {**manifest, "budget_series": series})
    write_json(output / "score-vs-budget.json", report)


def archived_fixtures(archive, solved):
    with source_files(archive, None) as (names, read):
        files, tasks = clean_inputs(names, read)
    for row in tasks:
        directory = row["directory"]
        task = json.loads(files[f"{directory}/task.json"])
        done = solved / directory
        answer = (done / "answer.txt").read_text(encoding="utf-8").strip()
        steps = []
        marker = None
        kind = task["task_type"]
        if kind == "coding":
            path = "mylib.py"
            original = files[f"{directory}/workspace/{path}"].decode()
            marker = "\n".join(original.strip().splitlines()[:2])
            steps = [
                ("read_file", {"path": path}),
                ("write_file", {"path": path, "content": (done / "workspace" / path).read_text(encoding="utf-8")}),
                ("run_python_file", {"path": "test_mylib.py"}),
            ]
        elif kind in {"data_analysis", "search"}:
            prefix = f"{directory}/workspace/"
            paths = [name.removeprefix(prefix) for name in files if name.startswith(prefix)]
            steps = [("read_file", {"path": path}) for path in paths[:2]]
        else:
            steps = [("run_python", {"code": f"print({answer!r})"})]
        steps.append(("submit_answer", {"answer": answer}))
        yield {"instruction": task["instruction"], "marker": marker, "steps": steps}


def mixed_fixtures(cases):
    for case in cases:
        paths = list(case.files) if case.answer is not None else (
            ["vault/closing.csv"] if case.key == "vault-close" else ["revisions"]
        )
        steps = [("read_file", {"path": path}) for path in paths]
        if case.answer is not None:
            steps.append(("submit_answer", {"answer": case.answer}))
        yield {"instruction": case.instruction, "steps": steps}


def docker_check(image, archive, output):
    mounts = [arg for name in ("benchmark.py", "contest_harness/evaluation")
              for arg in ("-v", f"{ROOT / name}:/app/{name}:ro")]
    completed = subprocess.run(
        ["docker", "run", "--rm", "--pull", "never", "--network", "none", *DOCKER_LIMITS, *mounts,
         "-v", f"{archive}:/data/reference.zip:ro", "-v", f"{output}:/data/run:rw",
         "--entrypoint", "python", image, "/app/benchmark.py", "check",
         "--archive", "/data/reference.zip", "--run-dir", "/data/run"],
        capture_output=True, text=True, timeout=180,
    )
    (output / "checker.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    if completed.returncode not in (0, 1) or not (output / "score.jsonl").is_file():
        raise RuntimeError(f"Checker failed for {output}")


def compare(args):
    args.output.mkdir(parents=True)
    source = args.output / "source"
    cases = PAIR if args.phase == "pair" else CASES
    if args.archive is None:
        build_source(source, cases)
    rows = []
    fingerprints = []

    def experiment(name, mode, cap, reference=None, previous=None):
        directory = args.output / name
        prepare(SimpleNamespace(run_dir=directory, source_dir=None if args.archive else source, archive=args.archive))
        flags = {"mode": "baseline" if mode == "baseline" else "B+C", **ABLATIONS[mode]}
        if flags.get("routing"):
            flags["routing_profile"] = args.profile
        status = run(SimpleNamespace(
            **flags, run_dir=directory, image=args.image, network=args.network, timeout=args.timeout,
            baseline=reference, local_budget=cap, proxy_budget=None, proxy_kind=args.proxy_kind,
        ))
        if args.archive and args.image:
            docker_check(args.image, args.archive, directory)
        elif args.archive:
            check(SimpleNamespace(run_dir=directory, archive=args.archive, source_dir=None, baseline=reference))
        else:
            score_run(directory, cases, reference)
        if previous:
            previous_dir = args.output / previous
            previous_manifest = load_manifest(previous_dir)
            previous_summary = json.loads((previous_dir / "summary.json").read_text())
            manifest = load_manifest(directory)
            manifest["previous_stage_reference"] = {
                "path": str(previous_dir), "summary": previous_summary,
                "content_sha256": previous_manifest["input"]["content_sha256"],
                "conditions": comparison_conditions(previous_manifest),
            }
            generate(directory, directory, manifest)
        if missing := [item for item in ARTIFACTS if not (directory / item).is_file()]:
            raise RuntimeError(f"Incomplete artifacts: {name}: {missing}")
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        manifest = load_manifest(directory)
        rows.append({"name": name, "mode": mode, "limit_tenths": cap,
                     "input_sha256": manifest["input"]["content_sha256"], "agent_exit": status, **summary})
        write_json(args.output / "matrix.json", rows)
        print(f"{name}: {summary['score']}/{summary['max_score']}; {summary['output_equivalent']}; {status}", flush=True)
        if (
            status not in {0, 1} or summary["status"] not in {"completed", "partial", "budget_exceeded"}
            or not summary["usage_complete"] or summary["exact_tenths"] > cap
        ):
            raise RuntimeError(f"Series stopped after failed, incomplete or over-budget execution: {name}")
        fingerprints.append(manifest["runtime"]["code_sha256"])
        if fingerprints[-1] != fingerprints[0] or not manifest["runtime_code_matches_recorded_files"]:
            raise RuntimeError(f"Runtime source fingerprint changed during the series: {name}")
        return directory

    if args.phase == "ablation":
        reference = experiment("baseline", "baseline", args.budget)
        for mode in ABLATIONS:
            if mode != "baseline":
                experiment(mode, mode, args.budget, reference, PREVIOUS[mode])
        for index in range(2, args.repeats + 1):
            for mode in ("baseline", "F", "G"):
                experiment(f"{mode}-r{index}", mode, args.budget, reference)
    elif args.phase == "repeat":
        for index in range(2, args.repeats + 2):
            for mode in args.modes:
                experiment(f"{mode}-r{index}", mode, args.budget)
    elif args.phase == "curve":
        for percent in PERCENTS:
            for mode in args.modes:
                experiment(f"{mode}-{percent}", mode, args.budget * percent // 100)
    else:
        for mode in ("F", "G"):
            experiment(mode, mode, args.budget)
    if len({row["input_sha256"] for row in rows}) != 1:
        raise RuntimeError("Prepared input fingerprint varied across runs")
    if args.phase == "curve":
        report_budget_series(args.output, rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("fixtures", "ablation", "repeat", "curve", "pair"))
    parser.add_argument("--archive", type=lambda value: Path(value).resolve())
    parser.add_argument("--solved", type=Path, help="Recorded successful workspaces for the replay fixture")
    parser.add_argument("--output", required=True, type=lambda value: Path(value).resolve())
    parser.add_argument("--image")
    parser.add_argument("--network")
    parser.add_argument("--profile", type=lambda value: Path(value).resolve())
    parser.add_argument("--budget", type=budget_tenths)
    parser.add_argument("--allocation", type=budget_tenths, help="Allocated total for this organizer series")
    parser.add_argument("--proxy-kind", choices=("test-double", "organizer"), default="test-double")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--modes", nargs="+", choices=tuple(ABLATIONS))
    args = parser.parse_args()
    args.modes = args.modes or (CORE_MODES if args.phase == "curve" else ("F", "G"))
    if args.phase == "fixtures":
        if bool(args.archive) != bool(args.solved):
            parser.error("Provide --archive and --solved together")
        records = archived_fixtures(args.archive, args.solved) if args.archive else mixed_fixtures(CASES)
        write_json(args.output, list(records))
        return
    if not all((args.profile, args.budget, args.timeout > 0, args.repeats > 0)):
        parser.error("Experiments require --profile and a positive --budget")
    if bool(args.image) != bool(args.network):
        parser.error("For Docker, provide --image and an isolated --network together")
    if args.proxy_kind == "organizer" and not args.network:
        parser.error("Organizer experiments require a proxy-only Docker network")
    if args.phase == "pair" and args.archive:
        parser.error("The pair phase uses two of the mixed tasks")
    if args.phase == "curve":
        maximum = len(args.modes) * sum(args.budget * percent // 100 for percent in PERCENTS)
    elif args.phase == "ablation":
        maximum = args.budget * (len(ABLATIONS) + 3 * (args.repeats - 1))
    elif args.phase == "repeat":
        maximum = args.budget * args.repeats * len(args.modes)
    else:
        maximum = args.budget * 2
    if args.proxy_kind == "organizer" and (args.allocation is None or args.allocation < maximum):
        parser.error(f"The allocated series budget must cover {maximum / 10:g} output-equivalent tokens")
    if not os.environ.get("LLM_BASE_URL") or not os.environ.get("LLM_API_KEY"):
        parser.error("Export LLM_BASE_URL and LLM_API_KEY for the selected proxy")
    os.environ["LLM_MODEL"] = MODEL
    compare(args)


if __name__ == "__main__":
    main()
