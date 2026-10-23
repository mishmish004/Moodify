"""Phase 3 — Docker image and compose stack."""
import json
import re
import socket
import subprocess

import pytest
import yaml

from .conftest import ROOT, dotenv

DOTENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
BUILDER_TAG = "moodify-checkup:builder"
IMAGE_TAG = "moodify-checkup:latest"
MAX_DEPS_GB = 2.0


def _dockerfile() -> str:
    return (ROOT / "Dockerfile").read_text()


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def _app_service() -> dict:
    services = _compose()["services"]
    return next(s for s in services.values() if "build" in s)


def _cmd() -> list[str]:
    line = [l for l in _dockerfile().splitlines() if l.strip().startswith("CMD")][-1]
    return json.loads(line.split("CMD", 1)[1].strip())


def _module_exists(dotted: str) -> bool:
    base = ROOT.joinpath(*dotted.split("."))
    return base.with_suffix(".py").exists() or (base / "__init__.py").exists()


def test_cmd_targets_existing_module():
    cmd = _cmd()
    if "-m" in cmd and cmd[cmd.index("-m") + 1] != "uvicorn":
        target = cmd[cmd.index("-m") + 1]
    else:
        target = next(a for a in cmd if ":" in a).split(":")[0]
    assert _module_exists(target), f"CMD {cmd} -> module {target!r} not found relative to PYTHONPATH=/app"


def test_runtime_stage_does_not_copy_whole_context():
    """`COPY . .` + a leaky .dockerignore ships archives, storage and caches into the image."""
    runtime = _dockerfile().split("FROM", 2)[-1]
    assert not re.search(r"^COPY\s+\.\s+\.", runtime, re.M), "copy only src/ (and scripts/) into the runtime image"


def test_compose_does_not_shadow_image_code():
    mounts = [v.split(":") for v in _app_service().get("volumes", [])]
    shadowing = [":".join(m) for m in mounts if len(m) > 1 and m[1].rstrip("/") == "/app"]
    assert not shadowing, f"{shadowing} replaces the image's /app (src/, PYTHONPATH) with the host folder"


def test_compose_supplies_required_settings():
    """.env is dockerignored (good), so compose must pass it in or Settings() fails at container start."""
    svc = _app_service()
    env_keys = set()
    for item in svc.get("environment", []):
        env_keys.add(item.split("=", 1)[0])
    has_env_file = bool(svc.get("env_file"))
    required = {"ENV", "COLLECTION_NAME", "QDRANT_URL", "QDRANT_API_KEY", "OPENAI_API_KEY"}
    assert has_env_file or required <= env_keys, f"missing in compose: {sorted(required - env_keys)} (add env_file: .env)"


def test_compose_qdrant_matches_client_version():
    image = _compose()["services"]["qdrant"]["image"]
    server = re.search(r":v?(\d+\.\d+)", image).group(1)
    req = (ROOT / "requirements.txt").read_text()
    client = re.search(r"qdrant-client==(\d+\.\d+)", req).group(1)
    assert server == client, f"qdrant server {server} vs qdrant-client {client}"


def _port_owner(port: int) -> str | None:
    with socket.socket() as s:
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", port)) != 0:
            return None
    out = subprocess.run(
        ["docker", "ps", "--filter", f"publish={port}", "--format", "{{.Names}}"], capture_output=True, text=True
    ).stdout.split()
    return out[0] if out else "a non-docker process"


@pytest.mark.parametrize(
    "port",
    sorted({int(p.split(":")[0]) for s in _compose()["services"].values() for p in s.get("ports", [])}),
)
def test_compose_host_port_available(port):
    owner = _port_owner(port)
    assert owner is None or owner.startswith("moodify-"), f"host port {port} already bound by {owner}"


@pytest.mark.docker
def test_builder_stage_builds_cpu_only():
    proc = subprocess.run(
        ["docker", "build", "--target", "builder", "-t", BUILDER_TAG, "."],
        cwd=ROOT, capture_output=True, text=True, timeout=3600,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = subprocess.run(
        ["docker", "run", "--rm", BUILDER_TAG, "sh", "-c",
         "du -sm /install | cut -f1; ls /install/lib/python3.10/site-packages | grep -c '^nvidia' || true"],
        capture_output=True, text=True,
    ).stdout.split()
    size_gb, nvidia = int(out[0]) / 1024, int(out[1])
    assert nvidia == 0 and size_gb < MAX_DEPS_GB, f"deps layer {size_gb:.1f} GB with {nvidia} nvidia packages"


@pytest.mark.docker
def test_image_builds_and_imports_services():
    from .test_p0_hygiene import test_docker_build_context_is_small

    try:
        test_docker_build_context_is_small()
    except AssertionError as e:
        pytest.fail(f"refusing to build: context too large ({e.args[0].splitlines()[0]})")
    build = subprocess.run(["docker", "build", "-t", IMAGE_TAG, "."], cwd=ROOT, capture_output=True, text=True, timeout=3600)
    assert build.returncode == 0, build.stderr[-3000:]
    env_flags = [arg for k, v in dotenv().items() if DOTENV_KEY.match(k) for arg in ("-e", f"{k}={v}")]
    run = subprocess.run(
        ["docker", "run", "--rm", *env_flags, IMAGE_TAG,
         "python", "-c", "import src.services, src.services.ai_engine; print('ok')"],
        capture_output=True, text=True, timeout=600,
    )
    assert run.stdout.strip().endswith("ok"), run.stderr[-3000:]
