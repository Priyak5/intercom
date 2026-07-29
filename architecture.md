# architecture.md

Technical architecture for the support platform POC. Companion to `CLAUDE.md`
(rules) and `plan.md` (sequence). This file explains **why**, and is the document to
update whenever a design decision changes.

---

## 1. Design constraints

| Constraint | Consequence |
|---|---|
| POC, ~10 days, one engineer | Minimal moving parts; every component must earn its place |
| Cost ≈ $0 | No managed services with paid tiers; free-tier VM only |
| Must be Dockerized, images transferable | Self-contained image, `docker save`-able, volume for state |
| Graded on real-time architecture | Ordering, reconnect, and idempotency handled explicitly, not incidentally |
| Graded on trade-off documentation | Every shortcut named, with its production alternative |

The dominant decision follows from cost + minimalism: **one process**. Redis, Celery,
and a managed Postgres all exist to coordinate work across process boundaries. With a
single process, an in-memory channel layer and an in-process job queue are correct, not
lazy — and they remove three services, three failure modes, and all recurring cost.

The price is no horizontal scaling. §11 names the exact swap for each component.

---

## 2. Topology

```
                        Internet
                            │
                    ┌───────▼────────┐
                    │  Caddy 2       │  :80 / :443
                    │  auto-TLS      │  on_demand_tls { ask app:8000/... }
                    └───────┬────────┘
                            │ reverse_proxy app:8000 (HTTP + WS upgrade)
        ┌───────────────────▼─────────────────────────────────┐
        │  app container — ONE uvicorn process, 1 worker      │
        │                                                     │
        │   ASGI router                                       │
        │    ├── http → Django (DRF views, templates)         │
        │    └── ws   → Channels consumers                    │
        │                                                     │
        │   InMemoryChannelLayer  (groups: ws:{id}, conv:{id}) │
        │   Presence + typing dicts (45s / 3s expiry)          │
        │                                                     │
        │   Threads (gated by RUN_BACKGROUND_THREADS=1):       │
        │    ├── job worker   — polls core.Job every 1s        │
        │    ├── imap poller  — IDLE or 30s poll               │
        │    └── sweeper      — presence expiry, snooze expiry, │
        │                       nightly VACUUM INTO backup      │
        └────────────────┬────────────────────────────────────┘
                         │
              ┌──────────▼───────────┐        ┌──────────────────┐
              │ SQLite (WAL) + FTS5  │        │ External (egress) │
              │ /app/data/db.sqlite3 │        │ IMAP, SMTP,       │
              │  ← docker volume     │        │ Anthropic API     │
              └──────────────────────┘        └──────────────────┘
```

Two containers total: `app` (built here) and `caddy` (official image).
All mutable state lives in the `./data` volume: the SQLite database and Caddy's
certificate store.

Static assets are served from the `app` process by **WhiteNoise** (added in Phase 0,
the one dependency beyond the original stack list). With a single container, no nginx,
and static files not shared into the `caddy` container, WhiteNoise serves `/static/`
compressed and cache-headered directly from the ASGI app. It adds no service and no
recurring cost; Caddy still terminates TLS and proxies everything.

### Why threads and not a worker container

`InMemoryChannelLayer` can only broadcast to WebSocket connections held by its own
process. A separate worker container that finished a summarization job would have no
way to push `summary.ready` to a connected agent — it would need Redis as a broker,
which is the dependency we removed. Threads inside the ASGI process keep
`channel_layer.group_send` usable from background work. This is the load-bearing
decision of the whole design; it is why `--workers 1` is an invariant and not a default.

---

## 3. Application decomposition

Six Django apps. Boundaries chosen so that any given bug has exactly one plausible home.

| App | Responsibility | Key files |
|---|---|---|
| `core` | `BaseModel`, tenant middleware, `Job` queue + worker, health, exceptions | `models.py`, `middleware.py`, `worker.py`, `health.py` |
| `accounts` | Identity and tenancy: User, Workspace, Membership, Invite, Domain | `models.py`, `views.py`, `permissions.py`, `domains.py` |
| `inbox` | The core domain: Contact, Conversation, Message, realtime, AI | `services.py`, `consumers.py`, `ai.py`, `prompts.py` |
| `widget` | Public visitor surface: session tokens, config, embed | `views.py`, `auth.py` |
| `mail` | IMAP ingest, threading resolution, SMTP send | `poller.py`, `threading.py`, `send.py` |
| `kb` | Categories, Articles, FTS5 search, public pages, widget suggest | `models.py`, `search.py`, `public_views.py` |

