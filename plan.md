# plan.md — Build Plan to Closure

Ten working phases. Each has a hard acceptance gate. Do not start phase N+1 until
phase N's gate is fully checked. `CLAUDE.md` §9 defines "done"; this file defines
"done *with what*".

Ordering principle: **deploy on day one, hardest thing while fresh, graded-checklist
items never left to the end.**

---

## Phase 0 — Skeleton + live deploy (Day 1, half day)

Goal: a URL that responds over HTTPS before any feature exists. Removes the single
biggest submission risk ("if it's not live and deployed, it's an automatic no").

- [x] Repo init, `.gitignore` (exclude `data/`, `.env`, `*.sqlite3`)
- [x] `requirements.txt` pinned
- [x] `config/settings.py` — one file, env-driven, SQLite with WAL pragmas
- [x] `core.BaseModel` (UUID pk, timestamps)
- [x] `/healthz` returning DB write check + thread liveness
- [x] `config/asgi.py` — ProtocolTypeRouter, boot assert for single-worker (I1)
- [x] `Dockerfile`, `docker-compose.yml`, `Caddyfile`
- [ ] VM provisioned (Oracle Always Free ARM, or Hetzner CX22)  ← **deploy handoff**
- [ ] Domain on Cloudflare DNS; `app.<domain>` → VM, `help-demo.<domain>` CNAME set up
      for the phase-9 custom-domain demo  ← **deploy handoff**
- [ ] `docker compose up -d` on the VM, Caddy issuing a real cert  ← **deploy handoff**
- [x] `.env.example` complete
- [x] `README.md` stub with architecture heading

Verified locally + in Docker: `docker build` succeeds, container runs `migrate` +
single-worker uvicorn, `/healthz` → 200 `{"status":"ok","db":"ok","threads":{}}`; WAL +
`foreign_keys`/`busy_timeout` pragmas active on every connection; I1 assert refuses
`>1` worker; custom `accounts_user` table (no `auth_user`). The three unchecked items
need a VM + Cloudflare + deploy access — see the deploy runbook.

**Gate:** `https://app.<domain>/healthz` returns 200 with a valid certificate, from
a container image built by `docker build`. _(Image + `/healthz` proven; live cert
pending the deploy handoff.)_

---

## Phase 1 — Auth, workspaces, tenancy (Day 1 second half – Day 2)

Requirement #1.

- [x] `User` (email login, `USERNAME_FIELD = "email"`), `Workspace`, `Membership`, `Invite`
- [x] Signup creates User + Workspace + admin Membership atomically
- [x] Login / logout / `GET /api/auth/me`
- [x] `TenantMiddleware` sets `request.workspace` (I6); rejects requests with no membership
- [x] `IsWorkspaceMember`, `IsWorkspaceAdmin` DRF permissions
- [x] Invite flow: admin creates → token emailed (console backend + link in UI; SMTP is Phase 4) → acceptance sets password + Membership
- [x] Member list / role change / remove (admin only)
- [x] `templates/base.html`, auth pages, workspace switcher in the shell
- [x] Session cookie hardening; CSRF on all dashboard POSTs (DRF SessionAuthentication + form tokens)
- [x] `tests/test_tenancy.py` — cross-workspace read and write both denied (9/9 green on Py3.12)

**Gate:** Two workspaces created via UI. Workspace A's admin cannot see, modify, or
enumerate anything in workspace B via any endpoint. Invited agent can log in and is
correctly denied admin-only actions.

---

## Phase 2 — Inbox core + chat realtime (Days 3–4) ← hardest, do it fresh

Requirement #2 backend + #4 foundation. This phase decides the "Real-time
Architecture" score.

- [x] `Contact`, `Conversation`, `Message` models with the exact constraints from
      `architecture.md` §4 (`unique(conversation, seq)`, `unique(conversation, client_msg_id)`)
- [x] Atomic `seq` allocation helper — one `UPDATE ... RETURNING` (I2)
- [x] `inbox/services.py`: `get_or_create_conversation`, `post_message`,
      `assign`, `set_status`, `mark_read` — the only writers
- [x] `inbox/consumers.py`: `AgentConsumer` (groups `ws.{workspace}`, `conv.{id}`),
      `WidgetConsumer` (group `conv.{id}`) — sync consumers; colon→dot (Channels group names)
- [x] Event envelope `{type, conversation_id, seq, data}` for all seven event types
      (`message.ack` rides the HTTP response; `summary.ready` reserved for Phase 7)
- [x] `static/js/socket.js`: connect, heartbeat, exponential backoff 1s→30s with jitter,
      gap detection, `?after_seq=` backfill, offline banner + offline send queue
