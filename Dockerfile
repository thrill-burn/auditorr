# Pinned to the build host's architecture: the Vite output is plain JS/CSS and
# is identical on every target, so building it natively keeps the arm64 image
# from paying for an emulated npm build.
FROM --platform=$BUILDPLATFORM node:20-slim AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
# glibc gives each thread pool its own malloc arena (8 gthread workers → up to
# 8×), multiplying heap fragmentation under scan-heavy allocation. Two arenas
# is the standard setting for long-running Python services and materially
# lowers steady-state RSS on very large libraries.
ENV MALLOC_ARENA_MAX=2

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY sources/ ./sources/
COPY --from=frontend-build /build/dist ./frontend/dist

RUN mkdir -p /app/data

LABEL org.opencontainers.image.title="auditorr"
LABEL org.opencontainers.image.description="Media library audit tool for qBittorrent + Sonarr/Radarr"

EXPOSE ${AUDITORR_PORT:-8677}

# gthread worker: the master heartbeat runs in the worker's main loop, so a
# slow request (streaming a huge /api/files) or a CPU-heavy audit thread can't
# trip the timeout and get the worker SIGKILLed mid-scan — which previously
# restarted the worker, re-triggered the startup audit, and looped forever on
# very large libraries. Threads also let /api/progress polls respond while a
# large response is being served.
CMD ["sh", "-c", "gunicorn app:app --workers 1 --worker-class gthread --threads 8 --bind 0.0.0.0:${AUDITORR_PORT:-8677} --timeout 300 --access-logfile - --error-logfile -"]
