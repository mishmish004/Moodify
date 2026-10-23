# One small image: the app, its templates and static files. Settings come from the environment at
# runtime (.env through compose locally, the host's variables in prod) and are never baked in.
# The SQLite file lives on a volume mounted at /app/data.
# Python 3.10 matches the dev venv the tests run in.
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Every dependency ships a manylinux wheel, so no compiler is needed
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src/ src/
COPY templates/ templates/
COPY static/ static/

# Non-root. /app/data (the SQLite volume) and /app/logs (MoodifyLogger's file) must be writable by it;
# a new named volume takes this ownership from the image
RUN useradd --create-home moodify && mkdir -p /app/data /app/logs && chown moodify /app/data /app/logs
USER moodify

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --start-interval=1s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/health', timeout=2)"

# Hosts such as Railway pass the port in $PORT. TLS ends at the host's proxy, hence --proxy-headers.
# exec makes uvicorn PID 1, so it gets SIGTERM and shuts down cleanly.
CMD exec uvicorn src.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips "*"
