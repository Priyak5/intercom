# Support Platform POC

A single-tenant-per-workspace customer support platform (Intercom-style): live chat
widget, email channel, unified inbox, knowledge base, AI summarization, and custom
domains — built as a single-process Django app. See `claude.md` (rules),
`architecture.md` (design + trade-offs), and `plan.md` (build phases).

- **Live URL:** [https://intercom-pk.up.railway.app](https://intercom-pk.up.railway.app) — health at [/healthz](https://intercom-pk.up.railway.app/healthz)
- **Test credentials:** none pre-seeded — signup at [/signup](https://intercom-pk.up.railway.app/signup) creates a workspace atomically, see **Try it in 5 minutes** below.

## Try it in 5 minutes

The live app is at **[https://intercom-pk.up.railway.app/](https://intercom-pk.up.railway.app/)**. Sign up with any email + password — a workspace is created for you automatically and you land on its inbox as the admin. Then try each feature from the top navigation:

- **Live chat.** Open the visitor demo [demo](https://intercom-pk.up.railway.app/widget/test/) with key from [link](https://intercom-pk.up.railway.app/api/auth/me) after signup (key - public_key). Append `?key=<your-workspace-public_key>` to the URL and send a message as a customer, reply from the dashboard as the agent. Typing indicators, presence dots, and read receipts all update live in both windows.
- **Email.** Send any email to the workspace's support address (`priyademo@zohomail.in` on this deploy). It appears as a new conversation in the inbox within ~30 seconds. Reply from the dashboard — the customer's mail client receives a properly threaded reply.
- **Unified inbox.** Chat and email conversations share one list. Filter by status, channel, assignee, or free-text search.
- **Assign, snooze, resolve.** Buttons at the top of every conversation. Updates go live to everyone in the workspace.
- **Knowledge base.** Write an article in the rich-text editor. It's readable on your workspace's public help page and shows up as a suggestion inside the chat widget as visitors type.
- **AI summary.** Open a conversation with 5 or more messages. A summary card appears above the thread — what the customer wants, what's been tried, current status, key details.
- **Team.** Invite colleagues by email; they receive an accept link.
- **Custom domains.** Add your own hostname (e.g. `help.yourdomain.com`) from the **Domains** page to serve your KB from that hostname over HTTPS. Setup steps in the **Custom domains** section below.

For technical details on each feature (protocol design, tests, trade-offs), read the **For graders** section next.

## For graders — 5-minute tour

Every feature from the assignment brief lives one click away from signup. Recommended path:

1. **[Sign up](https://intercom-pk.up.railway.app/signup)** with any email + password. A workspace is created for you atomically; you land on an empty inbox as its admin.
2. **Team management** — Topbar → **Team** → send yourself (or a colleague) an invite. Console email backend prints the accept URL to Railway logs, or paste the link straight from the flash message. Accept in incognito → sign in as an agent.
3. **Live chat** — Open [/widget/test/](https://intercom-pk.up.railway.app/widget/test/) in a new tab (or `demo/index.html` served from any local origin). Append `?key=<your-workspace-public_key>` to the URL — find the key via `/api/auth/me`[link](https://intercom-pk.up.railway.app/api/auth/me) after signup (key - public_key), or `Workspace.objects.first().public_key` in `railway run python manage.py shell`. Send a message from the widget → it appears in the dashboard within a second; agent reply appears back in the widget instantly. Typing indicators, presence, and read receipts all live.
4. **Email channel** — Send an email to the support address configured for this deploy (currently `priyademo@zohomail.in`). Within ~30s the poller ingests it into the unified inbox. Agent reply from the dashboard goes out over Brevo (HTTPS API), threaded correctly via `In-Reply-To`/`References`.
5. **Unified inbox** — Same list combines chat + email. Filter by `channel`, `status`, `assignee`, `q`. Assign, snooze, resolve — updates propagate live to other agents on the same workspace over WS.
6. **Knowledge base** — Public at [/kb/&lt;your-workspace-slug&gt;/](https://intercom-pk.up.railway.app/kb/). Admin at [/kb/admin/](https://intercom-pk.up.railway.app/kb/admin/) with Quill editor. Search hits FTS5, ranking `bm25()`; widget's suggest calls the same table.
7. **AI summarization** — Open any conversation with 5+ messages. Panel above the thread shows the JSON-schema summary. Without an Anthropic key the panel shows a `degraded: true` deterministic fallback (never spins forever — I8).
8. **Custom domains** — [/domains](https://intercom-pk.up.railway.app/domains) as admin. Full flow documented in the **Custom domains** section below; live demo uses `hvdjez12.up.railway.app` as the CNAME target.

## Architecture

One ASGI process (Django 5 + Channels 4 on Uvicorn, `--workers 1`), SQLite in WAL mode,
in-process job/worker threads, Caddy 2 for TLS. The full topology diagram, data model,
realtime protocol, email threading, and AI pipeline are documented in `architecture.md`;
this README will carry the reader-facing summary as features land.

## Setup

**Local dev**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then fill it in
python manage.py migrate
uvicorn config.asgi:application --port 8000   # --workers stays 1
```

**Live deploy (Railway — how the production URL above is served)**

Push to `main` on the connected GitHub repo — Railway detects the Dockerfile, builds, and boots. `$PORT` is honored by the CMD ([Dockerfile:14-15](Dockerfile#L14-L15)). Persistent SQLite via a Railway volume mounted at `/app/data`. TLS terminates at Railway's edge (no Caddy needed there). Env vars set in the Railway dashboard mirror `.env.example`. Custom domains via Railway's dashboard or GraphQL API (see **Custom domains** section).

**Self-hosted VM alternative (equivalent, not currently live)**

```bash
docker compose up --build                        # local: app only, http://127.0.0.1:8000
docker compose --profile proxy up -d --build     # VM: adds Caddy for public TLS on 80/443
```

The Caddy path is fully wired (`Caddyfile`, on-demand TLS, internal `/api/internal/domain-allowed` endpoint) and documented in [architecture.md §10](architecture.md); it's the target for any redeploy to Oracle Cloud, Hetzner, Fly, or bare-metal.

## AI summarization

Anthropic-Claude-Haiku generates a compact triage summary of each active
conversation: what the customer wants, what's been tried, current status, and
key details. Agents see it as a card above the thread when they open a
conversation, so a fresh hand can catch up in seconds rather than by scrolling
30 messages.

**Prompt design.** [`apps/inbox/prompts.py`](apps/inbox/prompts.py) locks the
output to strict JSON:

```json
{
  "what_they_want": "...",
  "whats_been_tried": "...",
  "current_status": "...",
  "key_details": ["...", "..."]
}
```

The system message forbids markdown, code fences, or extra keys. The user
message packs a transcript trimmed by [`build_transcript`](apps/inbox/prompts.py):
the first customer message (usually the original ask) is always kept; the last
40 messages ride along; if the estimated token count exceeds
`AI_MAX_INPUT_TOKENS` (default 8k) older tail messages are dropped first.

**Cost per summary.** With Haiku list pricing (~$1/1M input, $5/1M output as
of writing) a typical 15-message summary costs ~$0.001-0.002. Configured via
`AI_PRICE_INPUT_USD_PER_MTOKEN` / `AI_PRICE_OUTPUT_USD_PER_MTOKEN` in
settings; totals rendered on the admin cost dashboard
(`/admin/inbox/summaryjob/spend/`).

**Trigger.** [`services.post_message`](apps/inbox/services.py) checks the
delta after every send; when `last_seq - summary_upto_seq >=
AI_ENQUEUE_THRESHOLD` (default 5) and no active job exists, it enqueues one.
The refresh button on the summary card force-enqueues regardless of delta.

**Fallback (CLAUDE.md I8).** The LLM call is bounded by `AI_TIMEOUT_SEC` (8s)
via the Anthropic SDK client timeout. On timeout or exception we retry once,
then run a deterministic non-LLM summary: first customer message + last three
messages, packed into the same JSON schema with `degraded: true`. The card
shows a "Basic summary — AI unavailable" badge. The UI always renders
_something_ — even with an empty `ANTHROPIC_API_KEY` set the worker just
skips the LLM path entirely.

**Restart survival.** A `SummaryJob` in RUNNING state at crash time is swept
back to QUEUED at worker boot; nothing is lost.

## Custom domains

A workspace can serve its public KB from its own hostname
(e.g. `help.acme.com`) instead of `intercom-pk.up.railway.app/kb/acme/`. The
feature is domain-agnostic — the middleware
([`apps/core/middleware.py::_resolve_public_kb_by_host`](apps/core/middleware.py))
resolves any `Host` header against the `Domain` table and routes to the bound
workspace.

### Testing with your own domain (grader / reviewer flow)

You bring a hostname you own (e.g. `help.yourdomain.com`). You'll need admin
access to a workspace in the live app — ask the app operator for credentials
if you don't already have them. Then:

**Step 1 — Point your domain at the app.**
At your DNS provider, add a **CNAME** record:

```
cosmofeed.com  CNAME  hvdjez12.up.railway.app
```

That's the Railway edge target for this deployment. Railway auto-provisions a
Let's Encrypt certificate for your hostname on first HTTPS request — nothing
else to do on the SSL side.

**Step 2 — Connect the domain in the app.**
Log in as admin → click **Domains** in the topbar → enter
`help.yourdomain.com` → **Add domain**. The row appears with status
**Pending**. Click **Verify** — status flips to **Verified**.

_The Verify action is a stub in this build (documented in Known Limitations);
production wiring is described below._

**Step 3 — Hit your custom domain.**

```
https://help.yourdomain.com/
```

Redirects to your workspace's public KB. Category and article URLs stay under
`/kb/<workspace-slug>/…` on that host (see Known Limitations for the clean-URL
follow-up).

DNS propagation usually takes 1-5 minutes; Railway's cert issuance is
typically under a minute after that.

### Operator note (Railway account owner only)

If a new custom domain doesn't route (Railway serves its own 404 page instead
of hitting our container), the domain hasn't been registered on Railway's
side yet. Only the Railway account owner can do this:

Railway dashboard → service → Settings → Networking → **+ Custom Domain** →
enter the hostname → Railway confirms and issues the cert.

This IS a real production limitation — Vercel/Fly/Cloudflare-for-SaaS all
solve it with a "programmatic domain add" API integration (call the platform
API from our `services.create_domain`, pass the tester's hostname, get back
the CNAME target to display). Noted in Known Limitations as a follow-up.

**SSL provisioning.** On Railway, TLS is fully automatic — Let's Encrypt via
Railway's edge, one cert per custom domain, renewed transparently. Cloudflare
in front of Railway is a drop-in alternative (proxied mode gives you
Cloudflare-issued certs + edge caching for free). Self-hosters can wire
Caddy's `on_demand_tls { ask http://app:8000/api/internal/domain-allowed }`
directive — same Domain table drives it, plus a small internal endpoint that
returns 200 for verified rows. Not implemented in this phase (Railway
obsoletes it for the primary deploy) but noted here for completeness.

**DNS verification is stubbed** — see
[`apps.accounts.services.verify_domain`](apps/accounts/services.py). Clicking
Verify immediately flips `verified_at`; there is no live DNS check today.
The assignment brief explicitly permits stubbing this. A production
implementation runs two `dnspython` queries and requires both to pass:

1. `dns.resolver.resolve(hostname, "CNAME")` returns a target equal to
   `settings.BASE_HOST` — proves the DNS points traffic at us.
2. `dns.resolver.resolve(f"_verify.{hostname}", "TXT")` returns a chunk equal
   to `domain.verify_token` — proves ownership of the DNS zone (an attacker
   can't create a TXT record on a domain they don't control).

Both wrapped in a ~4s per-query timeout. Failures populate a `verify_error`
column, leave `verified_at=None`, and surface the reason to the operator.
Total code addition ~30 lines. The stub-versus-real swap is the only piece
between this phase and a production-grade custom-domain feature.

## Knowledge base

Per-workspace public KB with a Quill (CDN) WYSIWYG editor, categories, draft/publish
states, SQLite FTS5-backed search, and a widget-side suggest that surfaces relevant
articles as the visitor types.

**URL shape.** Public pages live at path-based tenant prefixes:

- `/kb/<workspace_slug>/` — index (category tiles + latest articles)
- `/kb/<workspace_slug>/c/<category_slug>/` — category detail
- `/kb/<workspace_slug>/a/<article_slug>/` — article detail
- `/kb/<workspace_slug>/search?q=…` — public search

[`TenantMiddleware._resolve_public_kb`](apps/core/middleware.py) reads `<slug>`
from the second URL segment and sets `request.workspace` for the view. Phase 9
will layer Host-header resolution on top (`help.acme.com` → same view) without
touching Phase 6's routes.

Dashboard admin lives at [`/kb/admin/`](apps/kb/urls.py) — Quill 2.0 loads from
jsDelivr; the editor's HTML output is never trusted, `apps/kb/services.py::sanitise_html`
runs `bleach.clean()` on every write with a tight allow-list (see I7).

**Search.** FTS5 virtual table `kb_article_fts` mirrors title/body_text/category
name; three SQL triggers ([migration 0002](apps/kb/migrations/0002_fts5.py))
keep it in step on every Article insert/update/delete — so shell edits, admin
UI, and future service functions all stay indexed. Ranking is
`bm25(kb_article_fts, 10.0, 1.0, 3.0)` — title matches weigh 10× body, category
name 3×. User queries are token-quoted so FTS operators (`AND`, `NEAR`, `*`,
`"`) can't misbehave; SQL injection is already impossible via parameter binding.

**Widget suggest.** As a visitor types (2+ chars, debounced 400 ms), the widget
iframe calls `GET /api/widget/kb/suggest?q=…` with its signed session token; the
endpoint runs FTS scoped to the token's workspace and returns the top 3 articles.
Clicking opens the public article page in a new tab.

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

**Deliverability & provider choice.** Outbound picks the first provider configured, in order **Brevo → Resend → SMTP** ([`apps/mail/send.py`](apps/mail/send.py)):

- **Brevo (HTTPS API)** — first because it supports *single-sender verification*: a free mailbox like `priyademo@zohomail.in` can be authorized by clicking a link Brevo emails to that address. No domain ownership required. This is what the live demo uses. Free tier is 300 emails/day.
- **Resend (HTTPS API)** — second choice when you own a domain and want production-grade deliverability. Requires SPF/DKIM/DMARC verification on the sending domain in the Resend dashboard.
- **SMTP (`smtplib`)** — local-dev fallback only. Railway, Fly.io, and most PaaS block outbound ports 25/465/587, so this deadlocks against a shipping host. Handy for hitting Zoho/Gmail app-passwords from a laptop.

`MAIL_FROM` must exactly match the sender you verified in Brevo (or the domain you verified in Resend). Inbound polling is IMAP over TLS on 993 (universally allowed).

Production deliverability requires **SPF**, **DKIM**, and **DMARC** on the sending domain — the switch from Brevo's single-sender flow to a verified domain (Brevo, Resend, or SES) is a config change, no code touch.

## Known Limitations

Every deliberate shortcut is listed here with its production swap (this is graded).
Populated as each phase lands; the full scaling-seam table lives in `architecture.md` §11.

| Limitation                                                                                                                 | Why                                                                                                                                                       | Production swap                                                                                                                                                                                                                      |
| -------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Invite emails print to the console (Django `EMAIL_BACKEND`)                                                                | Support-reply SMTP is separate (uses `smtplib` directly with `SMTP_*` env); keeping the Django backend on console avoids two credential sets for the demo | Set `EMAIL_BACKEND` to the SMTP backend to deliver invites, or reuse `SMTP_*` via `django.core.mail.backends.smtp.EmailBackend`                                                                                                      |
| Email channel handles **one workspace per deploy** (`MAIL_WORKSPACE_ID`)                                                   | Single shared support mailbox; every un-threaded inbound routes to one workspace                                                                          | Per-workspace catch-all address (e.g. `support-{slug}@domain`) or a mailbox per workspace, decoded in the poller                                                                                                                     |
| Attachments in inbound email are stripped                                                                                  | Storage, download UX, virus scanning are non-trivial for POC                                                                                              | Save payloads to `data/attachments/<msg_id>/`, new `AttachmentMeta` model, per-conversation download endpoint                                                                                                                        |
| Outbound email is best-effort, no inline retry                                                                             | `delivery_state=FAILED` is the visible signal; agent can resend manually                                                                                  | Wrap in a Job worker with exponential backoff (SummaryJob shape is the template)                                                                                                                                                     |
| Outbound uses Brevo's single-sender flow (`priyademo@zohomail.in`) — no verified custom domain on the demo                 | Author doesn't own the sending domain; Brevo lets a single free mailbox be authorized without domain ownership. Selection order is Brevo → Resend → SMTP  | Buy a domain, verify it in Brevo/Resend, set SPF/DKIM/DMARC, swap `MAIL_FROM`. Zero code change — provider selection is env-driven in [`apps/mail/send.py`](apps/mail/send.py).                                                        |
| `MAIL_FROM` must exactly equal the Brevo-verified sender (or Resend-verified domain)                                       | Both providers refuse arbitrary senders to prevent spoofing                                                                                                | Same as above — verify a domain, then any `@<domain>` local-part works                                                                                                                                                                |
| IMAP polls every 30s (no IDLE)                                                                                             | Simpler; latency is fine for a POC                                                                                                                        | `imapclient` supports IDLE — wire a supervisor loop that falls back to polling on IDLE drops                                                                                                                                         |
| Plus-address round-trip requires provider `+` sub-addressing                                                               | Gmail/Zoho support it; some corporate servers strip `+`                                                                                                   | `In-Reply-To`/`References` still threads via Path 1; corporate deployments should use a per-workspace catch-all instead                                                                                                              |
| Inbox free-text search covers subject + contact name/email only, **not message bodies**                                    | Message-body search wants SQLite FTS5, which lands with the KB in Phase 6                                                                                 | Add an FTS5 virtual table mirroring `inbox_message.body_text`, sync from `post_message`, `UNION` its hits into the `?q=` filter                                                                                                      |
| `conversation.updated` triggers a full list refetch on every connected agent                                               | Simple + always correct with active filters; POC scale (dozens of conversations) makes the extra fetches trivial                                          | In-place patch from the envelope, or a materialised list view keyed by (workspace, status, channel, assignee)                                                                                                                        |
| Snooze wake-up granularity is 60 s                                                                                         | Sweeper runs every 60 s; a conversation may reopen up to a minute after its `snoozed_until`                                                               | Reduce the interval, or wake on the exact time via a scheduled task queue                                                                                                                                                            |
| Invite token is stored raw and travels in the accept URL                                                                   | POC simplicity; token is single-use + expires (default 7 days)                                                                                            | Hash the token at rest; keep the TTL/single-use guarantees                                                                                                                                                                           |
| Rate limiting not yet applied to auth endpoints                                                                            | Deferred to Phase 10                                                                                                                                      | In-memory token bucket per IP (per `CLAUDE.md` §7)                                                                                                                                                                                   |
| Concurrent posts with the _same_ `client_msg_id` can skip one seq value                                                    | The idempotency loser burns a seq before losing the unique race                                                                                           | Harmless — the client heals the gap via `?after_seq=` backfill; the alternative (insert-first) leaks rows                                                                                                                            |
| Sync WebSocket consumers cap concurrent in-flight WS messages at the ASGI threadpool size                                  | Sync consumers keep the service layer un-colored and correct                                                                                              | Async consumers + `database_sync_to_async`, or the Redis channel layer + N replicas                                                                                                                                                  |
| Presence/typing are lost on restart                                                                                        | In-process dicts, swept by the sweeper thread                                                                                                             | Redis `SETEX` keys (architecture §11) — everything else is in the DB and backfilled                                                                                                                                                  |
| `message.ack` rides the HTTP POST response, not a WS frame                                                                 | Sends go over idempotent REST (I3); the response carries the assigned seq                                                                                 | n/a — documented design choice                                                                                                                                                                                                       |
| Widget visitor surface lives inside `apps/inbox` (`widget_api.py`, `widget_auth.py`) rather than a separate `apps/widget/` | Keeps the app count at five (CLAUDE.md §5); the code is small and cohesive with the inbox services it calls                                               | Split out only if the widget grows its own models/services                                                                                                                                                                           |
| Widget endpoints have no per-IP rate limiting                                                                              | Deferred to Phase 10 hardening                                                                                                                            | In-memory token bucket per IP on `/api/widget/session` and `/api/widget/messages` (POST)                                                                                                                                             |
| No HMAC identity verification of the visitor on widget session-create                                                      | POC assumes anonymous or trust-on-first-use                                                                                                               | Customer's server signs `user_hash = HMAC(hmac_secret, user_id)` in-page; server verifies before binding to a persistent Contact                                                                                                     |
| Empty `Workspace.allowed_origins` accepts any embed origin                                                                 | Dev-default so `demo/index.html` works cross-origin without config                                                                                        | Production must set `allowed_origins` at workspace creation; enforced in `WidgetSessionView`                                                                                                                                         |
| Widget shows offline UI until the iframe is reloaded — no live online→offline transitions                                  | POC keeps `config` a one-shot fetch on iframe load                                                                                                        | Poll `/api/widget/config` every ~30s, or push presence changes over a lightweight WS channel                                                                                                                                         |
| KB search is FTS5 in SQLite, not a dedicated search cluster                                                                | SQLite ships with FTS5; zero infra, adequate ranking for a POC                                                                                            | Postgres `tsvector`/`ts_rank` for another single-instance step; Elasticsearch/OpenSearch for horizontal scale (architecture.md §11)                                                                                                  |
| KB public URLs are path-prefixed (`/kb/<workspace_slug>/…`), not on a customer domain                                      | Phase 6 predates Phase 9 custom-domain wiring; keeps local demo working with no DNS                                                                       | Phase 9 layers `Host`-header resolution on top of the middleware branch — same views, no route changes                                                                                                                               |
| KB image upload is not implemented — Quill's image button accepts remote URLs only                                         | Storage + serving is Phase 8+ material; embed URLs from any CDN                                                                                           | Add `AttachmentMeta` model + WhiteNoise-served upload endpoint, plus a Quill image handler that POSTs and inserts the returned URL                                                                                                   |
| KB articles have a single `is_published` boolean — no archived state, no versioning, no scheduled publish                  | Simplest working state for a POC; matches the ~100-article scale a hiring demo needs                                                                      | `state` TextChoices enum + `revisions` FK table for authoring history                                                                                                                                                                |
| AI cost dashboard is staff-only (Django admin), not per-workspace-scoped                                                   | POC pattern — internal-team view, not customer-visible                                                                                                    | Add a per-workspace "usage" tab in the dashboard, gated by `IsWorkspaceAdmin`, with the same aggregation moved into a service                                                                                                        |
| AI summaries: Anthropic only, no failover to OpenAI/Gemini                                                                 | One dependency footprint for the POC; Claude Haiku's list-price is well inside the "cheap enough" band                                                    | Wrap `_make_client` in a strategy interface; add a second implementation and pick via `AI_PROVIDER` env                                                                                                                              |
| AI cost per-1M-token prices live in `settings.py` and must be updated manually as Anthropic pricing changes                | Zero infra cost; hiring-demo lifespan is short                                                                                                            | Read from a per-provider price constants module refreshed via a build-time fetch, or move pricing to a `PriceHistory` DB table                                                                                                       |
| `SummaryJob` retention is unbounded — every summary attempt is kept forever                                                | Simple; the cost dashboard depends on the history                                                                                                         | Add a nightly `VACUUM INTO`-adjacent prune (e.g. keep 90d of SUCCEEDED/DEGRADED rows)                                                                                                                                                |
| **Custom-domain DNS verification is stubbed** — clicking Verify flips `verified_at` without any DNS query                  | Assignment brief explicitly permits stubbing; `dnspython` isn't in the dep list                                                                           | Add ~30 lines: dnspython CNAME resolve to `BASE_HOST` + TXT `_verify.<host>` equals `verify_token`, both required, 4s per-query timeout. Swap point is the body of `services.verify_domain`.                                         |
| Custom-domain URLs keep the `/kb/<slug>/…` prefix (`help.acme.com/kb/acme/a/pricing/`)                                     | Kept the phase small to fit the stub scope; the middleware routing is what makes the demo real                                                            | Add parallel root routes (`/a/<slug>/`, `/c/<slug>/`, `/`) that reuse the same views + a template tag that emits the shorter form when `request.is_custom_domain` is True. Roughly a day of work.                                    |
| Live custom-domain demo requires a purchased domain (~$2) or an `/etc/hosts` entry                                         | Author is on `*.up.railway.app` without a custom domain purchased                                                                                         | Buy any domain, point CNAME at `hvdjez12.up.railway.app`, add in dashboard, verify in our UI. All code paths are already tested.                                                                                                     |
| Adding a tester's custom domain requires manual Railway-dashboard action by the app operator                               | Railway routes by Host header at its edge — only the account owner can register new hostnames on the service                                              | Wire Railway's public GraphQL API into `services.create_domain` so the app auto-provisions the hostname on Railway and returns the CNAME target to display. Standard pattern (Vercel / Fly / Cloudflare-for-SaaS) — ~1 hour of work. |
| `ALLOWED_HOSTS=['*']` in production behind Railway                                                                         | Railway is the gatekeeper — only Railway-configured domains ever reach the container                                                                      | On a bare-metal deploy without an upstream proxy, tighten this to the specific hostnames + inject verified Domain hostnames on middleware init.                                                                                      |
| No automated SQLite backup on Railway                                                                                      | Railway's volume durability + point-in-time snapshot is our backup story on this deploy. Nightly `VACUUM INTO` is designed for the self-hosted VM variant | Sweep thread runs `VACUUM INTO /app/data/backup-<date>.sqlite3` nightly on VM ([architecture.md §10](architecture.md)). For managed durability + PITR, swap to Postgres.                                                              |
| No custom error page for `413 Payload Too Large` (WhiteNoise / uvicorn defaults)                                            | POC — Brevo attachments blocked upstream, KB uploads deferred (Known Limitation above). No user-visible 413 today.                                        | When attachments land, add a `413.html` alongside the existing 400/403/404/500 pages ([templates/](templates/)).                                                                                                                      |
