# ── Stage 1: Build dependencies ──────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# System deps for asyncpg compilation
RUN apt-get update && apt-get install -y \
    build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: Runtime image ────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Runtime system deps only
RUN apt-get update && apt-get install -y \
    libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini .

# FIX: Removed `COPY VLCR_Frontend_Linked.html .`
# The frontend is a separate deployment target (frontend/ → Vercel).
# It has no place in the backend Docker image.

# Non-root user for security
RUN adduser --disabled-password --gecos "" vlcr \
    && chown -R vlcr:vlcr /app
USER vlcr

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
