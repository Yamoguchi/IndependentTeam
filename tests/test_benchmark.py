"""Regression checks for separated inputs and reporting an interrupted run."""

import json
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from contest_harness.evaluation.artifacts import read_jsonl, write_jsonl
from contest_harness.evaluation.history import report
from contest_harness.evaluation.launch import run
from contest_harness.evaluation.manifest import load_manifest, prepare
from contest_harness.evaluation.metrics import prediction_errors, probability_metrics, tool_stats
from contest_harness.evaluation.report import generate
from contest_harness.evaluation.scoring import check


def test_metrics_distinguish_zero_cost_and_insufficient_probability_samples():
    assert prediction_errors([(2, 0), (4, 2)]) == {
        "samples": 2, "mae": 2, "mape_samples": 1, "mape_percent": 100,
    }
    assert probability_metrics([(0.8, True)])["brier_score"] is None
    result = probability_metrics([(0.8, True)] * 20)
    assert result["brier_score"] == 0.04
    assert result["log_loss"] == pytest.approx(0.223144)
    assert result["calibration"][0]["observed_rate"] == 1
    unavailable = tool_stats([{
        "event": "tool.unavailable", "tool": "write_file", "call_id": 1, "tool_call_id": "c",
        "reason": "schema_not_offered",
    }])["write_file"]
    assert unavailable["invalid_action_rate"] == 1
    assert unavailable["unavailable_reasons"] == {"schema_not_offered": 1}


def example(tmp_path):
    archive = tmp_path / "examples.zip"
    with ZipFile(archive, "w") as zipped:
        zipped.writestr("task-dir/service.txt", "ignored")
        zipped.writestr(
            "task-dir/task-a/task.json",
            json.dumps(
                {
                    "task_id": "different-from-directory",
                    "task_type": "math",
                    "instruction": "Compute 7 + 11.",
                    "reward": 2,
                    "expected": 18,
                    "check": "exact_int",
                }
            ),
        )
        zipped.writestr("task-dir/task-a/answer.txt", "old answer")
        zipped.writestr("task-dir/task-a/workspace/answer.txt", "old draft")
    return SimpleNamespace(archive=archive, source_dir=None, run_dir=tmp_path / "fresh", baseline=None)


def test_prepared_input_is_isolated_and_checked_for_changes(tmp_path):
    args = example(tmp_path)
    original = args.archive.read_bytes()
    prepare(args)
    task = args.run_dir / "tasks" / "task-a"
    assert set(json.loads((task / "task.json").read_text())) == {
        "task_id",
        "task_type",
        "instruction",
        "reward",
    }
    assert not list((args.run_dir / "tasks").rglob("answer.txt"))
    with pytest.raises(FileExistsError):
        prepare(args)
    (task / "answer.txt").write_text("unexpected\n")
    with pytest.raises(ValueError, match="Prepared inputs changed"):
        run(args)
    assert args.archive.read_bytes() == original


def test_report_recovers_prefix_and_counts_call_usage_once(tmp_path):
    args = example(tmp_path)
    prepare(args)
    root = args.run_dir
    manifest = load_manifest(root)
    manifest["launch"]["status"] = "running"
    journal = root / "journal"
    journal.mkdir()
    fields = {"run_id": "run", "task_index": 1, "task_id": "different-from-directory"}
    events = [
        {**fields, "event": "task.started", "directory": "task-a"},
        {**fields, "event": "llm.started", "call_id": 1, "model": "test"},
        {
            **fields,
            "event": "llm.finished",
            "call_id": 1,
            "model": "test",
            "duration_ms": 20,
            "usage": {
                "calls": 1,
                "measured_calls": 1,
                "input_tokens": 100,
                "cached_input_tokens": 70,
                "output_tokens": 10,
                "reasoning_tokens": 4,
                "usage_complete": True,
            },
        },
        {**fields, "event": "llm.started", "call_id": 2, "model": "test"},
    ]
    trace = journal / "trace.jsonl"
    write_jsonl(
        trace, [{**row, "elapsed_ms": index, "sequence": index} for index, row in enumerate(events, 1)]
    )
    with trace.open("a") as file:
        file.write('{"partial')
    original = trace.read_bytes()
    (root / "tasks" / "task-a" / "answer.txt").write_text("18\n")
    generate(root, root, manifest)
    assert check(args) == 0
    summary = json.loads((root / "summary.json").read_text())
    assert summary["status"] == "interrupted"
    assert summary["submitted"] == summary["correct"] == 1
    assert summary["calls"] == 2
    assert summary["measured_calls"] == summary["unknown_usage_calls"] == 1
    assert summary["output_equivalent"] is None
    assert summary["measured_output_equivalent"] == 20
    assert summary["reasoning_tokens"] == 4
    assert summary["cached_input_tokens"] == 70
    assert trace.read_bytes() == original
    assert load_manifest(root)["journal"]["trace_tail_discarded"] is True

    recovered = tmp_path / "recovered"
    report(SimpleNamespace(source_run=root, run_dir=recovered, baseline=root))
    assert json.loads((recovered / "summary.json").read_text()) == summary
    assert load_manifest(recovered)["journal"]["trace_tail_discarded"] is True
    assert read_jsonl(recovered / "tokens.jsonl")[0][1]["status"] == "unfinished"
    assert (recovered / "score.jsonl").is_file()
    with pytest.raises(FileExistsError):
        report(SimpleNamespace(source_run=root, run_dir=recovered, baseline=None))
