"""AI summary worker + service (Phase 7).

The public API:
  - enqueue_summary(*, conversation) — idempotent job creator, called from
    services.post_message and from the manual "refresh" endpoint.
  - run_summary(*, job) — moves QUEUED → RUNNING → SUCCEEDED/DEGRADED.
    LLM call has a hard 8s timeout, one retry, then falls through to a
    deterministic non-LLM fallback (I8). Broadcasts `summary.ready` on any
    terminal state.
  - make_ai_worker_thread() — daemon poll loop, sweeper-pattern.

Fabricating a getattr / patching seam:
  _make_client() returns an Anthropic client. Tests monkeypatch this to a
  fake with the same `.messages.create` shape, so no network is hit and
  behaviour is deterministic.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from apps.inbox import prompts, realtime
from apps.inbox.models import (
    Conversation,
    Message,
    SummaryJob,
    SummaryJobStatus,
)

log = logging.getLogger("inbox.ai")

# Max total attempts (initial + retry) inside run_summary. CLAUDE.md I8: "one retry".
_MAX_ATTEMPTS = 2

# Sweep RUNNING rows older than this back to QUEUED on worker boot (crash recovery).
_STALE_RUNNING_AFTER = timedelta(seconds=30)


# --- Anthropic client seam --------------------------------------------------

def _make_client():
    """Return an Anthropic client (or None if no key). Wrapped so tests can
    monkeypatch a fake that never touches the network.
    """
    if not settings.ANTHROPIC_API_KEY:
        return None
    import anthropic

    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=settings.AI_TIMEOUT_SEC)


# --- enqueue ----------------------------------------------------------------

def _has_active_job(conversation) -> bool:
    return SummaryJob.objects.filter(
        conversation=conversation,
        status__in=[SummaryJobStatus.QUEUED, SummaryJobStatus.RUNNING],
    ).exists()


def enqueue_summary(*, conversation: Conversation, force: bool = False) -> SummaryJob | None:
    """Idempotent enqueue. Returns the created (or existing) QUEUED job, or None
    if nothing was enqueued.

    Skipped when:
      - Another QUEUED/RUNNING job for this conversation already exists.
      - No new messages since the last summary (`last_seq <= summary_upto_seq`),
        unless `force=True`.
      - Delta below threshold and not forced.
    """
    # Cheap guard: nothing new? (force still bypasses this — the refresh button
    # lets an agent re-run even when only 2 new messages exist.)
    if conversation.last_seq <= conversation.summary_upto_seq and not force:
        return None
    if not force:
        delta = conversation.last_seq - conversation.summary_upto_seq
        if delta < settings.AI_ENQUEUE_THRESHOLD:
            return None
    if _has_active_job(conversation):
        return None

    job = SummaryJob.objects.create(
        conversation=conversation,
        upto_seq=conversation.last_seq,
        status=SummaryJobStatus.QUEUED,
    )
    log.info(
        "ai_enqueue_ok conv_id=%s job_id=%s upto_seq=%s force=%s",
        conversation.id, job.id, job.upto_seq, force,
    )
    return job


# --- LLM call ---------------------------------------------------------------

def _parse_llm_json(text: str) -> str:
    """Extract the JSON object from a Claude response. Tolerates leading/trailing
    prose (against instructions, but happens) by scanning to the first `{` and
    matching braces. Returns the raw JSON text (round-trippable) or "" on failure.
    """
    if not text:
        return ""
    # Strip common code-fence wrappers first.
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    # Find the first balanced JSON object.
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError:
                    return ""
                # Coerce to the schema — anything extra is dropped.
                out = {
                    "what_they_want": str(parsed.get("what_they_want", "") or ""),
                    "whats_been_tried": str(parsed.get("whats_been_tried", "") or ""),
                    "current_status": str(parsed.get("current_status", "") or ""),
                    "key_details": [
                        str(x) for x in (parsed.get("key_details") or [])[:5]
                    ],
                }
                return json.dumps(out)
    return ""


def _call_llm(client, messages) -> tuple[str, int, int]:
    """Single LLM attempt. Returns (raw_text, input_tokens, output_tokens).

    Raises the underlying exception on any failure so the caller records the
    error and decides whether to retry / fall back.
    """
    resp = client.messages.create(
        model=settings.AI_MODEL,
        max_tokens=settings.AI_MAX_OUTPUT_TOKENS,
        system=prompts.SUMMARIZE_SYSTEM,
        messages=[
            {"role": "user", "content": prompts.user_prompt(messages, settings.AI_MAX_INPUT_TOKENS)}
        ],
    )
    # `.content` is a list of content blocks; take the first text block.
    text = ""
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", "") or ""
            break
    usage = getattr(resp, "usage", None)
    in_tok = int(getattr(usage, "input_tokens", 0) or 0)
    out_tok = int(getattr(usage, "output_tokens", 0) or 0)
    return text, in_tok, out_tok


# --- fallback ---------------------------------------------------------------

def _truncate(s: str, n: int = 240) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _fallback_summary(*, conversation: Conversation, messages: list) -> str:
    """Deterministic non-LLM summary using the first message + last three. Packs
    into the same JSON schema so the UI renders it identically.
    """
    if not messages:
        payload = {
            "what_they_want": "",
            "whats_been_tried": "",
            "current_status": "No messages yet.",
            "key_details": [],
        }
        return json.dumps(payload)
    first = messages[0]
    tail = messages[-3:]
    key_details = []
    for m in tail:
        prefix = "customer" if m.sender_type == "contact" else m.sender_type
        key_details.append(f"[{prefix}] {_truncate(m.body_text, 140)}")
    payload = {
        "what_they_want": _truncate(first.body_text, 240),
        "whats_been_tried": "",
        "current_status": _truncate(tail[-1].body_text, 240),
        "key_details": key_details,
    }
    return json.dumps(payload)


# --- run one job ------------------------------------------------------------

def _finalise(*, job: SummaryJob, summary_json: str, upto_seq: int, degraded: bool) -> None:
    """Commit the summary onto the conversation + terminal-state the job. Broadcasts
    `summary.ready` last so a consumer never sees a stale envelope.
    """
    conv = job.conversation
    now = timezone.now()
    with transaction.atomic():
        conv.summary = summary_json
        conv.summary_upto_seq = upto_seq
        conv.summary_generated_at = now
        conv.summary_degraded = degraded
        conv.save(
            update_fields=[
                "summary", "summary_upto_seq", "summary_generated_at",
                "summary_degraded", "updated_at",
            ]
        )
        job.status = (
            SummaryJobStatus.DEGRADED if degraded else SummaryJobStatus.SUCCEEDED
        )
        job.finished_at = now
        job.save(update_fields=["status", "finished_at", "updated_at"])
    realtime.broadcast(
        realtime.conv_group(conv.id), realtime.summary_ready_envelope(conv)
    )


def run_summary(*, job: SummaryJob) -> None:
    """Move QUEUED → RUNNING → SUCCEEDED / DEGRADED. Never raises; the worker
    loop relies on this contract to keep pulling new jobs after a failure.
    """
    # Guard against being called on an already-terminal job.
    if job.status != SummaryJobStatus.QUEUED:
        log.info("ai_run_skip_not_queued job_id=%s status=%s", job.id, job.status)
        return

    job.status = SummaryJobStatus.RUNNING
    job.started_at = timezone.now()
    job.save(update_fields=["status", "started_at", "updated_at"])

    conv = job.conversation
    messages = list(conv.messages.order_by("seq"))

    client = _make_client()
    if client is None:
        # No API key configured — go straight to fallback. This is still a
        # "degraded" result per I8, and the UI shows the same badge.
        log.info("ai_run_no_key job_id=%s conv_id=%s", job.id, conv.id)
        fallback = _fallback_summary(conversation=conv, messages=messages)
        _finalise(job=job, summary_json=fallback, upto_seq=job.upto_seq, degraded=True)
        return

    total_ms = 0
    last_err: Exception | None = None
    llm_json = ""
    in_tok = out_tok = 0
    attempt = 0

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        started = time.monotonic()
        try:
            text, in_tok, out_tok = _call_llm(client, messages)
            llm_json = _parse_llm_json(text)
            if llm_json:
                total_ms += int((time.monotonic() - started) * 1000)
                break
            # Parseable-but-empty: treat as failure so we retry once.
            last_err = ValueError("llm_returned_unparseable_json")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            log.warning(
                "ai_llm_attempt_failed job_id=%s attempt=%s error=%r",
                job.id, attempt, exc,
            )
        total_ms += int((time.monotonic() - started) * 1000)

    # Persist token / latency accounting once, regardless of outcome.
    job.attempts = attempt
    job.input_tokens = max(job.input_tokens, in_tok)
    job.output_tokens = max(job.output_tokens, out_tok)
    job.latency_ms = total_ms
    job.save(update_fields=["attempts", "input_tokens", "output_tokens", "latency_ms", "updated_at"])

    if llm_json:
        _finalise(job=job, summary_json=llm_json, upto_seq=job.upto_seq, degraded=False)
        log.info(
            "ai_run_ok job_id=%s conv_id=%s in_tokens=%s out_tokens=%s latency_ms=%s",
            job.id, conv.id, job.input_tokens, job.output_tokens, total_ms,
        )
        return

    # All attempts failed — deterministic fallback (I8).
    job.error = repr(last_err) if last_err else "unknown"
    job.save(update_fields=["error", "updated_at"])
    fallback = _fallback_summary(conversation=conv, messages=messages)
    _finalise(job=job, summary_json=fallback, upto_seq=job.upto_seq, degraded=True)
    log.warning(
        "ai_run_degraded job_id=%s conv_id=%s attempts=%s error=%r",
        job.id, conv.id, job.attempts, last_err,
    )


# --- worker thread ----------------------------------------------------------

def _revive_stale_running() -> int:
    """Flip RUNNING rows older than _STALE_RUNNING_AFTER back to QUEUED. Handles
    the crash-during-summarize case so we don't lose an enqueued job.
    """
    cutoff = timezone.now() - _STALE_RUNNING_AFTER
    updated = SummaryJob.objects.filter(
        status=SummaryJobStatus.RUNNING, started_at__lte=cutoff
    ).update(status=SummaryJobStatus.QUEUED)
    if updated:
        log.warning("ai_revived_stale_running count=%s", updated)
    return updated


def _pop_one_queued() -> SummaryJob | None:
    """Atomically move one QUEUED job to RUNNING and return it. SQLite has no
    SKIP LOCKED, but we only run ONE worker (I1), so a plain UPDATE ... WHERE
    id IN (SELECT id … LIMIT 1) suffices to claim exactly one row.
    """
    with transaction.atomic():
        job = (
            SummaryJob.objects.select_for_update(skip_locked=False)
            .filter(status=SummaryJobStatus.QUEUED)
            .order_by("created_at")
            .first()
        )
        # We keep the RUNNING transition inside run_summary so tests can also
        # invoke it directly. Just return the row here.
        return job


def make_ai_worker_thread() -> threading.Thread:
    """Daemon poll loop for the AI worker. Registered by InboxConfig.ready when
    RUN_BACKGROUND_THREADS=1. Matches the sweeper pattern:
      1. close_old_connections()  (I9)
      2. revive stale RUNNING once at boot
      3. pop one QUEUED; run_summary; loop
      4. sleep AI_WORKER_POLL_INTERVAL_SEC
    """
    interval = int(getattr(settings, "AI_WORKER_POLL_INTERVAL_SEC", 2))

    def run():
        log.info(
            "ai_worker_start model=%s interval=%s has_key=%s",
            getattr(settings, "AI_MODEL", "?"), interval,
            bool(getattr(settings, "ANTHROPIC_API_KEY", "")),
        )
        # One-shot at boot; a supervisor could revive on a schedule too but for a
        # POC one pass is enough — the worker's own runs will overwrite terminal states.
        try:
            close_old_connections()
            _revive_stale_running()
        except Exception as exc:  # noqa: BLE001
            log.warning("ai_worker_boot_revive_error error=%r", exc)

        while True:
            close_old_connections()
            try:
                job = _pop_one_queued()
                if job is not None:
                    run_summary(job=job)
                    continue  # tight loop when work is available
            except Exception as exc:  # noqa: BLE001 — worker must never die
                log.warning("ai_worker_loop_error error=%r", exc)
            time.sleep(interval)

    t = threading.Thread(target=run, name="ai_worker", daemon=True)
    t.start()
    return t
