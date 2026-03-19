# Contributing to auditorr

Thanks for your interest in contributing!

## Reporting bugs

Open an issue and include:
- What you expected to happen
- What actually happened
- Your setup (Unraid version, qBittorrent version, Docker version)
- Relevant logs from `docker logs auditorr`

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
