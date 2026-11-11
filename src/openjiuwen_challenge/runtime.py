"""Launch the official JiuwenSwarm runtime with project-local data."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"


def runtime_env():
    env = os.environ.copy()
    env.update(
        JIUWENSWARM_DATA_DIR=str(ROOT / ".runtime" / ".jiuwenswarm"),
        JIUWENSWARM_HOME=str(ROOT / ".runtime"),
        BROWSER_RUNTIME_MCP_ENABLED="0",
        JIUWENSWARM_SSL_VERIFY="true",
        FREE_SEARCH_SSL_VERIFY="true",
    )
    return env


def setup():
    """Use upstream's initializer, never rebuild a fake harness workspace."""
    import yaml

    os.environ.update(runtime_env())
    from jiuwenswarm.common.utils import prepare_workspace

    data_dir = Path(os.environ["JIUWENSWARM_DATA_DIR"])
    config = data_dir / "config" / "config.yaml"
    if config.exists():
        raise ValueError("Runtime уже настроен; отредактируйте .runtime/.jiuwenswarm/config/config.yaml")
    prepare_workspace(overwrite=False, preferred_language="en", workspace_dir=data_dir)
    if not config.exists():
        raise ValueError("Изменилась структура JiuwenSwarm: config/config.yaml не создан")
    cfg = yaml.safe_load(config.read_text())
    cfg["preferred_language"] = "en"
    cfg["mcp"] = {
        "servers": [
            {
                "name": "agent-registry",
                "enabled": True,
                "transport": "streamable-http",
                "url": "https://openjiuwen.iir.nsu.ru/mcp",
                "timeout_s": 30,
            }
        ]
    }
    cfg["memory"]["engine"] = "none"
    cfg["task_memory"]["enabled"] = False
    cfg["auto_memory_enabled"] = False
    cfg["sandbox"]["enabled"] = False
    config.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
    env_file = data_dir / "config" / ".env"
    # No upstream dummy keys, browser downloads, or unrelated integration credentials.
    env_file.write_text(
        f"API_BASE={OPENROUTER_BASE}\nOPENROUTER_API_KEY=\n"
        "API_KEY=${OPENROUTER_API_KEY}\n"
        f"MODEL_NAME={DEFAULT_MODEL}\nMODEL_PROVIDER=OpenAI\n"
        "BROWSER_RUNTIME_MCP_ENABLED=0\nJIUWENSWARM_SSL_VERIFY=true\n"
        "FREE_SEARCH_SSL_VERIFY=true\n"
    )
    env_file.chmod(0o600)
    skill = data_dir / "agent" / "workspace" / "skills" / "challenge-registration"
    skill.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "docs" / "AGENT_SKILL.md", skill / "SKILL.md")
    print(f"Runtime создан: {data_dir}\nВпишите OPENROUTER_API_KEY в {env_file}")


def start():
    data = ROOT / ".runtime" / ".jiuwenswarm"
    if not (data / "config" / "config.yaml").exists():
        raise ValueError("Сначала выполните runtime-init")
    from dotenv import dotenv_values

    settings = dotenv_values(data / "config" / ".env", interpolate=False)
    router_key = settings.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not router_key or not router_key.strip():
        raise ValueError("Заполните OPENROUTER_API_KEY в .runtime/.jiuwenswarm/config/.env")
    env = runtime_env()
    # Upstream app/AgentServer/Gateway call load_dotenv_runtime(override=True).
    # Resolve the local file once here, then stop children overwriting resolved keys.
    for key, value in settings.items():
        if value is not None and key not in {
            "API_KEY",
            "OPENROUTER_API_KEY",
            "API_BASE",
            "MODEL_NAME",
            "MODEL_PROVIDER",
        }:
            env[key] = value
    # Explicit values prevent inherited direct-provider API_KEY/API_BASE from taking precedence.
    env.update(
        OPENROUTER_API_KEY=router_key.strip(),
        API_KEY=router_key.strip(),
        API_BASE=OPENROUTER_BASE,
        MODEL_NAME=settings.get("MODEL_NAME") or DEFAULT_MODEL,
        MODEL_PROVIDER="OpenAI",
        PYTHON_DOTENV_DISABLED="1",
    )
    executable = Path(sys.executable).parent / "jiuwenswarm-start"
    # The native package launches its real gateway, agent server and web interface.
    return subprocess.call([str(executable)], cwd=ROOT, env=env)