Realtime and AI are files inside `inbox`, not separate apps: they operate on the same
aggregate (`Conversation`) and splitting them would only add import indirection.

### The service layer rule

`inbox/services.py` contains every write path for the core domain. `post_message` is
called by four different callers — the dashboard REST endpoint, the widget endpoint,
the WebSocket consumer, and the IMAP poller. Each caller supplies a different
`sender_type`; the sequencing, persistence, broadcast, and side-effect logic exists
once. This is the single most important structural choice for debuggability: when a
message is wrong, there is one function to read.

```python
def post_message(*, conversation, sender_type, body_text,
                 client_msg_id, sender_user=None, email_meta=None) -> Message:
    # 1. idempotency check on (conversation, client_msg_id)
    # 2. atomic seq allocation
    # 3. INSERT message
    # 4. update conversation denorms (last_message_at, first_response_at)
    # 5. broadcast message.created to conv:{id} and ws:{workspace}
    # 6. enqueue side-effect jobs (outbound email, summary refresh)
```

---

## 4. Data model

Nine tables. `BaseModel` supplies `id` (UUID), `created_at`, `updated_at`.
Every tenant-owned row carries `workspace_id`.

### accounts

```
User          email(unique) name password is_active         # USERNAME_FIELD = email
Workspace     name slug(unique) public_key hmac_secret
Membership    user→ workspace→ role[admin|agent]            # unique(user, workspace)
Invite        workspace→ email role token expires_at accepted_at
Domain        workspace→ hostname(unique) verify_token
              verified_at ssl_status[pending|active|failed]
```

`public_key` is embedded in customer web pages and is safe to expose.
`hmac_secret` never leaves the server; it signs visitor session tokens and backs
optional identity verification.

### inbox

```
Contact       workspace→ email name last_seen_at current_page
              # unique(workspace, email)

Conversation  workspace→ contact→ channel[chat|email]
              status[open|snoozed|resolved] assignee→User subject
              last_seq int default 0                 ← ordering counter
              agent_last_read_seq int
              contact_last_read_seq int
              snoozed_until last_message_at
              first_response_at resolved_at          ← SLA metrics, free
              summary text summary_upto_seq int
              # index (workspace, status, -last_message_at)
              # index (workspace, assignee, -last_message_at)

Message       conversation→ seq int
              sender_type[contact|agent|system] sender_user→User
              body_text body_html client_msg_id uuid
              email_message_id email_in_reply_to     ← threading, no extra table
              delivery_state[queued|sent|failed]
              raw_mime text null                     ← replay/debug for email
              # unique(conversation, seq)
              # unique(conversation, client_msg_id)
              # index (conversation, seq)

CannedResponse workspace→ shortcut title body        # stretch
```

Deliberate collapses versus a "proper" schema, each with the reason:

- **No `ReadState` table** → two integer columns. Only two reader classes exist
  (the assigned agent and the contact); a table would buy nothing.
- **No `InboundEmail` / `OutboundEmail` tables** → three columns on `Message` plus
  `raw_mime`. Threading needs `Message-ID` lookup, which an index on
  `email_message_id` gives directly.
- **No `ConversationEvent` table** → audit entries are `Message` rows with
  `sender_type=system`. They render inline in the thread, which is what the UI wants
  anyway.
- **No `AIRun` table** → token counts, latency, model, and cost live on
  `Job.payload`. Still queryable for a spend total, which is what the AI-integration
  criterion asks for.

### kb

```
Category      workspace→ name slug position          # unique(workspace, slug)
Article       workspace→ category→ title slug body_html
              status[draft|published] published_at view_count
              # unique(workspace, slug)
ArticleFTS    (FTS5 virtual table: rowid, title, body_text, workspace_id)
```

### core

```
Job           kind payload(json) status[pending|running|done|failed]
              attempts run_at locked_at error
              # index (status, run_at)
```

### Sequence allocation

The one piece of concurrency-critical SQL in the system:

```sql
UPDATE inbox_conversation
   SET last_seq = last_seq + 1
 WHERE id = ?
RETURNING last_seq;
```

A single statement, so it is atomic without `SELECT FOR UPDATE` and without a
transaction spanning application logic. Never read-then-write. `unique(conversation, seq)`
turns any future mistake into a loud `IntegrityError` rather than silent reordering.

