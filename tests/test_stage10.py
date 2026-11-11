import json
import subprocess
import sys
from io import StringIO

import pytest
from test_budget import INCLUDED, KEY, MODEL, proxy, quiet_sdk  # noqa: F401

from contest_harness.agent.generation import profile_limits
from contest_harness.agent.model import TracedModel
from contest_harness.planning.feasibility import Resolution, check_prerequisites, resolve, retry_decision
from contest_harness.scheduler import run
from contest_harness.telemetry.trace import RunTrace


@pytest.mark.parametrize(("instruction", "expected"), [
    ("Required existing input file: `calibration/raw.bin`.\nReport the checksum.", "required_input_absent"),
    ("Read the provided file `vault`.\nReport the owner.", "required_resource_wrong_type"),
    ("Use the required file `voucher.txt`.\nIf it is missing, compute the value from the embedded example.", None),
    ("Read the supplied file `notes.txt`.\nReturn its last word.", None),
    ("Run the analyzer over the supplied data.", None),
    ("Required input file: `alias.txt`.\nReturn its last word.", None),
    ("Required input file: `../secret.txt`.\nReturn its last word.", None),
    ("Consider this snippet:\n```text\nRequired input file: `fiction.bin`.\n```", None),
])
def test_calibration_gate(tmp_path, instruction, expected):
    (tmp_path / "vault").mkdir()
    (tmp_path / "notes.txt").write_text("available")
    (tmp_path / "alias.txt").symlink_to("nonexistent")
    result = check_prerequisites(instruction, tmp_path)
    if expected:
        assert result.status == "UNSOLVABLE_CONFIRMED"
        assert result.reason == expected
        assert result.evidence["observation"]
    else:
        assert result is None


@pytest.mark.parametrize(("status", "kwargs", "expected", "reason"), [
    ("submitted", {}, "DONE", "answer_published"),
    ("no_answer", {"signal": "UNSOLVABLE_SUSPECTED"}, "UNSOLVABLE_SUSPECTED", "model_suspected_missing_prerequisite"),
    ("no_answer", {"signal": "UNSOLVABLE_CONFIRMED"}, "UNCERTAIN", "model_no_submission"),
    ("no_answer", {"signal": "NEED_MORE_INFO"}, "NEED_MORE_INFO", "model_reported_missing_information"),
    ("step_limit", {}, "UNCERTAIN", "model_step_limit"),
    ("no_answer", {"rejection": True}, "UNCERTAIN", "candidate_rejected"),
    ("budget_exceeded", {}, "BUDGET_EXCEEDED", "llm_reservation_denied"),
    ("generation_failed", {}, "FAILED", "model_generation_failed"),
    ("proxy_error", {"errors": {"http_status": 503}}, "FAILED", "proxy_transient"),
    ("proxy_error", {"errors": {"http_status": 403}}, "FAILED", "proxy_request_failed"),
])
def test_resolution_taxonomy(status, kwargs, expected, reason):
    outcome = resolve(status, **kwargs)
    assert (outcome.status, outcome.reason) == (expected, reason)


def test_retry_gate():
    outcome = Resolution("UNCERTAIN", "model_step_limit")
    assert retry_decision(outcome, 10, 10000, 10000)["eligible"]
    assert retry_decision(outcome, 10, 10000, 9999)["reason"] == "insufficient_reserve"
    assert retry_decision(outcome, 10, 10000, None)["reason"] == "unknown_reserve"
    assert retry_decision(outcome, 1, 10000, 10000)["reason"] == "no_positive_gain"


def inputs(root, *instructions):
    root.mkdir()
    for index, (instruction, reward) in enumerate(instructions, 1):
        directory = root / f"case-{index}"
        directory.mkdir()
        (directory / "task.json").write_text(json.dumps({
            "task_id": directory.name, "task_type": "search", "instruction": instruction, "reward": reward,
        }))


def events(stream, name):
    return [row for line in stream.getvalue().splitlines() if (row := json.loads(line))["event"] == name]


