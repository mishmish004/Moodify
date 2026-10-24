"""Phase 8: the Docker image, and the deployed app.

Docker tests need the Docker daemon and build the image (about a minute the first time):
    .venv/bin/python -m pytest -m docker
The live test checks the deployed URL; set MOODIFY_URL to it:
    MOODIFY_URL=https://... .venv/bin/python -m pytest -m live tests/test_p8_deploy.py
"""
import os
import subprocess
import time
import uuid
from contextlib import contextmanager

import httpx
import pytest

from .conftest import ROOT

IMAGE = "moodify:test"
MAX_IMAGE_MB = 400
BOOT_SECONDS = 10
# ENV=prod, so the container also runs the production settings checks (HTTPS redirect URI, secure cookie)
CONTAINER_ENV = {
    "ENV": "prod",
    "SECRET_KEY": "docker-test-secret",
    "SPOTIFY_CLIENT_ID": "docker-test-client",
    "OPENAI_API_KEY": "docker-test-key",
    "SPOTIFY_REDIRECT_URI": "https://moodify.example/callback",
    "GOOGLE_CLIENT_ID": "docker-test-google-client",
    "GOOGLE_CLIENT_SECRET": "docker-test-google-secret",
    "BASE_URL": "https://moodify.example",
}


def docker(*args: str) -> str:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, f"docker {' '.join(args)} failed:\n{proc.stderr[-3000:]}"
    return proc.stdout.strip()


@pytest.fixture(scope="module")
def image() -> str:
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.fail("the Docker daemon isn't reachable; start Docker and run again")
    docker("build", "--tag", IMAGE, str(ROOT))
    return IMAGE


@pytest.fixture
def volume():
    name = f"moodify-test-{uuid.uuid4().hex[:8]}"
    docker("volume", "create", name)
    yield name
    subprocess.run(["docker", "volume", "rm", "--force", name], capture_output=True)


@contextmanager
def running(image: str, volume: str):
    """A container on a random localhost port, removed afterwards. Yields (name, base url)."""
    name = f"moodify-test-{uuid.uuid4().hex[:8]}"
    env = [arg for key, value in CONTAINER_ENV.items() for arg in ("--env", f"{key}={value}")]
    docker("run", "--detach", "--name", name, "--publish", "127.0.0.1::8000", "--volume", f"{volume}:/app/data", *env, image)
    try:
        yield name, url_of(name)
    finally:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True)


def url_of(name: str) -> str:
    # Look it up again after a restart: Docker may pick a new random port
    port = docker("port", name, "8000").splitlines()[0].rsplit(":", 1)[1]
    return f"http://127.0.0.1:{port}"


def wait_for_health(name: str, url: str, seconds: float = BOOT_SECONDS) -> float:
    """Seconds until GET /health answered 200. Fails with the container's log if it never does."""
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                return time.monotonic() - start
        except httpx.TransportError:
            pass
        time.sleep(0.2)
    logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
    pytest.fail(f"/health not 200 within {seconds} s. Container log:\n{(logs.stdout + logs.stderr)[-3000:]}")


def in_container(name: str, code: str) -> str:
    return docker("exec", name, "python", "-c", code)


# --- image ------------------------------------------------------------------------------------

@pytest.mark.docker
def test_image_is_under_400_mb(image):
    # Measured from inside: `docker image inspect` reports the compressed size under the containerd store
    size_mb = int(docker("run", "--rm", "--user", "0", "--entrypoint", "du", image, "-sxm", "/").split()[0])
    assert size_mb < MAX_IMAGE_MB, f"image is {size_mb:,} MB unpacked"


@pytest.mark.docker
def test_image_holds_the_app_and_no_secrets_or_data(image):
    listing = docker("run", "--rm", "--entrypoint", "sh", image, "-c", "ls -A /app; ls -A /app/data")
    assert set(listing.split()) == {"requirements.txt", "src", "templates", "static", "data", "logs"}


# --- running container --------------------------------------------------------------------------

@pytest.mark.docker
def test_container_serves_health_within_10_s(image, volume):
    with running(image, volume) as (name, url):
        wait_for_health(name, url)
        assert httpx.get(f"{url}/health").json() == {"status": "ok"}
        assert httpx.get(f"{url}/static/style.css").status_code == 200
        # Every page waits behind the Google sign-in, the Spotify login included
        assert httpx.get(f"{url}/login").headers["location"] == "/signin"
        signin = httpx.get(f"{url}/signin")
        assert signin.status_code == 200 and "Sign in with Google" in signin.text
        assert in_container(name, "import os; print(os.getuid())") != "0"


@pytest.mark.docker
def test_docker_healthcheck_reports_healthy(image, volume):
    with running(image, volume) as (name, url):
        deadline = time.monotonic() + 20
        status = ""
        while time.monotonic() < deadline:
            status = docker("inspect", name, "--format", "{{.State.Health.Status}}")
            if status == "healthy":
                break
            time.sleep(0.5)
        assert status == "healthy"


@pytest.mark.docker
def test_data_survives_restart_and_a_new_container(image, volume):
    write = "from src import users; users.upsert('friend1', 'Friend One', 'refresh-1')"
    read = "from src import users; print(users.get('friend1')['display_name'])"
    with running(image, volume) as (name, url):
        wait_for_health(name, url)
        in_container(name, write)
        docker("restart", name)
        wait_for_health(name, url_of(name))
        assert in_container(name, read) == "Friend One"
    # A redeploy replaces the container; only the volume carries over
    with running(image, volume) as (name, url):
        wait_for_health(name, url)
        assert in_container(name, read) == "Friend One"


# --- deployed -------------------------------------------------------------------------------------

@pytest.mark.live
def test_deployed_health_over_https():
    url = os.environ.get("MOODIFY_URL", "")
    if not url:
        pytest.skip("set MOODIFY_URL to the deployed https:// address")
    assert url.startswith("https://")
    resp = httpx.get(f"{url.rstrip('/')}/health", timeout=10)
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}