### SQLite configuration

Set on every connection:

```
journal_mode = WAL        -- concurrent readers alongside one writer
busy_timeout = 5000       -- wait rather than fail on lock contention
synchronous  = NORMAL     -- durable enough with WAL, much faster
foreign_keys = ON
```

WAL is what makes SQLite viable here: readers never block on the writer, and the
writer is always the single service layer.

---

## 5. Realtime protocol

Three rules make the difference between a demo that looks real-time and one that is
correct. They cost almost nothing and directly answer the graded criterion
"connection state, reconnection logic, message ordering guarantees".

### R1. The socket notifies; the database is the truth.

Every event carries `conversation_id` and `seq`. Nothing exists only in a WS payload.
A client that missed events recovers by asking the database, not by asking the socket
to replay.

### R2. Ordering is by server-assigned `seq`, and gaps are detected.

```
event in → if event.seq === lastSeen + 1 : apply, lastSeen++
        → if event.seq <= lastSeen        : ignore (duplicate)
        → if event.seq >  lastSeen + 1    : buffer, schedule backfill in 500ms
backfill  → GET /messages?after_seq=lastSeen → apply in order → drain buffer
```

Timestamps are never used for ordering. Clock skew between the browser, the container,
and the mail server makes them unusable for this.

### R3. Writes are idempotent on `client_msg_id`.

The client mints a UUID per send and renders it optimistically. The server either
inserts it or returns the existing row. `message.ack` carries the assigned `seq` so the
client swaps its optimistic entry for the canonical one. Retries after a timeout are
therefore always safe — which is what makes aggressive reconnection acceptable.

### Channels and groups

```
/ws/agent/?token=<session>     → groups: ws:{workspace_id}, conv:{id} (on open)
/ws/widget/?session=<token>    → group:  conv:{id}
```

`ws:{workspace_id}` drives inbox-list updates (new conversations, unread counts,
assignment changes). `conv:{id}` drives the open thread. An agent switching
conversations leaves the old `conv:` group and joins the new one.

### Event envelope

```json
{ "type": "message.created",
  "conversation_id": "…",
  "seq": 42,
  "data": { … } }
```

Types: `message.created`, `message.ack`, `typing`, `read.updated`,
`conversation.updated`, `presence.updated`, `summary.ready`.

### Connection lifecycle

- **Auth on connect**: session cookie for agents, signed visitor token for widget.
  Reject in `connect()` before accepting, never after.
- **Heartbeat**: client ping every 20s; server refreshes the presence key (45s TTL).
- **Reconnect**: exponential backoff 1s → 30s with full jitter, capped, infinite retries.
  On reconnect: re-join groups, then immediately backfill `?after_seq=`.
- **Offline UI**: a banner after two failed attempts; the composer stays enabled and
  queues sends, which are flushed on reconnect (safe because of R3).

### Typing and presence

In-process dictionaries: `{workspace_id: {actor_id: expires_at}}`. Typing entries
expire at 3s, presence at 45s, both swept by the sweeper thread every 5s.
Ephemeral by design — lost on restart, and nothing depends on them being durable.

---

## 6. Email engineering

### Why IMAP/SMTP instead of a webhook provider

An inbound-parse provider (Postmark, SendGrid) would give lower latency, but requires
domain verification, DKIM setup, signature verification code, and — past the trial —
money. IMAP polling needs a mailbox and nothing else, behaves identically on a laptop
and in production, and preserves the headers threading depends on. The trade is ~30s
inbound latency, which is documented.

### Outbound provider selection: Brevo → Resend → SMTP

`apps/mail/send.py::send_reply` picks the first configured provider, in this order:

1. **Brevo (HTTPS API)** — preferred. Brevo's *single-sender verification* lets us
   authorise a free mailbox (Zoho, Gmail, Yandex — anything) by clicking a link
   Brevo emails to that address. No domain ownership, no DKIM setup, no dashboard
   dance. The demo runs on `priyademo@zohomail.in`. Free tier is 300/day —
   plenty for a POC.
2. **Resend (HTTPS API)** — used when the deploy owns a domain and wants production
   deliverability. Requires SPF/DKIM/DMARC verification of the sending domain.
3. **SMTP (`smtplib`)** — fallback for local dev only. Railway and most PaaS block
   outbound 25/465/587, so this deadlocks against a shipping host; kept because it
   works on a laptop with any Zoho/Gmail app password.

