"""Real inherited harness evidence only; never synthesize JIUWENSWARM markers."""

import hashlib
import json
import os
from pathlib import Path

import psutil


def collect_report():
    # Environment values may contain credentials. Only directory markers are needed.
    allowed = {"JIUWENSWARM_DATA_DIR", "JIUWENSWARM_HOME", "JIUWENSWARM_LOG_DIR"}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    tree = []
    harness_ancestor = False
    for proc in psutil.Process().parents():
        try:
            args = proc.cmdline()
            # Examine executable/module positions, never match a shell's command string.
            entry = args[0] if args else ""
            if len(args) > 1 and Path(entry).name.lower().startswith("python"):
                entry = args[2] if args[1] == "-m" and len(args) > 2 else args[1]
            name = Path(entry).name
            harness = (
                name.startswith("jiuwenswarm")
                or name == "openjiuwen"
                or name.startswith("openjiuwen.")
                or (("/jiuwenswarm/" in entry or "/openjiuwen/" in entry) and name.endswith(".py"))
            )
            harness_ancestor |= harness
            # Do not leak argv, API keys, user prompts or personal paths from ps output.
            tree.append(f"pid={proc.pid} ppid={proc.ppid()} executable={name}")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if not env and not harness_ancestor:
        raise ValueError(
            "Нет признаков harness: выполните сбор через shell самого агента openJiuwen/JiuwenSwarm. "
            "Не создавайте переменные окружения ради обхода проверки."
        )
    report = json.dumps(
        {"env": env, "process_tree": "\n".join(tree)}, ensure_ascii=False, separators=(",", ":")
    )
    return report, hashlib.sha256(report.encode("utf-8")).hexdigest()
