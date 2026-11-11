"""Router and cascade on the real ReAct SDK with a scripted proxy and separate scoring."""

import json
from types import SimpleNamespace

import pytest
from test_budget import KEY, MODEL, proxy
from test_task_context import action, add_task, fixture_usage, response

from contest_harness.evaluation.artifacts import read_jsonl
from contest_harness.evaluation.launch import run
from contest_harness.evaluation.manifest import prepare
from contest_harness.evaluation.scoring import check
from contest_harness.planning.routing import ModelEvidence, Route, TaskRouter
from contest_harness.workspace.reference import compute_reference

SMART = "qwen3.6-35b-a3b-noreason"


def test_local_reference_is_conservative(tmp_path):
    csv = tmp_path / "ledger.csv"
    csv.write_text("area,quarter,amount\neast,2,7\neast,4,3\nwest,3,12\n")
    question = 'Sum the amount in ledger.csv where area = "east" and quarter is between 2 and 4 inclusive.'
    assert compute_reference(question, tmp_path, "integer") == ("10", "csv_filtered_sum")
    assert compute_reference(question + " Subtract 5 from the result.", tmp_path, "integer") is None
    csv.write_text("area,quarter,amount\neast,2,7,unexpected\n")
    assert compute_reference(question, tmp_path, "integer") is None
    field = tmp_path / "facts.md"
    question = 'Find the value of "Country" in facts.md.'
    field.write_text("Country: France\nCountry: Algeria\n")
    assert compute_reference(question, tmp_path, "line") is None
    field.unlink()
    field.symlink_to(csv)
    assert compute_reference(question, tmp_path, "line") is None
    assert compute_reference("Compute 2 ** 99999.", tmp_path, "integer") is None


def test_cascade_requires_positive_incremental_gain_and_known_reserve():
    evidence = ModelEvidence(SMART, 6, 5, 300, 3, 2)
    router = TaskRouter(MODEL, {}, True, penalty=0.01)
    route = Route("CASCADE", MODEL, "test", {}, smart=evidence)
    for outcome, reward, remaining, reason in (
        ("candidate_rejected", 10, 5000, "positive_gain"),
        ("candidate_rejected", 10, 4999, "insufficient_reserve"),
        ("candidate_rejected", 10, None, "unknown_reserve"),
        ("candidate_rejected", 1, 5000, "no_positive_gain"),
        ("no_answer", 10, 5000, "no_eligible_terminal_outcome"),
    ):
        decision = router.escalation(route, outcome, reward, remaining, 5000, 1000)
        assert decision["reason"] == reason
        assert decision["escalate"] is (reason == "positive_gain")
    assert decision["outcome"] == "no_answer"
    rejection = router.escalation(route, "candidate_rejected", 10, 5000, 5000, 1000)
    assert rejection["p_smart"] == pytest.approx(0.56)
    assert rejection["c_smart"] == 400 and rejection["c_verify"] == 0
    assert rejection["expected_gain"] == pytest.approx(1.6)