The three paths share the same threading-header logic (Message-ID / In-Reply-To /
References computed once in `send_reply`, then handed to whichever transport). Brevo
rewrites Message-ID on send — so our threading path 1 (matching on our own
Message-ID) becomes best-effort with Brevo; paths 2 (plus-address Reply-To) and 3
(sender + subject) still work reliably. Switching to Resend on a verified domain
restores full Message-ID control with zero code change.

### Inbound pipeline

```
imap poller thread
  → fetch UIDs > cursor
  → persist cursor BEFORE processing        (restart-safe: at-least-once, never lost)
  → parse MIME (email.parser)
  → resolve conversation (see below)
  → strip quoted text (email-reply-parser)
  → services.post_message(sender_type=contact, email_meta=…, raw_mime=…)
       → broadcasts message.created over WS
```

At-least-once delivery is made safe by the `Message-ID` uniqueness check: a
reprocessed email resolves to an existing `email_message_id` and is skipped.

### Threading resolution, in order

1. **`In-Reply-To` / `References`** — walk the chain, match against
   `Message.email_message_id`. Standards-compliant and handles reply-to-reply.
2. **Plus-addressed reply-to** — outbound sets
   `Reply-To: support+c{conv_short}.{hmac8}@domain`. Survives clients that strip
   `References`. The HMAC prevents conversation-id guessing.
3. **Heuristic** — same sender address + same normalised subject
   (`Re:`/`Fwd:` stripped) within 7 days.
4. **New conversation** — with `channel=email` and the subject preserved.

### Outbound

```
Message-ID:  <c{conversation_short}.m{seq}@{MAIL_DOMAIN}>   ← deterministic, our own
In-Reply-To: <last inbound message-id>
References:  <full chain, appended>
Reply-To:    support+c{conv}.{hmac8}@{MAIL_DOMAIN}
```

Sent as multipart/alternative (text + HTML). Generating our own `Message-ID` rather
than letting the SMTP server assign one is what makes path 1 above work in both
directions — we can always recognise a reply to something we sent.

Deliverability: SPF, DKIM, and DMARC configured on the sending domain, documented in
the README. Gmail's 500/day SMTP cap is far above POC needs.

---

## 7. Knowledge base and search

Articles are authored in Quill (loaded from CDN, no build step) and stored as HTML.
**Sanitization happens on write**, in the service layer, with an explicit `bleach`
allowlist of tags and attributes. Nothing renders untrusted HTML at read time except
KB bodies, which are already clean. This ordering matters: a WYSIWYG that persists raw
HTML is the most obvious XSS surface in the product and the first thing a reviewer
will probe.

Search uses SQLite **FTS5**, kept in sync from the service layer on create/update/delete,
ranked with `bm25()`. This replaces Elasticsearch or Postgres `tsvector` at zero
infrastructure cost, with the limitation of no fuzzy matching (documented; the swap is
Postgres FTS + `pg_trgm`).

Two consumers:
- **Public KB** — server-rendered pages, tenant resolved from the `Host` header, only
  `published` articles.
- **Widget auto-suggest** — `/api/widget/kb/suggest?q=`, called debounced at 400ms as
  the visitor types, returning the top three. This satisfies requirement 5's
  auto-suggest clause and is the highest-value integration point between two features.

---

## 8. AI integration

### Pipeline

```
agent opens conversation
  → if last_seq - summary_upto_seq >= 5 : enqueue Job(kind="summarize", conv_id)
  → serve the existing summary immediately (stale-while-revalidate)

job worker thread
  → build context: first message + last 40 messages, truncated to ~8K tokens
                   (oldest dropped first; the first message is always kept because it
                    contains the original ask)
  → Claude Haiku, strict JSON response, 8s timeout
  → on success : persist summary + summary_upto_seq, group_send summary.ready
  → on failure : one retry, then deterministic fallback, degraded=true
  → always     : record model, input/output tokens, latency, cost on Job.payload
```

### Prompt design

Output is constrained to a fixed JSON schema so the UI can render fields rather than a
paragraph:

```json
{ "what_they_want": "…",
  "whats_been_tried": "…",
  "current_status": "…",
  "key_details": ["…"] }
```

The schema mirrors the requirement's wording ("what the user wants, what's been tried,
current status"). Parsing failures are treated as call failures and fall through to the
retry/fallback path — never rendered raw.

