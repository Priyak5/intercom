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
| Invite emails print to the console, not real inboxes | SMTP lands in Phase 4 | Set `EMAIL_BACKEND` to the SMTP backend (the invite link is also shown in the UI meanwhile) |
| Invite token is stored raw and travels in the accept URL | POC simplicity; token is single-use + expires (default 7 days) | Hash the token at rest; keep the TTL/single-use guarantees |
| Rate limiting not yet applied to auth endpoints | Deferred to Phase 10 | In-memory token bucket per IP (per `CLAUDE.md` §7) |
| Concurrent posts with the *same* `client_msg_id` can skip one seq value | The idempotency loser burns a seq before losing the unique race | Harmless — the client heals the gap via `?after_seq=` backfill; the alternative (insert-first) leaks rows |
| Sync WebSocket consumers cap concurrent in-flight WS messages at the ASGI threadpool size | Sync consumers keep the service layer un-colored and correct | Async consumers + `database_sync_to_async`, or the Redis channel layer + N replicas |
| Presence/typing are lost on restart | In-process dicts, swept by the sweeper thread | Redis `SETEX` keys (architecture §11) — everything else is in the DB and backfilled |
| `message.ack` rides the HTTP POST response, not a WS frame | Sends go over idempotent REST (I3); the response carries the assigned seq | n/a — documented design choice |
| Minimal visitor surface lives inside `apps/inbox` (no iframe/loader/origin-allowlist/rate-limits) | Phase 2 needs a testable visitor; the real widget is Phase 3 | Phase 3 promotes it to `apps/widget` |