@pytest.mark.parametrize("transient", [False, True])
async def test_retry_waits_for_other_tasks_and_shares_usage(tmp_path, transient):
    root = tmp_path / "tasks"
    first_query = "Return the name of the primary station."
    inputs(root, (first_query, 9), ("Return the backup station.", 4))
    stream = StringIO()
    trace = RunTrace(stream, limit_tenths=80000)

    def retried(request):
        assert any(first_query in message["content"] for message in request["messages"] if message["role"] == "user")
        return "Hazel"

    first_response = (
        (503, None, None, None) if transient else
        (200, INCLUDED, {"role": "assistant", "content": "Attempt stopped."}, None)
    )
    with proxy(first_response, (200, INCLUDED, "Spruce", None), (200, INCLUDED, retried, None)) as (url, received):
        model = TracedModel(trace, MODEL, url, KEY)
        await run(model, MODEL, root, trace, "B+C", early_stop=True)
    assert received.qsize() == 3
    assert (root / "case-1" / "answer.txt").read_bytes() == b"Hazel\n"
    assert (root / "case-2" / "answer.txt").read_bytes() == b"Spruce\n"
    outcomes = events(stream, "recognition.outcome")
    assert [row["task_id"] for row in outcomes] == ["case-1", "case-2", "case-1"]
    assert outcomes[0]["resolution"]["reason"] == ("proxy_transient" if transient else "model_no_submission")
    assert not outcomes[0]["final"]
    assert outcomes[-1]["resolution"]["status"] == "DONE"
    assert outcomes[0]["task_index"] == outcomes[-1]["task_index"]
    assert outcomes[-1]["usage"]["calls"] == 2
    assert outcomes[-1]["usage"]["held_tenths"] > 0 if transient else outcomes[-1]["usage"]["exact_tenths"] == 400
    assert events(stream, "run.finished")[-1]["retried_tasks"] == 1


async def test_confirmed_and_budget_skips_precede_dispatch(tmp_path):
    root = tmp_path / "tasks"
    inputs(root, ("Required input file: `manifest.tsv`.\nSum the weights.", 1), ("Solve the puzzle.", 2))
    stream = StringIO()
    trace = RunTrace(stream, limit_tenths=300)
    with proxy() as (url, received):
        model = TracedModel(trace, MODEL, url, KEY)
        await run(model, MODEL, root, trace, "B+C", early_stop=True)
    assert received.empty()
    outcomes = events(stream, "recognition.outcome")
    assert [row["resolution"]["status"] for row in outcomes] == ["UNSOLVABLE_CONFIRMED", "BUDGET_EXCEEDED"]
    assert outcomes[1]["resolution"]["reason"] == "estimated_cost_exceeds_remaining"
    assert all(row["usage"]["exact_tenths"] == 0 for row in outcomes)


@pytest.mark.parametrize("mode", ["B+C", "B+C+D"])
async def test_model_signals_are_distinct_and_unverified(tmp_path, mode):
    root = tmp_path / "tasks"
    inputs(root, ("Find the patrol label.", 8), ("Find the shift label.", 4))
    stream = StringIO()
    trace = RunTrace(stream, limit_tenths=80000)
    messages = [{"role": "assistant", "content": signal} for signal in ("UNSOLVABLE_SUSPECTED", "NEED_MORE_INFO")]
    with proxy(*[(200, INCLUDED, message, None) for message in messages]) as (url, received):
        model = TracedModel(trace, MODEL, url, KEY)
        limits = profile_limits([], 2048) if mode == "B+C+D" else None
        await run(model, MODEL, root, trace, mode, limits, early_stop=True)
    assert received.qsize() == 2
    assert all(received.get()["tool_choice"] == "auto" for _ in messages)
    assert [row["resolution"]["status"] for row in events(stream, "recognition.outcome")] == [
        "UNSOLVABLE_SUSPECTED", "NEED_MORE_INFO",
    ]
    assert not list(root.glob("*/answer.txt"))