### Context windowing and cost

Truncation is by token budget, not message count, so a conversation of long emails and
a conversation of short chats both fit. Only messages are sent; no PII beyond what the
conversation contains. At Haiku pricing a summary of a 40-message conversation costs
well under a tenth of a cent, and the total spend across development and demo is
tracked from `Job.payload` — which is the concrete answer to the "cost awareness"
criterion.

### Degradation

The fallback summary is first message + last three messages, labelled as non-AI. A
missing or rate-limited API key produces a usable panel, a logged error with the job
id, and no spinner. There is no code path where the summary panel can hang.

---

## 9. Multi-tenancy and security

**Tenancy is always derived** (`CLAUDE.md` I6):

| Surface | Source of `request.workspace` |
|---|---|
| Dashboard | Session → selected `Membership` |
| Widget | `public_key` + signed visitor token |
| Public KB | `Host` header → `Domain` → `Workspace` |
| Internal | Not tenant-scoped; not exposed via the proxy |

A `workspace_id` supplied by a client is ignored everywhere.
`tests/test_tenancy.py` asserts cross-workspace denial for conversation read, message
post, article read, member list, and domain create.

**Widget isolation.** `loader.js` (~2KB) injects an iframe rather than rendering into
the host DOM. Host-page CSS cannot break the widget, host JS cannot read it, and the
widget cannot read the host page. The cost is `postMessage` for resize events, which is
a few lines.

**Other controls**: `HttpOnly`/`Secure`/`SameSite=Lax` session cookies; CSRF on all
dashboard mutations; widget endpoints CSRF-exempt but token-authenticated with an
origin allowlist; `bleach` on KB write; in-memory token-bucket rate limits on the three
public surfaces (widget session create, widget message post, KB search); secrets
redacted from logs; `DEBUG=False` and correct `ALLOWED_HOSTS`/`CSRF_TRUSTED_ORIGINS`
in production.

---

## 10. Deployment

### Deploy target: Railway (currently live) with the VM setup as an alternative

The live URL is served from Railway from the same `Dockerfile` below. On Railway
the `caddy` compose service is unused because Railway's edge terminates TLS
itself. Persistent SQLite lives on a Railway-managed volume mounted at
`/app/data`. Custom domains (§9) register via Railway's GraphQL API
(`customDomainCreate`) rather than Caddy's `on_demand_tls { ask ... }` — same
`Domain` table, same DNS-verification middleware, different last-mile cert
mechanism (see `README.md` **Custom domains** for the Railway-specific wiring).

The Caddy path below still works and is the recommended shape for a self-hosted
VM (Oracle Cloud Always Free, Hetzner, Fly, bare-metal). Everything downstream
of the reverse proxy is identical between the two variants; only the front-door
TLS+DNS layer differs.

### Dockerfile

```dockerfile
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN python manage.py collectstatic --noinput
EXPOSE 8000
CMD ["sh","-c","python manage.py migrate --noinput && \
     exec uvicorn config.asgi:application --host 0.0.0.0 --port 8000 --workers 1"]
```

`--workers 1` is mandatory (I1). `config/asgi.py` asserts it at boot and refuses to
start if `RUN_BACKGROUND_THREADS=1` with a worker count above one.

### docker-compose.yml

```yaml
services:
  app:
    build: .
    env_file: .env
    volumes: ["./data:/app/data"]
    restart: unless-stopped
    healthcheck:
      test: ["CMD","python","-c","import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')"]
      interval: 30s
  caddy:
    image: caddy:2-alpine
    ports: ["80:80","443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - ./data/caddy:/data
    restart: unless-stopped
```

### Caddyfile — this is the entire custom-domain feature

```
{
  on_demand_tls {
    ask http://app:8000/api/internal/domain-allowed
  }
}

app.{$BASE_DOMAIN} {
  reverse_proxy app:8000
}

:443 {
  tls { on_demand }
  reverse_proxy app:8000
}
```

Flow: customer sets `CNAME help.theirdomain.com → app.<ourdomain>`; we verify with
`dnspython` and set `verified_at`; on the first HTTPS request Caddy asks
`/api/internal/domain-allowed?domain=help.theirdomain.com`, gets 200, and obtains a
real Let's Encrypt certificate automatically. No ACME code, no stub, ~20 lines total.
The `ask` endpoint returning 200 only for verified rows is what prevents anyone
pointing a hostname at us and consuming rate-limited certificate issuance.

