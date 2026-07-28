# CLAUDE.md

Operating instructions for any AI agent or engineer working in this repo.
Read this file fully before writing code. Read `architecture.md` before changing a design.
Read `plan.md` to know what phase we are in and what "done" means.

---

## 1. What this is

A single-tenant-per-workspace customer support platform (Intercom clone) built as a
time-boxed POC for a hiring assignment. Seven non-negotiable features:

1. Auth & team management (roles: admin, agent)
2. Embeddable live chat widget (real-time, typing, presence, read receipts, history)
3. Email channel (inbound parse, reply from dashboard, RFC-correct threading)
4. Unified inbox (chat + email, filter, assign, snooze, resolve)
5. Knowledge base (rich text editor, categories, public page, search)
6. AI issue summarization (LLM, updates as conversation grows)
7. Custom domains for the public KB (real SSL)

It will be graded on: system design, real-time architecture, email engineering,
AI integration, production readiness, security, frontend quality, and — explicitly —
**documented trade-off decisions**. Writing down a shortcut scores; hiding it does not.

---

## 2. Non-negotiable invariants

Violating any of these is a bug even if tests pass. Do not "improve" them without
updating `architecture.md` in the same commit.

### I1. One ASGI process. Exactly one.
`uvicorn --workers 1`. The Channels layer is `InMemoryChannelLayer` and background
work runs as threads inside this process. A second worker silently splits the channel
layer and presence state — messages stop being delivered with no error.
`config/asgi.py` must assert this at boot and crash loudly if misconfigured.

### I2. Ordering comes from a server-assigned `seq`, never from timestamps.
Every `Message` has a monotonic per-conversation `seq`, allocated by a single atomic
statement:

```sql
UPDATE inbox_conversation SET last_seq = last_seq + 1 WHERE id = ? RETURNING last_seq;
```

Never `SELECT max(seq)` then insert. Never order the UI by `created_at`.

### I3. WebSocket is a notification transport, not the source of truth.
Every WS event carries `conversation_id` and `seq`. The client detects a gap
(`incoming.seq != last_seen_seq + 1`) and reconciles via
`GET /api/conversations/{id}/messages?after_seq=`. Truth lives in the database.
If you find yourself putting state only in a WS payload, stop.

### I4. Sends are idempotent via `client_msg_id`.
The client generates a UUID per send. `unique(conversation, client_msg_id)`.
A retry or double-tap returns the existing message rather than creating a second one.
This is what makes optimistic UI safe.

### I5. All business logic lives in `services.py`.
HTTP views and WS consumers are thin adapters that both call the same service function.
`inbox.services.post_message(...)` is called from the REST endpoint, the widget
endpoint, the WS consumer, and the email poller. No logic in `Model.save()`,
no logic in serializers, no logic duplicated across a view and a consumer.

### I6. Tenancy is derived, never accepted from the client.
`request.workspace` is set by middleware from (a) the session's selected workspace for
dashboard requests, (b) the widget `public_key` + signed visitor token for widget
requests, or (c) the `Host` header for public KB requests. A `workspace_id` in a
request body or query param is always ignored. Every queryset touching tenant data
filters on `workspace`. There is a test for this (`tests/test_tenancy.py`) — keep it green.

### I7. Untrusted HTML is sanitized on write, not on read.
KB articles come from a WYSIWYG editor. `bleach.clean()` with an explicit allowlist
runs in the service layer before persisting. Templates render KB bodies with `|safe`
only because sanitization already happened. Never `|safe` anything else.

### I8. AI never blocks a request and never hangs the UI.
Summarization is a queued `Job`. Hard 8s timeout, one retry, then a deterministic
non-LLM fallback (first message + last three) returned with `degraded: true`.
The UI must always render something.

### I9. Every background thread manages its own DB connections.
At the top of each loop iteration: `django.db.close_old_connections()`.
SQLite + long-lived threads without this leaks connections and eventually locks.

### I10. Secrets come from the environment. Always.
No key, token, password, or hostname in the repo. `.env.example` lists every variable
with a safe placeholder. `settings.py` reads them with explicit defaults only for
non-secret values.