- [x] `GET /api/conversations` keyset-paginated; `GET .../messages?after_seq=`
- [x] `POST .../messages` idempotent on `client_msg_id`, returns assigned `seq`
- [x] Optimistic send + reconciliation in the UI (dedupe by `client_msg_id`; ack = POST response)
- [x] Typing indicator (throttled, 3s server-side expiry)
- [x] Presence (in-process dict, 45s expiry, swept by the sweeper thread)
- [x] Read receipts via `agent_last_read_seq` / `contact_last_read_seq`
- [x] `tests/test_ordering.py` — concurrent posts produce a dense gap-free seq range
- [x] `tests/test_idempotency.py` — same `client_msg_id` twice yields one message

Realtime broadcast path verified: a visitor's REST post is delivered over WebSocket
(main-loop capture + `run_coroutine_threadsafe`), bidirectional agent↔visitor messaging
persists, and `/healthz` reports the live sweeper thread. Full suite green on Py3.12
(`docker run … pytest -q` → 12 passed). The 30s-network-kill browser step is satisfied
architecturally by socket.js reconnect+backfill+offline-queue.

**Gate:** Two browsers, agent and visitor. Kill the network for 30s mid-conversation
while the other side keeps sending. On reconnect the transcript is complete, in order,
with no duplicates. Restart the container — full history persists.

---

## Phase 3 — Widget packaging (Day 5)

Requirement #2 delivery surface.

- [x] `static/widget/loader.js` — the single `<script>` tag, injects a bubble + lazy iframe
- [x] `templates/inbox/widget_frame.html` — the real UI, isolated in the iframe (kept in
      `apps/inbox/` alongside `widget_api.py`; CLAUDE.md §5 repo map updated accordingly)
- [x] `GET /api/widget/config?key=` — brand colour, welcome message, online state
      (online = `realtime.workspace_has_online_agent`, agent presence on `ws.<id>`)
- [x] `POST /api/widget/session` — signed visitor token, origin allowlist check,
      returns `visitor_id` for return-visitor reuse
- [x] Visitor `{visitor_id}` in `localStorage` → returning visitor rejoins their open
      conversation; token is reminted on each load (24h TTL is fine)
- [x] `GET /api/widget/conversation` history restore
- [x] Offline state: no agent present → capture email instead of live chat
- [x] `demo/index.html` — standalone page with the widget installed **(graded checklist item)**
- [x] Mobile-responsive widget (fullscreen iframe under 480px; `env(safe-area-inset-*)`)
- [ ] HMAC identity verification — deferred, documented in README Known Limitations
- [ ] IP rate limits on `/api/widget/*` — deferred to Phase 10 hardening

**Gate:** `demo/index.html` served from a different origin loads the widget with one
script tag, sends a message that appears in the dashboard in under a second, and the
conversation is still there after closing the browser and returning.

---

## Phase 4 — Email channel (Days 6–7)

Requirement #3. The second-heaviest graded criterion.

- [ ] Mailbox provisioned (Gmail/Zoho app password), catch-all or plus-addressing verified — deploy-time step
- [x] `apps/mail/poller.py` — IMAPClient poll thread, 30s interval (no IDLE — POC
      tradeoff documented in README), UID cursor persisted **before** processing
      (`MailboxCursor.last_uid`). UIDVALIDITY change resets the cursor with a warning
- [x] Raw MIME stored on `Message.raw_mime` for replay/debugging
- [x] `apps/mail/threading.py` — resolution in order:
      1. `In-Reply-To` / `References` → existing `Message.email_message_id`
      2. plus-address token `support+c{conv}.{hmac8}@` (`apps/mail/addressing.py`)
      3. same sender + same normalised subject within 7 days
      4. otherwise new conversation (Conversation.objects.create — never reuses
         an open conversation on subject miss)
- [x] Quoted-text stripping (`email-reply-parser`), HTML→text fallback (stdlib
      regex; BeautifulSoup avoided to keep deps light)
- [x] Attachment handling: stripped, one-line placeholder appended, documented
      Known Limitation
- [x] `apps/mail/send.py` — SMTP with explicit
      `Message-ID: <c{conv}.m{msg}@domain>`, `In-Reply-To`, growing `References`
      chain (capped at 20); `Reply-To` carries the plus-address for path-2 return
- [x] Inbound email → `Conversation(channel=email)` via `services.post_message`
      → broadcast `message.created` on `AgentConsumer` (unchanged path)
