"""
Shared harness for the phased Moodify checkup.

Phases (run in order, each one assumes the previous passes):
  p0 hygiene -> p1 config -> p2 deps/imports -> p3 docker -> p4 qdrant
  -> p5 ingestion -> p6 embeddings -> p7 query+search -> p8 api -> p9 stress
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
QDRANT_LOCAL = "http://localhost:6333"
STORAGE = ROOT / ".qdrant_storage"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run_py(code: str, timeout: int = 180, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run code in a fresh interpreter so sys.exit() / import side effects can't kill pytest."""
    full_env = {**os.environ, "PYTHONPATH": str(ROOT), **(env or {})}
    return subprocess.run(
        [PY, "-c", code], cwd=ROOT, env=full_env, capture_output=True, text=True, timeout=timeout
    )


def dotenv() -> dict:
    from dotenv import dotenv_values

    return {k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None}


def http_json(url: str, headers: dict | None = None, timeout: float = 5.0):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read() or b"null")


def _reachable(url: str) -> bool:
    try:
        http_json(url, timeout=2)
        return True
    except Exception:
        return False


def local_collections() -> dict:
    """Collections persisted in .qdrant_storage, read straight from disk."""
    out = {}
    for cfg in (STORAGE / "collections").glob("*/config.json"):
        out[cfg.parent.name] = json.loads(cfg.read_text())
    return out


@pytest.fixture(scope="session")
def local_qdrant() -> str:
    """Start the compose `qdrant` service (mounts .qdrant_storage) and wait until it answers."""
    if not _reachable(QDRANT_LOCAL):
        proc = subprocess.run(
            ["docker", "compose", "up", "-d", "qdrant"], cwd=ROOT, capture_output=True, text=True
        )
        if proc.returncode != 0:
            pytest.fail(f"docker compose up qdrant failed:\n{proc.stderr}")
        deadline = time.time() + 60
        while not _reachable(QDRANT_LOCAL):
            if time.time() > deadline:
                pytest.fail("local qdrant did not become ready within 60s")
            time.sleep(1)
    return QDRANT_LOCAL


@pytest.fixture(scope="session")
def settings():
    proc = run_py("import src.core.config")
    if proc.returncode != 0:
        pytest.fail(f"src.core.config does not import (see phase 1):\n{proc.stdout}{proc.stderr}")
    from src.core.config import settings as s

    return s