---

## 3. Stack — and what is deliberately absent

| Layer | Choice |
|---|---|
| Framework | Django 5, Django REST Framework |
| Realtime | Channels 4 + `InMemoryChannelLayer`, served by Uvicorn (ASGI) |
| Database | SQLite in WAL mode; FTS5 for KB search |
| Jobs | `core.Job` table + one poller thread in-process |
| Email | IMAP poll (inbound) + SMTP (outbound), any free mailbox |
| AI | Anthropic Claude Haiku via `anthropic` SDK |
| Frontend | Django templates + vanilla JS. **No build step, no npm, no bundler.** |
| Static | WhiteNoise, served from the single ASGI process (justified in `architecture.md` §2) |
| Editor | Quill via CDN |
| Proxy / TLS | Caddy 2 with on-demand TLS |
| Packaging | Docker; two containers (`app`, `caddy`) |

**Absent on purpose:** Redis, Celery, Postgres, React, Webpack/Vite, Elasticsearch,
Postmark/SendGrid, Kubernetes. Each was removed because it exists to coordinate
multiple processes or requires a paid tier, and we have one process and a $0 budget.
`architecture.md` §11 names the exact swap for each if this ever needs to scale.

Do not add a dependency without adding a line to `requirements.txt` **and** a
justification in `architecture.md`. Default answer to "should we add X" is no.

---

## 4. Commands

```bash
# local dev
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                 # then fill it in
python manage.py migrate
python manage.py createsuperuser
uvicorn config.asgi:application --reload --port 8000   # NOTE: --workers stays 1

# tests
pytest -q
pytest tests/test_ordering.py -v

# docker
docker compose up --build            # app + caddy
docker compose logs -f app
docker compose exec app python manage.py migrate

# transfer image
docker build -t support-poc:v1 .
docker save support-poc:v1 | gzip > support-poc-v1.tar.gz
docker load < support-poc-v1.tar.gz
```

Background threads are gated by `RUN_BACKGROUND_THREADS=1` so tests and one-off
management commands don't start the worker or the IMAP poller.

---

## 5. Repo map

```
config/          settings.py (one file, env-driven), urls.py, asgi.py (thread boot + assert)
apps/core/       BaseModel, tenant middleware, Job model, worker.py, health.py
apps/accounts/   User, Workspace, Membership, Invite, Domain, auth views, permissions
apps/inbox/      Contact, Conversation, Message, services.py, consumers.py, ai.py, prompts.py,
                 widget_api.py (visitor endpoints), widget_auth.py (signed tokens)
apps/mail/       poller.py (IMAP thread), threading.py (resolution), send.py (SMTP)
apps/kb/         Category, Article, FTS5 sync, admin views, public views, search.py
templates/       base.html, auth/, inbox/ (incl. widget_frame.html), kb_admin/, public_kb/
static/          css/app.css, js/socket.js, js/inbox.js, widget/loader.js, widget/frame.css
demo/            index.html — standalone page with widget installed (graded checklist item)
data/            volume: db.sqlite3, caddy certs. Never committed.
tests/           test_ordering.py, test_threading.py, test_tenancy.py, test_idempotency.py
```

Realtime, AI, and the visitor widget surface are **files inside `inbox`**, not separate
apps. Five apps total (core, accounts, inbox, mail, kb). Do not add another without a
reason written in `architecture.md`.

---

## 6. Code conventions

- Python: 4 spaces, type hints on service functions and anything non-obvious.
  `ruff` defaults if you want a linter; don't add more tooling than that.
- Models: UUID primary keys via `core.BaseModel`. Explicit `related_name` everywhere.
  Choice fields use module-level `TextChoices` classes, never bare strings inline.
- Services: `def post_message(*, conversation, sender, body, client_msg_id) -> Message`.
  Keyword-only args. Return domain objects, not dicts. Raise typed exceptions from
  `core/exceptions.py`; DRF handlers map them to status codes.
