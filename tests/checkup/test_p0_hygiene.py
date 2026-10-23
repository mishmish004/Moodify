"""Phase 0 — repo hygiene: secrets, ignore files, git index, Docker build context."""
import re
import subprocess

import pytest

from .conftest import ROOT

SECRET_PATTERNS = {
    "jwt (qdrant api key)": re.compile(r"eyJ[\w-]{10,}\.eyJ[\w-]{10,}\.[\w-]{10,}"),
    "openai key": re.compile(r"sk-(?:proj-)?[\w-]{20,}"),
}
SCAN_SKIP = {".git", ".venv", "venv", ".qdrant_storage", "__pycache__", ".pytest_cache"}
TEXT_SUFFIXES = {".py", ".yml", ".yaml", ".toml", ".txt", ".md", ".json", ".cfg", ".ini", ".ipynb", ""}

MUST_BE_DOCKERIGNORED = [
    ".env",
    ".git/HEAD",
    ".venv/bin/python",
    ".qdrant_storage/raft_state.json",
    "_archive/graveyard_backup/fma_small.zip",
    "_trash/session_backup.py",
    "logs/moodify.log",
    "src/core/__pycache__/config.cpython-310.pyc",
]
MAX_CONTEXT_MB = 100


def _text_files():
    for p in ROOT.rglob("*"):
        rel = p.relative_to(ROOT)
        if any(part in SCAN_SKIP for part in rel.parts) or not p.is_file():
            continue
        if p.name.startswith(".env") or p.suffix not in TEXT_SUFFIXES or p.stat().st_size > 2_000_000:
            continue
        yield p


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


def test_no_hardcoded_secrets_in_source():
    hits = []
    for path in _text_files():
        text = path.read_text(errors="ignore")
        for label, rx in SECRET_PATTERNS.items():
            for m in rx.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                hits.append(f"{path.relative_to(ROOT)}:{line} ({label})")
    assert not hits, "secrets committed to source (rotate them, then delete):\n" + "\n".join(sorted(set(hits)))


@pytest.mark.parametrize(
    "path",
    [".env", ".venv/pyvenv.cfg", ".qdrant_storage/raft_state.json", "_archive/x.zip", "data/fma_metadata/tracks.csv", "logs/moodify.log"],
)
def test_gitignore_covers(path):
    proc = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
    assert proc.returncode == 0, f"{path} is not git-ignored"


@pytest.mark.parametrize("rel", MUST_BE_DOCKERIGNORED)
def test_dockerignore_excludes(rel):
    assert _dockerignored(rel, _dockerignore_patterns()), f"{rel} would be sent to the Docker build context / COPY . ."


def test_docker_build_context_is_small():
    patterns = _dockerignore_patterns()
    total, biggest = 0, []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(ROOT))
        if _dockerignored(rel, patterns):
            continue
        size = p.stat().st_size
        total += size
        biggest.append((size, rel))
    biggest.sort(reverse=True)
    top = "\n".join(f"  {s / 1e6:,.1f} MB  {r}" for s, r in biggest[:5])
    assert total / 1e6 < MAX_CONTEXT_MB, f"build context is {total / 1e9:.2f} GB; largest files:\n{top}"


def test_git_index_has_no_staged_files_missing_on_disk():
    out = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout
    broken = [line[3:] for line in out.splitlines() if line[:2] == "AD"]
    assert not broken, f"staged as added but deleted from disk: {broken}"
