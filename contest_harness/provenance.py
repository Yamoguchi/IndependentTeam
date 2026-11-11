"""Source fingerprints for run and evaluation manifests."""

from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = Path(__file__).resolve().parent


def runtime_hashes() -> dict[str, str]:
    files = [ROOT / "run_agent.py"]
    files.extend(path for path in PACKAGE.rglob("*.py") if path.relative_to(PACKAGE).parts[0] != "evaluation")
    return {path.relative_to(ROOT).as_posix(): sha256(path.read_bytes()).hexdigest() for path in sorted(files)}
