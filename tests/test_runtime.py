import json
import subprocess
import sys

from openjiuwen_challenge import runtime


def test_upstream_dotenv_reload_keeps_selected_openrouter_key(tmp_path, monkeypatch):
    """Exercise upstream's real override=True loader in a child, not just mock Popen."""
    config = tmp_path / ".runtime/.jiuwenswarm/config"
    config.mkdir(parents=True)
    (config / "config.yaml").write_text("{}")
    dotenv = config / ".env"
    dotenv.write_text(
        "OPENROUTER_API_KEY=\nAPI_KEY=old-direct-key\n"
        "API_BASE=https://old-direct.invalid\nMODEL_NAME=provider/test-model\n"
    )
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    native_run = subprocess.run

    def launch(_args, *, cwd, env):
        child = native_run(
            [
                sys.executable,
                "-c",
                """
import json, os, sys
from jiuwenswarm.dotenv_early import load_dotenv_runtime
load_dotenv_runtime(sys.argv[1], override=True)
assert os.environ["API_KEY"] == "test-openrouter-key"
assert os.environ["API_BASE"] == "https://openrouter.ai/api/v1"
print(json.dumps({"model": os.environ["MODEL_NAME"]}))
""",
                str(dotenv),
            ],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        assert json.loads(child.stdout)["model"] == "provider/test-model"
        return 0

    monkeypatch.setattr(runtime.subprocess, "call", launch)
    assert runtime.start() == 0


def test_missing_openrouter_key_stops_before_launch(tmp_path, monkeypatch):
    config = tmp_path / ".runtime/.jiuwenswarm/config"
    config.mkdir(parents=True)
    (config / "config.yaml").write_text("{}")
    (config / ".env").write_text("API_KEY=old-provider-key\n")
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    import pytest

    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        runtime.start()