def test_competition_defaults_rank_and_publish_with_a_tight_budget(tmp_path, monkeypatch):
    root = tmp_path / "tasks"
    inputs(root, ("Find the backup station.", 2), ("Find the primary station.", 10))
    trace = tmp_path / "trace.jsonl"
    output = root / "results.jsonl"
    output.write_text('{"run_id":"previous"}\n', encoding="utf-8")
    with proxy((200, INCLUDED, "Primary", None), (200, INCLUDED, "Backup", None)) as (url, received):
        monkeypatch.setenv("LLM_BASE_URL", url)
        monkeypatch.setenv("LLM_API_KEY", KEY)
        monkeypatch.setenv("LLM_MODEL", MODEL)
        monkeypatch.setenv("HARNESS_TOKEN_BUDGET", "550")
        monkeypatch.setenv("OUT", str(output))
        result = subprocess.run(
            [sys.executable, "run_agent.py", "--tasks-dir", str(root), "--trace-file", str(trace)],
            capture_output=True, timeout=20,
        )
    assert result.returncode == 0, result.stderr.decode()
    requests = [received.get(), received.get()]
    assert "primary station" in str(requests[0]["messages"])
    assert all(128 <= request["max_tokens"] < 768 for request in requests)
    previous, *rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert previous == {"run_id": "previous"}
    assert rows[0]["run_id"] == rows[1]["run_id"] != previous["run_id"]
    assert [row["task_id"] for row in rows] == ["case-2", "case-1"]
    assert all(row["status"] == "submitted" for row in rows)
    assert (root / "case-2/answer.txt").read_text() == "Primary\n"
    assert (root / "case-1/answer.txt").read_text() == "Backup\n"


async def test_deferred_attempt_rechecks_reserve_after_other_tasks(tmp_path):
    root = tmp_path / "tasks"
    inputs(root, ("Return the position of the first beacon.", 9), ("Return the backup beacon.", 4))
    stream = StringIO()
    trace = RunTrace(stream, limit_tenths=37000)
    no_answer = {"role": "assistant", "content": "Attempt stopped."}
    expensive = {"prompt_tokens": 100, "completion_tokens": 1500, "total_tokens": 1600}
    with proxy((200, INCLUDED, no_answer, None), (200, expensive, "Hazel", None)) as (url, received):
        model = TracedModel(trace, MODEL, url, KEY)
        await run(model, MODEL, root, trace, "B+C", early_stop=True)
    assert received.qsize() == 2
    assert events(stream, "recognition.retry_decision")[0]["eligible"]
    admission = events(stream, "recognition.retry_admission")
    assert len(admission) == 1 and admission[0]["reason"] == "insufficient_task_budget"
    final = events(stream, "recognition.outcome")[-1]
    assert final["task_id"] == "case-1" and final["final"]
    assert final["resolution"]["reason"] == "model_no_submission"
    assert final["usage"]["exact_tenths"] == 200


async def test_coding_retry_keeps_original_edit_and_test_evidence(tmp_path):
    from test_task_context import action, response

    root = tmp_path / "tasks"
    inputs(root, ("Fix rates.py. Run python3 test_rates.py after editing. Write only the word DONE.", 12))
    directory = root / "case-1"
    task = directory / "task.json"
    task.write_text(task.read_text().replace('"search"', '"coding"'))
    workspace = directory / "workspace"
    workspace.mkdir()
    (workspace / "rates.py").write_text("def rate():\n    return 1\n")
    test_source = b"from rates import rate\nassert rate() == 4\n"
    (workspace / "test_rates.py").write_bytes(test_source)
    corrected = "def rate():\n    return 4\n"

    def submit_after_retry(request):
        query = next(message["content"] for message in request["messages"] if message["role"] == "user")
        assert "Continue from the previous attempt" in query and "rates.py" in query
        return "DONE"

    trace = RunTrace(StringIO(), limit_tenths=80000)
    messages = [
        response("edit", [action("write_file", path="rates.py", content=corrected)]),
        response("test", [action("run_python_file", path="test_rates.py")]),
        {"role": "assistant", "content": "Stopped before submission."},
        submit_after_retry,
    ]
    with proxy(*[(200, INCLUDED, item, None) for item in messages]) as (url, received):
        model = TracedModel(trace, MODEL, url, KEY)
        assert await run(model, MODEL, root, trace, "B+C", early_stop=True) == 0
    assert received.qsize() == 4
    assert (directory / "answer.txt").read_bytes() == b"DONE\n"
    assert (workspace / "rates.py").read_text() == corrected
    assert (workspace / "test_rates.py").read_bytes() == test_source