- [x] Reply from the dashboard sends via SMTP (bounded 8s timeout; `delivery_state`
      flips SENT/FAILED, failure surfaced as `⚠ failed to send` on the bubble)
- [x] `tests/test_threading.py` — 4 paths + reply-to-reply + idempotent replay
      + bad-plus-token fallthrough + subject normalisation + addressing round-trip
      (10 cases; +1 cross-workspace tenancy case)
- [ ] SPF / DKIM / DMARC noted in README (deliverability is graded)
- [ ] End-to-end verification with a real mailbox (deploy-time)

**Gate:** Send an email from a personal Gmail to the support address → appears as a
conversation. Reply from the dashboard → arrives in Gmail as a normal threaded reply.
Reply again from Gmail → lands in the *same* conversation, not a new one. Restart the
container mid-flight; no message is lost or duplicated.

---

## Phase 5 — Unified inbox UI (Day 8, first half)

Requirement #4.

- [x] Single list combining chat + email, `channel` badge (already done Phase 4)
- [x] Filters: `status` (open/snoozed/resolved/all), `channel` (chat/email),
      `assignee_id` (uuid or `none` = unassigned), `q` (LIKE on subject + contact
      name/email — bodies deferred to FTS5 in Phase 6)
- [x] Assign / reassign (thread-header dropdown), snooze with `snoozed_until`
      (preset menu: 1h, 4h, tomorrow 9am, next Monday 9am, custom), resolve / reopen
- [x] Snooze expiry sweeper — `apps/inbox/snoozer.py`, 60s interval, reuses
      `services.set_status` so audit line + broadcast come for free
- [x] Audit line in the thread for assign/status changes (existing
      `sender_type=SYSTEM` message pattern — no extra table)
- [x] Live list updates over `ws.<workspace>` — `conversation.updated` envelope
      now carries `snoozed_until`; client refetches with the current filters
- [x] Keyboard-navigable (↑/↓ or j/k, Enter, `/`, Esc), responsive filter bar,
      empty ("No conversations match") and loading (skeleton) states
- [x] Two new indexes: `(workspace, channel, -last_message_at)` and
      `(status, snoozed_until)` — first backs channel filtering, second makes
      the snooze sweeper cheap

**Gate:** Two agents in the same workspace. One assigns and resolves; the other's list
updates without refresh. Filters produce correct counts. `EXPLAIN QUERY PLAN` shows an
index in use for the default list query.

---

## Phase 6 — Knowledge base (Day 8, second half)

Requirement #5.

- [ ] `Category`, `Article` with per-workspace unique slugs
- [ ] Quill editor via CDN; `bleach` sanitization on write (I7)
- [ ] Draft / publish states
- [ ] FTS5 virtual table synced in the service layer; `bm25()` ranking
- [ ] Public KB pages: index, category, article — server-rendered, tenant from `Host`
- [ ] Public search endpoint (published only)
- [ ] `GET /api/widget/kb/suggest?q=` — top 3, called debounced at 400ms as the
      visitor types in the widget
- [ ] XSS test: paste `<script>` and an `onerror` payload into an article, confirm neutralised

**Gate:** Article created in the dashboard is publicly readable and searchable. Typing
a question in the chat widget surfaces relevant articles before the message is sent.
Injected script payloads do not execute.

---

## Phase 7 — AI summarization (Day 9, first half)

Requirement #6.

- [ ] `core.Job` + `core/worker.py` poller thread (`close_old_connections()` per iteration)
- [ ] `inbox/prompts.py` — strict JSON schema:
      `{what_they_want, whats_been_tried, current_status, key_details[]}`
- [ ] `inbox/ai.py` — context window: first message + last 40, truncated to ~8K tokens,
      oldest dropped first
- [ ] Trigger: conversation opened and `last_seq - summary_upto_seq >= 5` → enqueue
- [ ] 8s timeout, one retry, then deterministic non-LLM fallback with `degraded: true` (I8)
- [ ] `summary.ready` pushed over WS from the worker thread
- [ ] Cost/latency/token counts recorded on the Job payload; a management command or
      admin view to total spend **(answers the "cost awareness" criterion)**
- [ ] Summary panel in the conversation view with stale/refreshing/degraded states

**Gate:** Open a 30-message conversation → summary appears within seconds and is
accurate. Add five more messages → it refreshes. Set an invalid API key → the panel
shows a usable fallback, the UI never hangs, and the failure is logged with the job id.

---

## Phase 8 — Stretch features (Day 9, second half — strictly time-boxed)

Only after phases 0–7 are green. Pick by cost/benefit, cheapest first.

