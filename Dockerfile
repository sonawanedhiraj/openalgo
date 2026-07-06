# syntax=docker/dockerfile:1.4
# ------------------------------ Python Builder Stage ----------------------- #
FROM python:3.12-bullseye AS python-builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl build-essential && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock* ./
# The cache mount on /root/.cache/uv lets uv reuse its downloaded wheel + http
# cache across builds when pyproject.toml is unchanged — turning a minute-scale
# reinstall into seconds. Requires BuildKit (default in Docker 23+).
RUN --mount=type=cache,target=/root/.cache/uv \
    pip install --no-cache-dir uv && \
    uv venv .venv && \
    uv pip install --upgrade pip && \
    uv sync && \
    uv pip install "gunicorn>=25.0,<26" eventlet

# ------------------------------ Frontend Builder Stage --------------------- #
# Node 24 matches .nvmrc + the dist-freshness CI; Vite minification produces
# different asset hashes across Node majors so any divergence breaks
# dist-freshness for contributors.
FROM node:24-bookworm-slim AS frontend-builder
WORKDIR /app
# --max-old-space-size=4096 raises Node's V8 heap cap from the ~2GB default
# to 4GB so the Vite/Rollup minify of the React bundle doesn't OOM
# (npm run build exit 134 / SIGABRT) on a constrained Docker runner.
ENV NODE_OPTIONS="--max-old-space-size=4096"
COPY frontend/package*.json ./frontend/
# npm ci (vs npm install) honours the committed package-lock.json for a
# faster, deterministic install. The cache mount on /root/.npm reuses
# downloaded tarballs across builds.
RUN --mount=type=cache,target=/root/.npm \
    cd frontend && npm ci
COPY frontend/ ./frontend/
RUN cd frontend && npm run build

# --------------------------------------------------------------------------- #
# ------------------------------ Production Stage --------------------------- #
FROM python:3.12-slim-bullseye AS production
# 0 – set timezone to IST (Asia/Kolkata) & install runtime dependencies
#     chromium + fonts-liberation are required by Kaleido 1.x (plotly static
#     image export) which drives a real headless Chromium via choreographer.
#     Without these, /chart in the Telegram bot silently fails inside Docker.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    curl \
    libopenblas0 \
    libgomp1 \
    libgfortran5 \
    chromium \
    fonts-liberation && \
    ln -fs /usr/share/zoneinfo/Asia/Kolkata /etc/localtime && \
    dpkg-reconfigure -f noninteractive tzdata && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
# 1 – user & workdir.
#     Pin appuser to UID/GID 1000 explicitly. install-docker.sh and
#     install-docker-multi-custom-ssl.sh chown the host's .env to UID 1000
#     before bind-mounting it into the container; if the container's
#     appuser ends up at a different UID (which can happen on ARM64 base
#     images that already have system users at low UIDs, or if the base
#     image bumps its useradd defaults), the bind-mounted .env becomes
#     unwritable to the running process. See marketcalls/openalgo#1394.
RUN groupadd --gid 1000 appuser && \
    useradd --create-home --uid 1000 --gid 1000 appuser
WORKDIR /app
# 2 – copy the ready-made venv and source with correct ownership
COPY --from=python-builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --chown=appuser:appuser . .
# 3 - copy built frontend from frontend-builder
COPY --from=frontend-builder --chown=appuser:appuser /app/frontend/dist /app/frontend/dist
# 4 – create required directories with proper ownership and permissions
#     Also create empty .env file with write permissions for Railway deployment
#
#     NOTE: chown /app itself (not just its contents). WORKDIR creates /app
#     as root:root mode 755, and that ownership persists even after we
#     COPY --chown=appuser:appuser into it. Without this chown the running
#     appuser process can read/execute /app but cannot create new files
#     there — which breaks any atomic-write helper that needs to put a
#     temp file in /app (e.g. utils/env_check.py rotating FERNET_SALT in
#     /app/.env). See marketcalls/openalgo#1394.
RUN mkdir -p /app/log /app/log/strategies /app/db /app/tmp /app/tmp/numba_cache /app/tmp/matplotlib /app/strategies /app/strategies/scripts /app/strategies/examples /app/keys && \
    chown appuser:appuser /app && \
    chown -R appuser:appuser /app/log /app/db /app/tmp /app/strategies /app/keys && \
    chmod -R 755 /app/strategies /app/log /app/tmp && \
    chmod 700 /app/keys && \
    touch /app/.env && chown appuser:appuser /app/.env && chmod 666 /app/.env
# 5 – entrypoint script and fix line endings
COPY --chown=appuser:appuser start.sh /app/start.sh
RUN sed -i 's/\r$//' /app/start.sh && chmod +x /app/start.sh
# ---- RUNTIME ENVS --------------------------------------------------------- #
# Limit OpenBLAS/NumPy threads to prevent RLIMIT_NPROC exhaustion in Docker
# See: https://github.com/marketcalls/openalgo/issues/822
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Kolkata \
    APP_MODE=standalone \
    TMPDIR=/app/tmp \
    NUMBA_CACHE_DIR=/app/tmp/numba_cache \
    LLVMLITE_TMPDIR=/app/tmp \
    MPLCONFIGDIR=/app/tmp/matplotlib \
    OPENBLAS_NUM_THREADS=2 \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    NUMEXPR_NUM_THREADS=2 \
    NUMBA_NUM_THREADS=2 \
    BROWSER_PATH=/usr/bin/chromium \
    CHROME_BIN=/usr/bin/chromium
# --------------------------------------------------------------------------- #
USER appuser
EXPOSE 5000
CMD ["/app/start.sh"]