def test_router_ablation_and_coding_handoff(tmp_path, monkeypatch):
    original = tmp_path / "reference"
    test_source = "from mylib import value\nassert value() == 7\n"
    previous = "def value():\n    return 3\n"
    updated = "def value():\n    return 7\n"
    coding_instruction = "Fix mylib.py. Run python3 test_mylib.py after editing. Write only the word DONE."
    add_task(original, "01-math", "math", "Compute 6 * 7. Return only the integer.", 42, "exact_int", 2)
    add_task(original, "02-csv", "data_analysis", 'Sum units in ledger.csv where color = "red". Return only the integer.',
             8, "exact_int", 3, {"ledger.csv": "color,units\nred,8\nblue,4\n"})
    add_task(original, "03-field", "search", 'Find the value of "Port" in notes.md. Return only the answer.',
             "A Coruña", "exact_str", 1, {"notes.md": "Port: A Coruña\n"})
    add_task(original, "04-complex", "math",
             "Calculate the aggregate product where the factors are 3 and 5. " + "Verify the multiplication. " * 25
             + "Return only the integer.", 15, "exact_int", 2)
    for name in ("05-repaired", "06-still-failing"):
        add_task(original, name, "coding", coding_instruction, "DONE", "unittest", 4,
                 {"mylib.py": "def value():\n    return 0\n", "test_mylib.py": test_source})
    add_task(original, "07-tool-error", "search", "Follow the pointer in notes.md. Return only the answer.",
             "Kyoto", "exact_str", 3, {"notes.md": "City: Kyoto\n"})

    evidence = [
        {"model": MODEL, "tasks": 3, "solved": 1, "mean_cost": 200,
         "after_failure_tasks": 2, "after_failure_solved": 0},
        {"model": SMART, "tasks": 3, "solved": 3, "mean_cost": 250,
         "after_failure_tasks": 2, "after_failure_solved": 2},
    ]
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"schema_version": 1, "cheap_model": MODEL,
                                   "by_type": {kind: evidence for kind in ("math", "search", "coding")}}))

    def smart_request(request):
        assert request["model"] == SMART
        messages = "\n".join(message["content"] or "" for message in request["messages"])
        assert coding_instruction in messages
        assert '"rejected_candidate":"DONE"' in messages
        assert "Original test failed" in messages and "AssertionError" in messages
        assert json.dumps(previous)[1:-1] in messages
        assert "test_mylib.py" in messages
        return response("smart-write", [action("write_file", path="mylib.py", content=updated)])

    def completion(index, name, **arguments):
        return response(index, [action(name, **arguments)])

    no_answer = {"role": "assistant", "content": ""}
    baseline = None
    reports = {}
    for routing, cascade in [(False, False), (True, False), (True, True)]:
        mode = "E1+E2+E3" if cascade else "E1+E2" if routing else "E1"
        directory = tmp_path / mode
        args = SimpleNamespace(
            run_dir=directory, source_dir=original, archive=None, baseline=baseline,
            mode="B+C", routing=routing, cascade=cascade, routing_profile=profile if routing else None,
            image=None, network=None, timeout=60, proxy_kind="test-double", proxy_budget=None, local_budget=200_000,
        )
        assert prepare(args) == 0
        messages = [] if routing else ["42", "8", "A Coruña"]
        messages.append("15")
        for index in range(2):
            messages.extend([completion(index, "write_file", path="mylib.py", content=previous), "DONE", no_answer])
            if cascade:
                messages.extend([smart_request, "DONE"] if index == 0 else [no_answer])
        messages.extend([completion("missing", "read_file", path="missing.md"), no_answer])
        with proxy(*[(200, fixture_usage, message, None) for message in messages]) as (url, received):
            monkeypatch.setenv("LLM_BASE_URL", url)
            monkeypatch.setenv("LLM_API_KEY", KEY)
            monkeypatch.setenv("LLM_MODEL", MODEL)
            assert run(args) == 1
        requests = [received.get_nowait() for _ in range(received.qsize())]
        assert len(requests) == len(messages)
        assert requests[0 if routing else 3]["model"] == (SMART if routing else MODEL)
        search_tools = {item["function"]["name"] for item in requests[-2]["tools"]}
        assert ("run_python" in search_tools) is not routing
        assert "write_file" not in search_tools
        assert check(args) == 1
        for name in ("05-repaired", "06-still-failing"):
            workspace = directory / "tasks" / name / "workspace"
            assert (workspace / "test_mylib.py").read_text() == test_source
            assert (workspace / "mylib.py").read_text() == (updated if cascade and name == "05-repaired" else previous)
        metrics = json.loads((directory / "metrics.json").read_text())
        reports[mode] = metrics
        assert metrics["score"] == (12 if cascade else 8)
        assert metrics["routes"]["escalations"] == (2 if cascade else 0)
        assert metrics["routes"]["local_publications"] == (3 if routing else 0)
        if baseline is None:
            baseline = directory

    directory = tmp_path / "E1+E2+E3"
    rows, _ = read_jsonl(directory / "results-by-task.jsonl")
    repaired, failed, ordinary = rows[-3:]
    assert repaired["route"] == failed["route"] == ordinary["route"] == "CASCADE"
    assert repaired["passed"] and not failed["passed"] and not ordinary["passed"]
    assert repaired["escalation"]["reason"] == "candidate_rejected"
    assert repaired["escalation"]["workspace"]["revision"] == 1
    assert repaired["attempts"][-1]["workspace"]["revision"] == 2
    assert failed["attempts"][-1]["workspace"] == failed["escalation"]["workspace"]
    assert ordinary["escalation"] is None
    assert ordinary["escalation_decision"]["reason"] == "no_eligible_terminal_outcome"
    assert ordinary["escalation_decision"]["outcome"] == "tool_errors_without_progress"
    assert not (directory / "tasks/07-tool-error/answer.txt").exists()
    events, _ = read_jsonl(directory / "trace.jsonl")
    failed_tools = [row for row in events if row["event"] == "tool.finished" and row["task_id"] == "07-tool-error"]
    assert [(row["tool"], row["status"]) for row in failed_tools] == [("read_file", "error")]

    routes = reports["E1+E2+E3"]["routes"]
    assert routes["escalations_without_gain"] == 1 and routes["escalation_points"] == 4
    assert routes["before_escalation"]["calls"] == 6 and routes["after_escalation"]["calls"] == 3
    assert sum(row["usage"]["exact_tenths"] for row in (repaired, failed)) == sum(
        routes[stage]["exact_tenths"] for stage in ("before_escalation", "after_escalation")
    )
    assert reports["E1+E2"]["calls"] == reports["E1"]["calls"] - 3
    assert reports["E1+E2+E3"]["calls"] == reports["E1+E2"]["calls"] + 3
