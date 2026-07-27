# Support Platform POC

A single-tenant-per-workspace customer support platform (Intercom-style): live chat
widget, email channel, unified inbox, knowledge base, AI summarization, and custom
domains — built as a $0, single-process Django app. See `claude.md` (rules),
`architecture.md` (design + trade-offs), and `plan.md` (build phases).

- **Live URL:** _TBD (Phase 0 deploy) — `https://app.<domain>/healthz`_
- **Test credentials:** _TBD (seeded in Phase 10)_

## Architecture

One ASGI process (Django 5 + Channels 4 on Uvicorn, `--workers 1`), SQLite in WAL mode,
in-process job/worker threads, Caddy 2 for TLS. The full topology diagram, data model,
realtime protocol, email threading, and AI pipeline are documented in `architecture.md`;
this README will carry the reader-facing summary as features land.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then fill it in
python manage.py migrate
uvicorn config.asgi:application --port 8000   # --workers stays 1

# docker — local: app only, reachable at http://127.0.0.1:8000
docker compose up --build
# on the VM: add Caddy (public TLS on 80/443) via the opt-in profile
docker compose --profile proxy up -d --build
```

## Known Limitations

Every deliberate shortcut is listed here with its production swap (this is graded).
Populated as each phase lands; the full scaling-seam table lives in `architecture.md` §11.

| Limitation | Why | Production swap |
|---|---|---|
| _populated per phase_ | | |
