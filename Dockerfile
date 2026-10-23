# Stage 1: Builder (Compiling dependencies)
FROM python:3.10-slim as builder

# Prevent Python from writing pyc files and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system build dependencies (needed for uvloop, numpy, hiredis)
RUN apt-get update && apt-get install -y \
    build-essential \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first to leverage Docker cache
COPY requirements.txt .
RUN pip install --prefix=/install --no-cache-dir -r requirements.txt

# Stage 2: Runtime (The actual image)
FROM python:3.10-slim

WORKDIR /app

# Copy compiled dependencies from builder stage
COPY --from=builder /install /usr/local

# Copy the entire project structure
# We copy everything so 'src' and 'utils' are available
COPY . .

# PHYSICS: Set PYTHONPATH so "from src.core..." works everywhere
ENV PYTHONPATH=/app

# Create a non-root user for security (Production Standard)
# /app/data (the SQLite volume) and /app/logs (MoodifyLogger's file) must be writable by that user
RUN useradd -m moodify_user && mkdir -p /app/data /app/logs && chown moodify_user /app/data /app/logs
USER moodify_user

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
