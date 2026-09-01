"""Shared runtime provenance for paper ledgers.

Every new decision/event/status record can attach the exact frozen inputs that
produced it: strategy identity, active config hash, registry hash, and Git
revision. This makes any later report traceable to the configuration that
generated it, without giving runners any ability to change configuration.
"""
import hashlib
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REGISTRY = ROOT / "strategy_registry.json"


def canonical_hash(value):
    """Stable SHA-256 over a JSON-serializable value."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def file_sha256(path):
    path = Path(path)
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_revision():
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=10,
            check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def strategy_version(strategy_id, registry=None):
    registry = registry if registry is not None else _load_registry()
    for strategy in registry.get("strategies", []):
        if strategy.get("strategy_id") == strategy_id:
            return strategy.get("version")
    return None


def _load_registry():
    try:
        return json.loads(REGISTRY.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def provenance(strategy_id, config_path=None, run_id=None):
    registry = _load_registry()
    config_file = Path(config_path) if config_path else None
    return {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version(strategy_id, registry),
        "config_sha256": file_sha256(config_file) if config_file else None,
        "registry_sha256": file_sha256(REGISTRY),
        "git_revision": git_revision(),
        "run_id": run_id or f"{strategy_id}:{int(time.time() * 1000)}",
    }
