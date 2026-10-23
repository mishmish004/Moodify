"""Phase 1 — configuration: .env syntax, Settings validation, required secrets."""
import re
from urllib.parse import urlparse

import pytest

from .conftest import ROOT, dotenv, run_py

DOTENV_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=")


def test_env_file_is_valid_dotenv():
    bad = [
        f"line {i}: {line.strip()}"
        for i, line in enumerate((ROOT / ".env").read_text().splitlines(), 1)
        if line.strip() and not line.lstrip().startswith("#") and not DOTENV_LINE.match(line)
    ]
    assert not bad, "unparseable .env lines (python code pasted into .env?):\n" + "\n".join(bad)


def test_settings_load():
    proc = run_py("from src.core.config import settings; print(settings.ENV, settings.COLLECTION_NAME)")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_config_errors_raise_instead_of_exiting():
    """A bad env must surface as an exception; sys.exit() at import kills uvicorn workers and pytest silently."""
    code = (
        "try:\n    import src.core.config\n"
        "except SystemExit:\n    print('SYSTEM_EXIT')\n"
        "except Exception as e:\n    print('RAISED', type(e).__name__)\n"
    )
    proc = run_py(code, env={"ENV": "not-a-valid-env"})
    assert "RAISED" in proc.stdout, f"config called sys.exit() on bad input: {proc.stdout.strip()}"


@pytest.mark.parametrize("key", ["QDRANT_API_KEY", "OPENAI_API_KEY", "OPENAI_MODEL"])
def test_required_secret_present(settings, key):
    value = getattr(settings, key, None)
    raw = value.get_secret_value() if hasattr(value, "get_secret_value") else value
    assert raw, f"{key} missing from Settings / .env"


def test_qdrant_url_is_a_valid_http_url(settings):
    url = urlparse(settings.QDRANT_URL or "")
    assert url.scheme in {"http", "https"} and url.hostname, (
        f"QDRANT_URL={settings.QDRANT_URL!r}; the cloud API key is useless without the cluster URL"
    )


def test_env_pattern_is_anchored(settings):
    from pydantic import ValidationError
    from src.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(ENV="production-ish")


def test_collection_name_cannot_be_empty(settings):
    from pydantic import ValidationError
    from src.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(COLLECTION_NAME="")


def test_prod_requires_qdrant_api_key(settings):
    from pydantic import ValidationError
    from src.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(ENV="prod", QDRANT_API_KEY=None)


def test_dotenv_and_settings_agree_on_collection(settings):
    assert dotenv().get("COLLECTION_NAME") == settings.COLLECTION_NAME