- [ ] Canned responses (one model, `/` shortcut in the composer) — ~1h, high visible value
- [ ] AI auto-reply drafts (reuses phase 7 pipeline + KB context) — ~1.5h, strong signal
- [ ] Contact timeline (past conversations, pages visited, last seen — data already captured) — ~1h
- [ ] SLA tracking (`first_response_at` / `resolved_at` already stored; add targets + breach badge) — ~1h
- [ ] Analytics dashboard (response time, resolution rate, busiest hours, per-agent) — ~2h
- [ ] Webhooks/API (outbound HTTP on events via the Job queue + a scoped API key) — ~2h

**Gate:** Anything half-built here is reverted, not shipped. A missing stretch feature
costs nothing; a broken one costs credibility.

---

## Phase 9 — Custom domains (Day 10, first half)

Requirement #7.

- [ ] `Domain` model with `verify_token`, `verified_at`, `ssl_status`
- [ ] `POST /api/domains` returns the exact CNAME + TXT records to set
- [ ] `POST /api/domains/{id}/verify` — `dnspython` resolution check, sets `verified_at`
- [ ] `GET /api/internal/domain-allowed?domain=` — 200 only for verified domains;
      not reachable through the public proxy
- [ ] Caddy `on_demand_tls { ask ... }` wired; cert issues on first request
- [ ] Host-header → `Domain` → `Workspace` resolution in middleware
- [ ] UI: add domain, show pending DNS instructions, verified/SSL state
- [ ] End-to-end demo using `help-demo.<domain>` as a stand-in customer domain

**Gate:** `https://help-demo.<domain>` serves that workspace's KB with a valid
Let's Encrypt certificate obtained automatically. An unverified hostname is refused
a certificate.

---

## Phase 10 — Hardening, docs, submission (Day 10, second half)

- [ ] Error pages (400/403/404/500) that don't leak internals
- [ ] `DEBUG=False` verified in prod; `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` correct
- [ ] Log review: every failure path emits one useful line with ids, no secrets
- [ ] SQLite backup: nightly `VACUUM INTO` via the worker thread, documented
- [ ] Full manual pass of the grader's script: sign up → create workspace → test every feature
- [ ] Seed/demo data command so a fresh grader account isn't staring at an empty inbox
- [ ] Image build + `docker save` + `docker load` verified on a clean machine
- [ ] `README.md` complete (see below)
- [ ] Commit history reviewed — granular, meaningful messages, no single mega-commit

### README must contain
1. What it is, live URLs, and test credentials
2. Architecture overview with the topology diagram from `architecture.md`
3. Tech choices **with reasons** — especially why no Redis/Celery/Postgres
4. Real-time protocol: seq ordering, gap detection, idempotency, reconnect
5. Email threading algorithm, all four resolution paths
6. AI: prompt design, context windowing, cost per summary, fallback behaviour
7. Custom domain + SSL approach
8. Setup instructions (local, Docker, image transfer)
9. **Known Limitations** table — every shortcut, with the production swap named
10. What was deliberately skipped and why

### Submission checklist (from the assignment)
- [ ] Live product URL — deployed, functional, signup works
- [ ] Live chat bubble demo page — separate page, widget installed
- [ ] Email inbox test — support address routes into the unified inbox with threading
- [ ] GitHub repository — clean history, README as specified
- [ ] Message Aditya on the given number with the links
- [ ] Email the submission to the given address, VP in CC

---

## Risk register

| Risk | Mitigation |
|---|---|
| Real-time work overruns and eats email time | Phase 2 is fixed at 2 days; if it slips, cut stretch features, never phase 4 |
| Free-tier VM reclaimed / rebooted | Data on a mounted volume; `restart: unless-stopped`; nightly `VACUUM INTO` backup pulled off-box |
| Gmail throttles or flags outbound | 500/day cap is ample; if flagged, swap to Zoho — `send.py` is the only file affected |
| SQLite write contention under demo load | WAL + `busy_timeout`; single writer path through services; documented as a known limit |
| `InMemoryChannelLayer` state lost on restart | Only presence/typing are ephemeral; everything else is in the DB and backfilled via `?after_seq=` |
| Custom-domain cert fails during grading | `help-demo` subdomain pre-provisioned and verified in phase 9, not live-demoed cold |
| Grader signs up and sees an empty product | Seed command in phase 10 |
| Scope creep into stretch features | Phase 8 is time-boxed and revertible by rule |

---

## Daily rhythm

Each day: pull the phase gate above, work only on it, and end the day by
(a) deploying to the VM, (b) updating the README's Known Limitations table with
anything you shortcut, and (c) committing. A day that ends undeployed is a day of
unmeasured risk.