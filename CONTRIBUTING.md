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

## Which branch to work from

**Base your work on `experimental`, not `main`.**

`experimental` is where active development happens. `main` only moves at releases,
so it can sit well behind — a change written against `main` may duplicate work that
already exists on `experimental`, or conflict with it badly enough that there's no
clean way to merge it.

**But don't run `experimental` against a library you care about.** It is a working
branch, not a preview build: any given commit can be mid-refactor, half-finished or
simply broken, and the `ghcr.io/thrill-burn/auditorr:experimental` image is published
on every push without that being a claim it works. For an install you actually use,
stay on a tagged release. If you need to test a change against real data, take a
backup first — auditorr generates scripts that delete and relink files.

```bash
git clone https://github.com/thrill-burn/auditorr.git
cd auditorr
git checkout experimental
git checkout -b my-change
```

When you open the pull request, set the base branch to `experimental`. GitHub will
default it to `main` — change it.

**If you're planning anything substantial, open an issue first and ask whether it's
already built.** Large parts of auditorr have been reworked on `experimental` without
a corresponding release, and checking costs you a message where finding out afterwards
costs you the work.

## Pull requests

1. Fork the repo and create a branch from `experimental` (see above)
2. Make your changes
3. Run the backend tests — from `experimental`, where they gate the CI build, so a
   red suite blocks the image. All I/O is mocked; no live qBittorrent or arr needed:

   ```bash
   pip install -r requirements-dev.txt
   python -m pytest backend_tests
   ```
4. Test by building the Docker image locally: `docker build -t auditorr .`
5. Submit a pull request against `experimental`, with a clear description of what
   changed and why

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
