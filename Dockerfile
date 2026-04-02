FROM node:20-slim AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY --from=frontend-build /build/dist ./frontend/dist

RUN mkdir -p /app/data

LABEL org.opencontainers.image.title="auditorr"
LABEL org.opencontainers.image.description="Media library audit tool for qBittorrent + Sonarr/Radarr"

EXPOSE ${AUDITORR_PORT:-8677}

CMD ["sh", "-c", "gunicorn app:app --workers 1 --bind 0.0.0.0:${AUDITORR_PORT:-8677} --timeout 300 --access-logfile - --error-logfile -"]
