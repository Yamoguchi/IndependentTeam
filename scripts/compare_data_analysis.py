"""Compare scored data-analysis runs using their sanitized, per-call traces."""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def read_rows(root, filename):
    with (root / filename).open(encoding="utf-8") as source:
        return [json.loads(line) for line in source]


def load_run(root):
    manifest = json.loads((root / "run-manifest.json").read_text(encoding="utf-8"))
    tasks = {
        row["task_id"]: row for row in read_rows(root, "results-by-task.jsonl")
        if row["task_type"] == "data_analysis"
    }
    events = defaultdict(list)
    for row in read_rows(root, "trace.jsonl"):
        if row.get("task_id") in tasks:
            events[row["task_id"]].append(row)
    return manifest["input"]["content_sha256"], tasks, events


def totals(tasks, events):
    calls = [row for row in events if row["event"] == "llm.finished" and row["usage"]["calls"]]
    first = {}
    for row in calls:
        first.setdefault(row["task_id"], row["usage"]["input_tokens"])
    requests = [row for row in events if row["event"] == "llm.request"]
    tools = [row for row in events if row["event"] == "tool.finished"]
    arguments = Counter(
        (row["task_id"], row["tool"], (row.get("arguments") or {}).get("sha256"))
        for row in events if row["event"] == "tool.started"
    )
    return {
        "input": sum(row["usage"]["input_tokens"] for row in calls),
        "output": sum(row["usage"]["output_tokens"] for row in calls),
        "reasoning": sum(row["usage"].get("reasoning_tokens", 0) for row in calls),
        "first input": sum(first.values()),
        "calls": len(calls),
        "generation length": sum(row.get("finish_reason") == "length" for row in calls),
        "tool schema bytes": sum(row["tools"]["serialized"]["utf8_bytes"] for row in requests),
        "system message bytes": sum(
            row["context"]["by_role"].get("system", {}).get("serialized_bytes", 0) for row in requests
        ),
        "tool message bytes": sum(
            row["context"]["by_role"].get("tool", {}).get("serialized_bytes", 0) for row in requests
        ),
        "tool calls": len(tools),
        "Python calls": sum(row["tool"] == "run_python" for row in tools),
        "tool errors": sum(row["status"] != "ok" for row in tools),
        "repeated calls": sum(n - 1 for n in arguments.values()),
        "tool result bytes": sum((row.get("result") or {}).get("utf8_bytes", 0) for row in tools),
        "task seconds": round(sum(row["seconds"] for row in tasks.values()), 3),
    }


def task_metrics(task, events):
    tools = [row for row in events if row["event"] == "tool.finished"]
    arguments = Counter(
        (row["tool"], (row.get("arguments") or {}).get("sha256"))
        for row in events if row["event"] == "tool.started"
    )
    return (
        task["seconds"], len(tools), sum(row["status"] != "ok" for row in tools),
        sum(count - 1 for count in arguments.values()),
        sum((row.get("result") or {}).get("utf8_bytes", 0) for row in tools),
    )


def timeline(events):
    rows = []
    for row in events:
        if row["event"] == "llm.finished":
            usage = row["usage"]
            cost = usage["output_equivalent"]
            rows.append((
                row["call_id"], "LLM", usage["input_tokens"], usage["output_tokens"],
                "—" if cost is None else f"{cost:.1f}",
                f"{row['status']}/{row.get('finish_reason') or '—'}",
            ))
        elif row["event"] == "tool.finished":
            size = (row.get("result") or {}).get("utf8_bytes", 0)
            rows.append((row.get("call_id", "—"), row["tool"], "—", "—", "—", f"{row['status']}, {size} B"))
    return rows


def report(baseline, modified):
    digest, old, old_events = load_run(baseline)
    new_digest, new, new_events = load_run(modified)
    if not digest or digest != new_digest or old.keys() != new.keys() or not old:
        raise ValueError("The two runs must have matching inputs and data-analysis tasks.")
    if any(row["actual_cost"] is None for run in (old, new) for row in run.values()):
        raise ValueError("Both runs must have complete per-task usage.")
    before = totals(old, [event for events in old_events.values() for event in events])
    after = totals(new, [event for events in new_events.values() for event in events])
    ids = sorted(old, key=lambda key: (old[key]["actual_cost"] - new[key]["actual_cost"], key))
    output = [
        "## Сводка по трассам", "",
        "| Показатель | Baseline | Modified |", "| --- | ---: | ---: |",
        *(f"| {key} | {before[key]} | {after[key]} |" for key in before),
        "", "## Расход по задачам", "",
        "| task_id | baseline_cost | modified_cost | difference | baseline_calls | modified_calls "
        "| baseline_input | modified_input | baseline_output | modified_output |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for key in ids:
        left, right = old[key], new[key]
        a, b = left["actual_cost"], right["actual_cost"]
        counters = [row["usage"][field] for field in ("calls", "input_tokens", "output_tokens") for row in (left, right)]
        output.append(f"| {key} | {a:.1f} | {b:.1f} | {b-a:+.1f} | " + " | ".join(map(str, counters)) + " |")
    output.extend([
        "", "## Инструменты по задачам", "",
        "| task_id | Время, с | Вызовы инструментов | Ошибки | Повторы аргументов | Вывод, байт |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ])
    for key in ids:
        left, right = task_metrics(old[key], old_events[key]), task_metrics(new[key], new_events[key])
        output.append(f"| {key} | " + " | ".join(f"{a} → {b}" for a, b in zip(left, right, strict=True)) + " |")
    output.extend(["", "## Последовательность действий", ""])
    for key in ids:
        output.extend([f"<details><summary>{key}</summary>", ""])
        for label, events in (("Baseline", old_events[key]), ("Modified", new_events[key])):
            output.extend([
                f"**{label}**", "",
                "| № | Запрос | Действие | Input | Output | Cost | Статус |",
                "| ---: | ---: | --- | ---: | ---: | ---: | --- |",
            ])
            for index, row in enumerate(timeline(events), 1):
                output.append(f"| {index} | " + " | ".join(map(str, row)) + " |")
            output.append("")
        output.extend(["</details>", ""])
    return "\n".join(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("modified", type=Path)
    args = parser.parse_args()
    print(report(args.baseline, args.modified))
