"""Phase 0: the old pipeline is gone, nothing secret is in the repo, and an empty app runs."""
import ast
import os
import re
import subprocess
import sys

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient

from src.main import app

from .conftest import ROOT

# --- secrets -----------------------------------------------------------------

SECRET_PATTERNS = {
    "openai key": re.compile(r"sk-(?:proj-)?[\w-]{20,}"),
    "jwt": re.compile(r"eyJ[\w-]{10,}\.eyJ[\w-]{10,}\.[\w-]{10,}"),
    "client_secret value": re.compile(r"client_secret[\"']?\s*[:=]\s*[\"']?[0-9A-Za-z]{20,}", re.I),
}
SECRET_ENV_KEY = re.compile(r"KEY|SECRET|TOKEN|PASSWORD")


def _repo_files():
    """Files git would commit: tracked plus untracked-but-not-ignored."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    for rel in sorted(set(filter(None, out.split("\0")))):
        path = ROOT / rel
        if path.is_file() and path.stat().st_size < 2_000_000:
            yield rel, path.read_text(errors="ignore")


def test_no_secrets_in_repo_files():
    env_values = {}
    if (ROOT / ".env").exists():
        env_values = {
            k: v for k, v in dotenv_values(ROOT / ".env").items()
            if v and len(v) >= 8 and SECRET_ENV_KEY.search(k)
        }
    hits = []
    for rel, text in _repo_files():
        for label, rx in SECRET_PATTERNS.items():
            for m in rx.finditer(text):
                hits.append(f"{rel}:{text.count(chr(10), 0, m.start()) + 1} ({label})")
        for key, value in env_values.items():
            if value in text:
                hits.append(f"{rel} (value of .env {key})")
    assert not hits, "secrets in files git would commit (rotate them, then remove):\n" + "\n".join(hits)


# --- ignore files ------------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    [".env", ".env.local", ".venv/pyvenv.cfg", "moodify.db", "data/moodify.db", "data/moodify.db-wal",
     "_archive/x.zip", ".qdrant_storage/raft_state.json", "logs/moodify.log"],
)
def test_gitignore_covers(path):
    assert subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode == 0, f"{path} is not git-ignored"


def test_env_example_is_not_gitignored():
    assert subprocess.run(["git", "check-ignore", "-q", ".env.example"], cwd=ROOT).returncode == 1


def _dockerignore_patterns():
    patterns = []
    for line in (ROOT / ".dockerignore").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        pat = line.lstrip("!").strip("/")
        # Docker semantics: anchored at context root, `*` stays inside one segment, `**` spans segments.
        rx = re.escape(pat).replace(r"\*\*", "§").replace(r"\*", "[^/]*").replace(r"\?", "[^/]").replace("§", ".*")
        patterns.append((re.compile(rf"^{rx}$"), negate))
    return patterns


def _dockerignored(rel: str, patterns) -> bool:
    parts = rel.split("/")
    prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    ignored = False
    for rx, negate in patterns:
        if any(rx.match(p) for p in prefixes):
            ignored = not negate
    return ignored


@pytest.mark.parametrize(
    "rel",
    [".env", ".git/HEAD", ".venv/bin/python", "data/moodify.db", "logs/moodify.log",
     ".qdrant_storage/raft_state.json", "_archive/graveyard_backup/fma_small.zip",
     "_trash/session_backup.py", "src/core/__pycache__/config.cpython-310.pyc",
     "portfolio/node_modules/astro/package.json"],
)
def test_dockerignore_excludes(rel):
    assert _dockerignored(rel, _dockerignore_patterns()), f"{rel} would be sent to the Docker build context"


@pytest.mark.parametrize(
    "rel", ["requirements.txt", "src/main.py", "src/db.py", "src/schema.sql", "templates/base.html", "static/style.css"]
)
def test_dockerignore_keeps_what_the_image_needs(rel):
    assert not _dockerignored(rel, _dockerignore_patterns()), f"{rel} is excluded but the image needs it"


def test_docker_build_context_is_small():
    patterns = _dockerignore_patterns()
    total, biggest = 0, []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel_dir = os.path.relpath(dirpath, ROOT)
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = [d for d in dirnames if not _dockerignored(rel_dir + d, patterns)]
        for name in filenames:
            rel = rel_dir + name
            if not _dockerignored(rel, patterns):
                size = os.path.getsize(os.path.join(dirpath, name))
                total += size
                biggest.append((size, rel))
    biggest.sort(reverse=True)
    top = "\n".join(f"  {s / 1e6:,.1f} MB  {r}" for s, r in biggest[:5])
    assert total / 1e6 < 5, f"build context is {total / 1e6:,.1f} MB; largest files:\n{top}"


def test_git_index_has_no_staged_files_missing_on_disk():
    out = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout
    broken = [line[3:] for line in out.splitlines() if line[:2] == "AD"]
    assert not broken, f"staged as added but deleted from disk: {broken}"


# --- src/ --------------------------------------------------------------------

BANNED_IMPORTS = {"torch", "laion_clap", "qdrant_client", "redis"}
DROPPED_REQUIREMENTS = {"torch", "laion-clap", "qdrant-client", "redis", "tinytag", "requests"}


def _src_modules():
    for path in sorted((ROOT / "src").rglob("*.py")):
        parts = path.relative_to(ROOT).with_suffix("").parts
        yield ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def test_src_modules_import_without_side_effects(tmp_path):
    """Fresh interpreter, empty environment, no .env values: imports must not read settings,
    print, create files or exit."""
    db = tmp_path / "must-not-exist.db"
    code = (
        "import importlib, sys\n"
        f"for m in {list(_src_modules())!r}: importlib.import_module(m)\n"
        "from src.core.config import get_settings\n"
        "sys.stdout.write(str(get_settings.cache_info().currsize))\n"
    )
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", "DB_PATH": str(db)}
    proc = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert proc.stdout == "0", "a module loads settings at import time"
    assert list(tmp_path.iterdir()) == [], "importing created files"


def test_no_banned_imports_in_src():
    hits = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            hits += [f"{path.relative_to(ROOT)}:{node.lineno} {n}" for n in names if n.split(".")[0] in BANNED_IMPORTS]
    assert not hits, "old-pipeline imports left in src/:\n" + "\n".join(hits)


def test_requirements_drop_the_old_stack():
    names = {
        re.split(r"[\[=<>~ ;]", line.strip())[0].lower()
        for line in (ROOT / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "-"))
    }
    assert not names & DROPPED_REQUIREMENTS, f"still required: {sorted(names & DROPPED_REQUIREMENTS)}"


# --- app ---------------------------------------------------------------------

def test_health():
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