### Image transfer

```bash
docker build -t support-poc:v1 .
docker save support-poc:v1 | gzip > support-poc-v1.tar.gz    # ~250 MB
# target machine
docker load < support-poc-v1.tar.gz
docker compose up -d
```

For ARM hosts (Oracle Always Free): `docker buildx build --platform linux/arm64`,
or build on the box.

### Hosting

Oracle Cloud Always Free ARM (4 vCPU / 24 GB, $0 indefinitely) or Hetzner CX22
(≈ €4/mo). Both give a persistent process, which WebSockets require.
Deliberately avoided: free tiers that sleep or evict containers — they terminate
WebSocket connections and discard in-memory presence mid-demo.

### Operations

- `/healthz` checks a DB write plus thread liveness; wired to the compose healthcheck.
- Logs to stdout, one structured line per inbound email, AI call, and job transition.
- Backup: sweeper thread runs `VACUUM INTO /app/data/backup-<date>.sqlite3` nightly,
  keeping seven; pulled off-box manually. Documented as manual, not automated.
- Env vars: `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`,
  `BASE_URL`, `BASE_DOMAIN`, `MAIL_DOMAIN`, `IMAP_HOST/USER/PASS`,
  `SMTP_HOST/PORT/USER/PASS`, `ANTHROPIC_API_KEY`, `RUN_BACKGROUND_THREADS`.

---

## 11. Known limitations and scaling seams

Stated plainly because "trade-off decisions" is an explicit evaluation criterion. Each
row names the swap, and each swap is a contained change because of the service-layer rule.

| Limitation | Cause | Production swap | Blast radius |
|---|---|---|---|
| Single process; no horizontal scale | `InMemoryChannelLayer`, in-process threads | `channels_redis` + Redis | `settings.py` only |
| ~few hundred concurrent WebSockets | One process, one event loop | Redis layer + N app replicas behind the proxy | Config |
| Presence/typing lost on restart | In-memory dicts | Redis `SETEX` keys | `presence.py` |
| Single writer; write contention under load | SQLite | Postgres via `DATABASE_URL` | `settings.py` + one migration pass |
| No fuzzy KB search | FTS5 | Postgres FTS + `pg_trgm` | `kb/search.py` |
| ~30s inbound email latency | IMAP polling | Provider inbound webhook | `mail/poller.py` → `mail/webhooks.py` |
| Job queue is DB polling; no fan-out or priorities | `core.Job` | Celery + Redis | `core/worker.py`, task signatures unchanged |
| Rate limits are per-process | In-memory buckets | Redis counters | `core/ratelimit.py` |
| Manual backups | No managed DB | Managed Postgres with PITR | Ops only |
| Attachments not stored | Time-boxed | Object storage (S3/R2) + `Attachment` model | New model + `mail` changes |

The pattern is intentional: every removed component has a single, named,
one-file re-entry point. The architecture is not "small because it's a POC" — it is
small in a shape that grows.

---

## 12. Requirement traceability

| # | Requirement | Where it lives | Notes |
|---|---|---|---|
| 1 | Auth & team management | `accounts` | Email/password, admin+agent roles, invites, agent assignment on `Conversation.assignee` |
| 2 | Chat bubble | `widget` + `inbox/consumers.py` + `static/widget/loader.js` | One script tag, iframe-isolated, typing/presence/read receipts, history via visitor token |
| 3 | Email channel | `mail` | IMAP in, SMTP out, four-path threading with `Message-ID`/`In-Reply-To`/`References` |
| 4 | Unified inbox | `inbox` + `templates/inbox` | One table, `channel` discriminator, filters, assign/snooze/resolve, live list updates |
| 5 | Knowledge base | `kb` | Quill editor, categories, public pages, FTS5 search, widget auto-suggest |
| 6 | AI summarization | `inbox/ai.py` + `core.Job` | Triggered on open, refreshes every 5 messages, JSON schema, timeout + fallback |
| 7 | Custom domains | `accounts/domains.py` + `Caddyfile` | DNS verification + Caddy on-demand TLS; real certificates, no stub |

Stretch features (`plan.md` phase 8) reuse existing data: SLA tracking from
`first_response_at`/`resolved_at`, contact timeline from `Contact.last_seen_at` and
conversation history, auto-reply drafts from the phase-7 AI pipeline plus KB context.
That reuse is why they are cheap, and why they are last.
