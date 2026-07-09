# Contributing to auditorr

Thanks for your interest in contributing!

## Reporting bugs

Open an issue and include:
- What you expected to happen
- What actually happened
- Your setup (Unraid version, qBittorrent version, Docker version)
- A debug report. Open `http://<host>:8677/api/debug/report`
and paste the output into your issue or forum post. The report is
**privacy-scrubbed and safe to share publicly**: it contains no credentials,
hostnames, IPs, or API keys, and media file/folder names are replaced with
short hashes. It includes your (sanitized) configuration, library size stats,
memory usage, recent audit history, crash evidence, and recent log lines —
usually everything needed to diagnose a problem in one paste.

## Suggesting features

Open an issue describing the feature and why it would be useful. Check existing issues first to avoid duplicates.

## Pull requests

1. Fork the repo and create a branch from `main`
2. Make your changes
3. Test by building the Docker image locally: `docker build -t auditorr .`
4. Submit a pull request with a clear description of what changed and why

## Local development

```bash
# Backend
pip install -r requirements.txt
python app.py

# Frontend (separate terminal)
cd frontend
npm install
npm run dev
```

The frontend dev server proxies `/api` requests to `http://localhost:8677` (see `vite.config.js`).