- Views: thin. Validate with a serializer, call a service, serialize the result.
- Queries: never `LIMIT/OFFSET` for conversation lists — keyset paginate on
  `(last_message_at, id)`. Any list endpoint touching messages or conversations must
  have an index backing its filter; check with `EXPLAIN QUERY PLAN`.
- JS: no framework, no bundler. `socket.js` owns reconnect/gap logic and is the only
  place that touches the WebSocket. Other modules subscribe to its events.
- Logging: `structlog`-style key=value via stdlib `logging`. Every inbound email,
  every AI call, every job transition logs one line with ids. This is the debuggability
  requirement — an unlogged failure path is an incomplete feature.
- Migrations: one per logical change, committed with the code. Never edit an applied
  migration.

---

## 7. Security checklist (graded)

- Session cookies: `HttpOnly`, `Secure`, `SameSite=Lax`. CSRF enabled on all
  dashboard POSTs. Widget endpoints are CSRF-exempt but token-authenticated.
- Widget auth: workspace `public_key` (public, safe to embed) + short-lived signed
  visitor session token. Optional HMAC identity verification using `hmac_secret`
  (server-side only, never shipped to the browser).
- Widget origin allowlist: `WidgetSettings.allowed_origins` checked on session create.
- Widget renders in an **iframe** so host-page CSS and JS cannot reach it, and vice versa.
- KB HTML sanitized with `bleach` on write (I7).
- Tenant isolation test must cover: conversation read, message post, article read,
  member list, and domain create.
- Rate limits on the three public surfaces: widget session create, widget message post,
  KB search. Simple in-memory token bucket keyed by IP is sufficient; say so in the README.
- `/api/internal/domain-allowed` must only be reachable from the Caddy container
  (not exposed through the proxy) and returns 200 only for a verified `Domain` row.
- No secrets in logs. Redact `Authorization`, tokens, and raw MIME on error paths.

---

## 8. Pitfalls specific to this design

| Symptom | Cause |
|---|---|
| Messages silently not delivered to some clients | More than one uvicorn worker (I1) |
| `database is locked` | Missing WAL / `busy_timeout`, or a thread holding a write txn |
| Connections climb until failure | Thread loop missing `close_old_connections()` (I9) |
| Duplicate messages on flaky networks | `client_msg_id` not sent or unique constraint missing (I4) |
| Messages appear out of order after reconnect | Client ordering by timestamp instead of `seq` (I2) |
| Email replies start new conversations | `References` chain not preserved on outbound send |
| Same email processed twice after restart | IMAP UID cursor not persisted before ack |
| Presence stuck "online" forever | Sweeper thread not expiring entries at 45s |
| Custom domain cert fails | `domain-allowed` returned non-200, or CNAME not propagated |

SQLite pragmas to set on every connection: `journal_mode=WAL`,
`busy_timeout=5000`, `synchronous=NORMAL`, `foreign_keys=ON`.

---

## 9. Definition of done, per feature

A feature is done when all five hold:

1. Works end-to-end through the UI, not just the API.
2. Survives a page refresh and a process restart (state is in the DB).
3. Has a logged, non-crashing failure path.
4. Is enforced for tenancy and permissions.
5. Is listed in `README.md` — including anything stubbed and why.

Do not move to the next phase in `plan.md` until the current phase's acceptance
criteria are checked off.

---

## 10. Working style for agents

- Before coding, restate which phase and which acceptance criterion you are satisfying.
- Prefer editing an existing file over creating a new one. Prefer deleting over adding.
- If a requirement seems to need Redis/Celery/Postgres, you have probably violated I5
  or I1 — re-read the relevant `architecture.md` section before adding infrastructure.
- When you take a shortcut, add it to the "Known Limitations" table in `README.md`
  in the same commit, with the production alternative named. This is graded.
- Never leave a spinner with no timeout, a bare `except:`, or a `TODO` without a
  matching line in `plan.md`.
- Commit granularly with meaningful messages. The graders read the commit history and
  have said one giant commit is an automatic fail.
  