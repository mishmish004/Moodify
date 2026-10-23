"""Phase 2 — dependencies and imports: requirements.txt is resolvable as intended and every module imports."""
import ast
import json
import re
import subprocess
import sys
from importlib.metadata import packages_distributions
from pathlib import Path

import pytest

from .conftest import PY, ROOT, run_py

LOCAL_TOPLEVEL = {"src", "scripts", "notebooks", "tests", "utils", "app"}
CODE_DIRS = ["src", "scripts", "notebooks"]


def _requirement_lines():
    for raw in (ROOT / "requirements.txt").read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            yield line


def _req_name(line: str) -> str:
    return re.split(r"[\s<>=!~\[;]", line, 1)[0].lower().replace("_", "-")


def _declared() -> set[str]:
    return {_req_name(line) for line in _requirement_lines()}


def _code_files():
    for d in CODE_DIRS:
        yield from (ROOT / d).rglob("*.py")


def _module_id(path: Path) -> str:
    return str(path.relative_to(ROOT))


def test_requirements_have_no_duplicates():
    names = [_req_name(line) for line in _requirement_lines()]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"duplicate requirements: {dupes}"


def test_no_per_line_index_url():
    """pip silently ignores --index-url on a requirement line, so torch comes from PyPI with CUDA wheels."""
    bad = [line for line in _requirement_lines() if "--index-url" in line or "--extra-index-url" in line]
    assert not bad, f"move these to a top-level `--extra-index-url` line: {bad}"


def test_fragile_ml_transitives_pinned():
    """laion-clap 1.1.4 breaks on new transformers (state-dict keys) and new torch; pin what it was built against."""
    missing = {"transformers"} - _declared()
    assert not missing, f"pin these in requirements.txt: {sorted(missing)}"


def test_every_third_party_import_is_declared():
    dist_for = {k: [d.lower().replace("_", "-") for d in v] for k, v in packages_distributions().items()}
    stdlib = set(sys.stdlib_module_names)
    declared = _declared()
    missing = {}
    for path in _code_files():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module]
            else:
                continue
            for mod in mods:
                top = mod.split(".")[0]
                if top in stdlib or top in LOCAL_TOPLEVEL:
                    continue
                dists = dist_for.get(top, [top.lower().replace("_", "-")])
                if not declared.intersection(dists):
                    missing.setdefault(dists[0], set()).add(_module_id(path))
    assert not missing, "imported but not declared in requirements.txt:\n" + "\n".join(
        f"  {d}: {sorted(files)}" for d, files in sorted(missing.items())
    )


def _import_code(path: Path) -> str:
    rel = path.relative_to(ROOT)
    if rel.parts[0] == "src":
        dotted = ".".join(rel.with_suffix("").parts).removesuffix(".__init__")
        return f"import importlib; importlib.import_module({dotted!r})"
    # scripts/ and notebooks/ are entrypoints: execute top level without triggering __main__
    return f"import runpy; runpy.run_path({str(path)!r}, run_name='__checkup__')"


@pytest.mark.parametrize("path", sorted(_code_files()), ids=_module_id)
def test_module_imports(path):
    proc = run_py(_import_code(path), timeout=300)
    tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-6:])
    assert proc.returncode == 0, f"{_module_id(path)} fails at import:\n{tail}"


@pytest.mark.live
def test_resolved_torch_is_cpu_only(tmp_path):
    """Resolve requirements.txt exactly as the Dockerfile does and make sure no CUDA wheels sneak in."""
    report = tmp_path / "report.json"
    proc = subprocess.run(
        [PY, "-m", "pip", "install", "--dry-run", "--ignore-installed", "-q", "--report", str(report),
         "-r", str(ROOT / "requirements.txt")],
        capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    names = [i["metadata"]["name"].lower() for i in json.loads(report.read_text())["install"]]
    cuda = sorted(n for n in names if n.startswith("nvidia-") or n == "triton")
    assert not cuda, f"CPU image would pull {len(cuda)} CUDA packages: {cuda}"
