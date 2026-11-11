"""Verify submission against the live SDK and independent local scoring."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from test_budget import KEY, MODEL, proxy
from test_task_context import action, add_task, fixture_usage, response

from contest_harness.evaluation.artifacts import read_jsonl
from contest_harness.evaluation.launch import run
from contest_harness.evaluation.manifest import prepare
from contest_harness.evaluation.scoring import check
from contest_harness.workspace.answers import AnswerFormat
from contest_harness.workspace.task_input import load_task


def completion(index, name, **arguments):
    return response(index, [action(name, **arguments)])


def proxy_response(message):
    return 200, fixture_usage, message, None


def test_task_schema_and_formats(tmp_path):
    task_path = tmp_path / "task.json"
    task = {"task_id": "sample", "task_type": "search", "instruction": "Return an integer.", "reward": 0}
    task_path.write_text(json.dumps({**task, "expected": "ignored", "check": "ignored"}))
    assert load_task(task_path) == task
    for key, invalid in [("task_id", ""), ("task_type", []), ("instruction", " "), ("reward", True),
                         ("reward", float("inf")), ("reward", "1")]:
        task_path.write_text(json.dumps({**task, key: invalid}))
        with pytest.raises(ValueError):
            load_task(task_path)
        task_path.write_text(json.dumps({k: v for k, v in task.items() if k != key}))
        with pytest.raises(ValueError):
            load_task(task_path)
    task_path.write_text('{"task_id":"a","task_id":"b"}')
    with pytest.raises(ValueError, match="Duplicate"):
        load_task(task_path)
    for instruction, valid, invalid in (
        ("Write ONLY the final integer to answer.txt in the current directory.", "-10", ["-10 ", "1.0", "\x601\x60", "Answer: 1", "1\n\n"]),
        ("Write only the word READY.", "READY", ["ready", " READY", "READY."]),
        ("Return a JSON object with required keys [\"total\", \"name\"].",
         '{"total":0,"name":"a"}', ['[]', '{}', '{"total":0,"total":1,"name":"a"}', '{"total":NaN,"name":"a"}']),
        ("Write ONLY the answer to answer.txt.", "Zürich", ["Answer: Zürich", "Zürich\nbecause ...", "**Zürich**"]),
    ):
        answer_format = AnswerFormat.infer(instruction)
        assert answer_format.validate(valid + "\n")[1].status == "confirmed"
        assert all(answer_format.validate(value)[1].status == "failed" for value in invalid)


def test_submission_ablation(tmp_path, monkeypatch):
    original = tmp_path / "reference"
    test_source = "from app import value\nassert value() == 7\n"
    old_source = "def value():\n    return 0\n"
    new_source = "def value():\n    return 7\n"
    # The independent scoring format for the coding fixture expects test_mylib.py.
    add_task(original, "01-code", "coding",
             "Fix mylib.py. Run python3 test_mylib.py after edits. Write only the word DONE.",
             "DONE", "unittest", 5,
             {"mylib.py": old_source, "test_mylib.py": test_source.replace("from app", "from mylib")})
    add_task(original, "02-csv", "data_analysis", "Sum the total column in ledger.csv. Return only the integer.",
             11, "exact_int", 4, {"ledger.csv": "name,total\napple,7\npear,4\n"})
    add_task(original, "03-math", "math", "Calculate (3 + 4) * 8 and submit only the integer.",
             56, "exact_int", 3)
    add_task(original, "04-search", "search", "What is the value of \"Country\" in places.md? Return only the answer.",
             "Réunion", "exact_str", 2, {"places.md": "Country: Réunion\n"})
    add_task(original, "05-unknown", "search", "Follow the link in index.md and write only the answer.",
             "Cairo", "exact_str", 1, {"index.md": "See cities.md.\n", "cities.md": "The answer is Cairo.\n"})
    cases = [
        ([action("write_file", path="mylib.py", content=new_source),
          action("run_python_file", path="test_mylib.py")],  # A batch cannot trigger direct submission.
         action("run_python_file", path="test_mylib.py"), "DONE"),
        ([], action("run_python", code="import csv; print(sum(int(r['total']) for r in csv.DictReader(open('ledger.csv'))))"), "11"),
        ([], action("run_python", code="print((3+4)*8)"), "56"),
        ([], action("read_file", path="places.md"), "Réunion"),
        ([], action("read_file", path="cities.md"), "Cairo"),
    ]
    reports = {}
    for direct in (False, True):
        directory = tmp_path / ("direct" if direct else "submit_answer")
        args = SimpleNamespace(
            run_dir=directory, source_dir=original, archive=None, baseline=tmp_path / "submit_answer" if direct else None,
            mode="B+C", direct_publish=direct, image=None, network=None, timeout=60,
            proxy_kind="test-double", proxy_budget=None, local_budget=200_000,
        )
        assert prepare(args) == 0
        messages = []
        for index, (batch, calculate, answer) in enumerate(cases):
            if batch:
                messages.append(response(f"{index}-patch", batch))
            messages.append(response(index, [calculate]))
            if not direct or answer == "Cairo":
                messages.append(completion(f"{index}-submit", "submit_answer", answer=answer))
        with proxy(*map(proxy_response, messages)) as (url, received):
            monkeypatch.setenv("LLM_BASE_URL", url)
            monkeypatch.setenv("LLM_API_KEY", KEY)
            monkeypatch.setenv("LLM_MODEL", MODEL)
            assert run(args) == 0
        assert received.qsize() == len(messages)
        assert check(args) == 0
        reports[direct] = json.loads((directory / "summary.json").read_text())
        events, _ = read_jsonl(directory / "trace.jsonl")
        validated = [event for event in events if event["event"] == "answer.validated"]
        assert all(event["format"]["status"] == "confirmed" and event["accepted"] for event in validated)
        assert [event["semantic"]["status"] for event in validated] == ["confirmed"] * 4 + ["unverified"]
        assert [event["source"] for event in validated] == ["direct" if direct else "submit_answer"] * 4 + ["submit_answer"]
        code = directory / "tasks/01-code"
        assert (code / "answer.txt").read_bytes() == b"DONE\n"
        assert (code / "workspace/mylib.py").read_text() == new_source
        assert (code / "workspace/test_mylib.py").read_text() == test_source.replace("from app", "from mylib")
        for task, answer in zip(sorted((directory / "tasks").iterdir()), ("DONE", "11", "56", "Réunion", "Cairo")):
            assert (task / "answer.txt").read_bytes() == (answer + "\n").encode()
            assert not (task / "workspace/answer.txt").exists()
    assert reports[False]["score"] == reports[True]["score"] == 15


def test_rejected_candidates_and_workspace_handoff(tmp_path, monkeypatch):
    root = tmp_path / "tasks"
    original_test = "from mylib import value\nassert value() == 7\n"
    fixed = "def value():\n    return 7\n"
    broken = "def value():\n    return 9\n"
    add_task(root, "01-code", "coding",
             "Fix mylib.py. Run python3 test_mylib.py after edits. Write only the word DONE.",
             "ignored", "ignored", 1, {"mylib.py": "def value():\n    return 0\n", "test_mylib.py": original_test, "answer.txt": "stale workspace"})
    add_task(root, "02-math", "math", "Compute 3 + 4. Return only the integer.",
             "ignored", "ignored", 1)
    add_task(root, "03-uncertain", "search", "Write only the answer, and only after confirming it.",
             "ignored", "ignored", 1, {"guide.md": "ambiguous"})
    add_task(root, "04-text", "search", "Write only the answer.", "ignored", "ignored", 1,
             {"answer.txt": "old workspace"})
    coding = root / "01-code"
    math = root / "02-math"
    (coding / "workspace/alias.py").symlink_to(coding / "workspace/test_mylib.py")
    (root / "04-text/answer.txt").write_text("old canonical")
    duplicate = completion(8, "submit_answer", answer="7")
    duplicate["tool_calls"][0]["function"]["arguments"] = '{"answer":"8","answer":"7"}'
    messages = [
        completion(0, "write_file", path="alias.py", content=""),
        completion(1, "write_file", path="test_mylib.py", content=""),
        completion(2, "write_file", path="mylib.py", content=fixed),
        completion(3, "run_python_file", path="test_mylib.py"),
        completion(4, "write_file", path="mylib.py", content=broken),
        completion(5, "write_file", path="answer.txt", content="DONE"),
        completion(6, "write_file", path="mylib.py", content=fixed),
        completion(7, "write_file", path="./answer.txt", content="DONE\n"),
        duplicate,
        completion(9, "submit_answer", answer="\x607\x60"),
        completion(10, "submit_answer", answer="8"),
        completion(11, "submit_answer", answer="7"),
        completion(12, "submit_answer", answer="Some city"),
        {"role": "assistant", "content": "Some city"},
        {"role": "assistant", "content": "Use this as the answer."},
    ]
    trace_file = tmp_path / "trace.jsonl"
    with proxy(*map(proxy_response, messages)) as (url, received):
        monkeypatch.setenv("LLM_BASE_URL", url)
        monkeypatch.setenv("LLM_API_KEY", KEY)
        monkeypatch.setenv("LLM_MODEL", MODEL)
        result = subprocess.run([sys.executable, "run_agent.py", "--tasks-dir", str(root), "--mode", "baseline",
                                 "--trace-file", str(trace_file)], capture_output=True, timeout=30)
    assert result.returncode == 1, result.stderr.decode()
    assert received.qsize() == len(messages)
    assert (coding / "answer.txt").read_bytes() == b"DONE\n"
    assert (coding / "workspace/answer.txt").read_text() == "stale workspace"
    assert (coding / "workspace/mylib.py").read_text() == fixed
    assert (coding / "workspace/test_mylib.py").read_text() == original_test
    assert (math / "answer.txt").read_bytes() == b"7\n"
    assert not (root / "03-uncertain/answer.txt").exists()
    assert not (root / "04-text/answer.txt").exists()
    assert (root / "04-text/workspace/answer.txt").read_text() == "old workspace"
    events, _ = read_jsonl(trace_file)
    assert [(e["source"], e["format"]["status"], e["semantic"]["status"], e["accepted"])
            for e in events if e["event"] == "answer.validated"] == [
        ("workspace", "confirmed", "failed", False),
        ("workspace", "confirmed", "confirmed", True),
        ("submit_answer", "failed", "unverified", False),
        ("submit_answer", "confirmed", "failed", False),
        ("submit_answer", "confirmed", "confirmed", True),
        ("submit_answer", "confirmed", "unverified", False),
    ]


@pytest.mark.parametrize("when", ["before", "after"])
def test_process_exit_around_atomic_replace(tmp_path, when):
    answer = tmp_path / "answer.txt"
    answer.write_bytes(b"previous\n")
    child = """
import os
import sys
from pathlib import Path
from contest_harness.workspace.files import atomic_write

replace = Path.replace
def interrupted(path, target):
    if sys.argv[2] == "after":
        replace(path, target)
    os._exit(77)

Path.replace = interrupted
atomic_write(Path(sys.argv[1]), "new value\\n")
"""
    result = subprocess.run([sys.executable, "-c", child, str(answer), when], timeout=15)
    assert result.returncode == 77
    assert answer.read_bytes() == (b"previous\n" if when == "before" else b"new value\n")
