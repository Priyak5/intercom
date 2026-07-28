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

## Email channel

The support mailbox is a single IMAP account (Gmail / Zoho / any provider that
supports plus-addressing and app passwords). Inbound messages are polled every
30 s ([`apps/mail/poller.py`](apps/mail/poller.py)); outbound replies go direct
over SMTP with explicit `Message-ID`, `In-Reply-To`, and `References` headers
so threads render correctly in the customer's mail client
([`apps/mail/send.py`](apps/mail/send.py)).

**Threading resolution** ([`apps/mail/threading.py`](apps/mail/threading.py)) —
tried in order:

1. `In-Reply-To` / `References` → existing `Message.email_message_id`.
2. Plus-address token `support+c<conv_hex>.<hmac8>@<MAIL_DOMAIN>` in the To/Cc
   headers. The 8-hex tag is `HMAC(workspace.hmac_secret, conv_id)[:8]`, so
   nobody outside the workspace can forge routing.
3. Same sender + same normalised subject (strips `Re:`/`Fwd:`) within 7 days.
4. Otherwise a fresh Conversation.

**Deliverability.** For a $0 POC we rely on the mailbox provider's outbound
reputation (Gmail app-passwords + Zoho catch-alls are typical). Production
should set up **SPF** (`v=spf1 include:_spf.google.com ~all`) plus **DKIM** at
the provider console, and **DMARC** (`v=DMARC1; p=quarantine; rua=mailto:...`)
in DNS, then swap `smtplib` for a transactional provider (Postmark / SES) —
that swap only touches [`apps/mail/send.py`](apps/mail/send.py).

## Known Limitations

Every deliberate shortcut is listed here with its production swap (this is graded).
Populated as each phase lands; the full scaling-seam table lives in `architecture.md` §11.

| Limitation | Why | Production swap |
|---|---|---|
| Invite emails print to the console (Django `EMAIL_BACKEND`) | Support-reply SMTP is separate (uses `smtplib` directly with `SMTP_*` env); keeping the Django backend on console avoids two credential sets for the demo | Set `EMAIL_BACKEND` to the SMTP backend to deliver invites, or reuse `SMTP_*` via `django.core.mail.backends.smtp.EmailBackend` |
| Email channel handles **one workspace per deploy** (`MAIL_WORKSPACE_ID`) | Single shared support mailbox; every un-threaded inbound routes to one workspace | Per-workspace catch-all address (e.g. `support-{slug}@domain`) or a mailbox per workspace, decoded in the poller |
| Attachments in inbound email are stripped | Storage, download UX, virus scanning are non-trivial for POC | Save payloads to `data/attachments/<msg_id>/`, new `AttachmentMeta` model, per-conversation download endpoint |
| SMTP send is best-effort, no inline retry | `delivery_state=FAILED` is the visible signal; agent can resend manually | Wrap in a Job worker with exponential backoff (Job model arrives with Phase 7 AI) |
| IMAP polls every 30s (no IDLE) | Simpler; latency is fine for a POC | `imapclient` supports IDLE — wire a supervisor loop that falls back to polling on IDLE drops |
| Plus-address round-trip requires provider `+` sub-addressing | Gmail/Zoho support it; some corporate servers strip `+` | `In-Reply-To`/`References` still threads via Path 1; corporate deployments should use a per-workspace catch-all instead |
| Inbox free-text search covers subject + contact name/email only, **not message bodies** | Message-body search wants SQLite FTS5, which lands with the KB in Phase 6 | Add an FTS5 virtual table mirroring `inbox_message.body_text`, sync from `post_message`, `UNION` its hits into the `?q=` filter |
| `conversation.updated` triggers a full list refetch on every connected agent | Simple + always correct with active filters; POC scale (dozens of conversations) makes the extra fetches trivial | In-place patch from the envelope, or a materialised list view keyed by (workspace, status, channel, assignee) |
| Snooze wake-up granularity is 60 s | Sweeper runs every 60 s; a conversation may reopen up to a minute after its `snoozed_until` | Reduce the interval, or wake on the exact time via a scheduled task queue |
| Invite token is stored raw and travels in the accept URL | POC simplicity; token is single-use + expires (default 7 days) | Hash the token at rest; keep the TTL/single-use guarantees |
| Rate limiting not yet applied to auth endpoints | Deferred to Phase 10 | In-memory token bucket per IP (per `CLAUDE.md` §7) |
| Concurrent posts with the *same* `client_msg_id` can skip one seq value | The idempotency loser burns a seq before losing the unique race | Harmless — the client heals the gap via `?after_seq=` backfill; the alternative (insert-first) leaks rows |
| Sync WebSocket consumers cap concurrent in-flight WS messages at the ASGI threadpool size | Sync consumers keep the service layer un-colored and correct | Async consumers + `database_sync_to_async`, or the Redis channel layer + N replicas |
| Presence/typing are lost on restart | In-process dicts, swept by the sweeper thread | Redis `SETEX` keys (architecture §11) — everything else is in the DB and backfilled |
| `message.ack` rides the HTTP POST response, not a WS frame | Sends go over idempotent REST (I3); the response carries the assigned seq | n/a — documented design choice |
| Widget visitor surface lives inside `apps/inbox` (`widget_api.py`, `widget_auth.py`) rather than a separate `apps/widget/` | Keeps the app count at five (CLAUDE.md §5); the code is small and cohesive with the inbox services it calls | Split out only if the widget grows its own models/services |
| Widget endpoints have no per-IP rate limiting | Deferred to Phase 10 hardening | In-memory token bucket per IP on `/api/widget/session` and `/api/widget/messages` (POST) |
| No HMAC identity verification of the visitor on widget session-create | POC assumes anonymous or trust-on-first-use | Customer's server signs `user_hash = HMAC(hmac_secret, user_id)` in-page; server verifies before binding to a persistent Contact |
| Empty `Workspace.allowed_origins` accepts any embed origin | Dev-default so `demo/index.html` works cross-origin without config | Production must set `allowed_origins` at workspace creation; enforced in `WidgetSessionView` |
| Widget shows offline UI until the iframe is reloaded — no live online→offline transitions | POC keeps `config` a one-shot fetch on iframe load | Poll `/api/widget/config` every ~30s, or push presence changes over a lightweight WS channel |
