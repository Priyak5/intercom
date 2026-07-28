"""Phase 7 AI summarisation. Tests are offline: `fake_anthropic` monkey-patches
`ai._make_client` so no HTTP request is made.

The invariants being guarded:
  - enqueue is idempotent (no duplicate QUEUED rows) and threshold-gated
  - run_summary always terminates the job (I8): success on happy path, DEGRADED
    when all attempts fail, never raises
  - Conversation.summary + summary_upto_seq + summary_generated_at are stamped
    atomically with the job's terminal transition
"""

from __future__ import annotations

import json
import uuid

import pytest

from apps.inbox import ai, prompts, services
from apps.inbox.models import (
    Channel,
    Conversation,
    Contact,
    SenderType,
    SummaryJob,
    SummaryJobStatus,
)

pytestmark = pytest.mark.django_db


# --- helpers ---------------------------------------------------------------


def _mkconv(admin, seed_messages: int = 0) -> Conversation:
    """Create a chat conversation with `seed_messages` posted via the real
    service (so seq allocation and Conversation.last_seq stay in sync).
    """
    contact = Contact.objects.create(workspace=admin.workspace, email=f"c-{uuid.uuid4().hex[:6]}@example.com")
    conv = services.get_or_create_conversation(
        workspace=admin.workspace, contact=contact, channel=Channel.CHAT,
    )
    for i in range(seed_messages):
        services.post_message(
            conversation=conv,
            sender_type=SenderType.CONTACT if i % 2 == 0 else SenderType.AGENT,
            body_text=f"message body #{i}",
            client_msg_id=uuid.uuid4(),
        )
    conv.refresh_from_db()
    return conv


def _good_llm_json() -> str:
    return json.dumps({
        "what_they_want": "Cancel their subscription.",
        "whats_been_tried": "Suggested a 20% discount.",
        "current_status": "Awaiting the customer's reply.",
        "key_details": ["order 42", "billed 2026-06-01"],
    })


# --- enqueue guardrails ----------------------------------------------------


def test_enqueue_below_threshold_is_noop(admin_a, settings):
    settings.AI_ENQUEUE_THRESHOLD = 5
    conv = _mkconv(admin_a, seed_messages=3)
    # `post_message` will have called enqueue at each step, but the threshold
    # gate stops it. Reset the queue and verify a direct call returns None.
    SummaryJob.objects.all().delete()
    assert ai.enqueue_summary(conversation=conv) is None
    assert SummaryJob.objects.filter(conversation=conv).count() == 0


def test_enqueue_above_threshold_creates_one_job(admin_a, settings):
    settings.AI_ENQUEUE_THRESHOLD = 5
    conv = _mkconv(admin_a, seed_messages=5)
    # post_message already enqueued as it crossed the threshold on message #5;
    # exactly one QUEUED job should exist.
    jobs = SummaryJob.objects.filter(conversation=conv, status=SummaryJobStatus.QUEUED)
    assert jobs.count() == 1
    # A second explicit enqueue is a no-op (an active job blocks new enqueues).
    assert ai.enqueue_summary(conversation=conv) is None
    assert SummaryJob.objects.filter(conversation=conv).count() == 1


def test_force_enqueue_bypasses_threshold(admin_a, settings):
    settings.AI_ENQUEUE_THRESHOLD = 5
    conv = _mkconv(admin_a, seed_messages=2)
    SummaryJob.objects.filter(conversation=conv).delete()
    job = ai.enqueue_summary(conversation=conv, force=True)
    assert job is not None
    assert job.upto_seq == conv.last_seq


# --- happy-path LLM run ----------------------------------------------------


def test_run_summary_success_writes_conversation(admin_a, fake_anthropic, settings):
    settings.AI_ENQUEUE_THRESHOLD = 3
    settings.ANTHROPIC_API_KEY = "sk-test"
    conv = _mkconv(admin_a, seed_messages=6)
    from tests.conftest import _FakeResponse

    fake_anthropic(lambda: _FakeResponse(_good_llm_json(), in_tok=180, out_tok=90))

    job = SummaryJob.objects.filter(conversation=conv, status=SummaryJobStatus.QUEUED).first()
    assert job is not None
    ai.run_summary(job=job)
    job.refresh_from_db()
    conv.refresh_from_db()
    assert job.status == SummaryJobStatus.SUCCEEDED
    assert job.input_tokens == 180
    assert job.output_tokens == 90
    assert conv.summary
    parsed = json.loads(conv.summary)
    assert parsed["what_they_want"] == "Cancel their subscription."
    assert conv.summary_upto_seq == job.upto_seq
    assert conv.summary_generated_at is not None
    assert conv.summary_degraded is False


# --- retry + degraded fallback ---------------------------------------------


def test_run_summary_all_attempts_fail_falls_back(admin_a, fake_anthropic, settings):
    settings.AI_ENQUEUE_THRESHOLD = 3
    settings.ANTHROPIC_API_KEY = "sk-test"
    conv = _mkconv(admin_a, seed_messages=4)

    calls = {"n": 0}
    def _always_fail():
        calls["n"] += 1
        raise TimeoutError("simulated api timeout")
    fake_anthropic(_always_fail)

    ai.enqueue_summary(conversation=conv, force=True)
    job = SummaryJob.objects.filter(conversation=conv, status=SummaryJobStatus.QUEUED).first()
    ai.run_summary(job=job)

    job.refresh_from_db()
    conv.refresh_from_db()
    assert calls["n"] == 2, "expected initial + 1 retry"
    assert job.status == SummaryJobStatus.DEGRADED
    assert job.attempts == 2
    assert "TimeoutError" in job.error
    # Fallback still populates the conversation summary (I8 — UI always renders).
    assert conv.summary
    payload = json.loads(conv.summary)
    assert conv.summary_degraded is True
    assert payload["what_they_want"]  # first message body_text carried through
    assert payload["key_details"]


def test_run_summary_no_api_key_goes_straight_to_fallback(admin_a, settings, monkeypatch):
    settings.AI_ENQUEUE_THRESHOLD = 3
    settings.ANTHROPIC_API_KEY = ""
    # Explicitly ensure _make_client returns None without any monkeypatch tricks.
    monkeypatch.setattr(ai, "_make_client", lambda: None)
    conv = _mkconv(admin_a, seed_messages=4)
    ai.enqueue_summary(conversation=conv, force=True)
    job = SummaryJob.objects.filter(conversation=conv, status=SummaryJobStatus.QUEUED).first()
    ai.run_summary(job=job)
    job.refresh_from_db()
    conv.refresh_from_db()
    assert job.status == SummaryJobStatus.DEGRADED
    assert conv.summary_degraded is True
    assert conv.summary


# --- LLM returns malformed JSON: retries then falls back --------------------


def test_run_summary_unparseable_json_falls_back(admin_a, fake_anthropic, settings):
    settings.ANTHROPIC_API_KEY = "sk-test"
    conv = _mkconv(admin_a, seed_messages=4)
    from tests.conftest import _FakeResponse

    fake_anthropic(lambda: _FakeResponse("this is not JSON at all"))
    ai.enqueue_summary(conversation=conv, force=True)
    job = SummaryJob.objects.filter(conversation=conv, status=SummaryJobStatus.QUEUED).first()
    ai.run_summary(job=job)
    job.refresh_from_db()
    conv.refresh_from_db()
    assert job.status == SummaryJobStatus.DEGRADED
    # The fallback ran with the deterministic schema.
    parsed = json.loads(conv.summary)
    assert set(parsed.keys()) == {"what_they_want", "whats_been_tried", "current_status", "key_details"}


# --- transcript trimming ---------------------------------------------------


def test_build_transcript_trims_middle_when_over_budget(admin_a):
    conv = _mkconv(admin_a, seed_messages=50)
    msgs = list(conv.messages.order_by("seq"))
    # 40-char budget * 4 chars/token = ~160 char total; forces heavy trimming.
    transcript = prompts.build_transcript(msgs, max_input_tokens=40)
    # Must still contain the first message and the tail.
    assert f"[{msgs[0].seq} " in transcript
    assert f"[{msgs[-1].seq} " in transcript
    # But NOT a random middle message.
    assert f"[{msgs[20].seq} " not in transcript


# --- tenancy ---------------------------------------------------------------


def test_enqueue_is_scoped_by_conversation_workspace(admin_a, admin_b):
    """A SummaryJob is always bound to a specific Conversation; there's no code
    path where a job in workspace B could touch a conversation in workspace A.
    This test is a sanity assertion — the FK does the enforcement.
    """
    conv_a = _mkconv(admin_a, seed_messages=6)
    conv_b = _mkconv(admin_b, seed_messages=6)
    assert (
        SummaryJob.objects.filter(conversation=conv_a).first().conversation.workspace_id
        == admin_a.workspace_id
    )
    assert (
        SummaryJob.objects.filter(conversation=conv_b).first().conversation.workspace_id
        == admin_b.workspace_id
    )
    # A workspace-A-scoped query never returns the B job.
    a_jobs = SummaryJob.objects.filter(conversation__workspace=admin_a.workspace)
    b_jobs = SummaryJob.objects.filter(conversation__workspace=admin_b.workspace)
    assert set(a_jobs.values_list("id", flat=True)).isdisjoint(set(b_jobs.values_list("id", flat=True)))


# --- open-thread REST payload includes the summary block --------------------


def test_open_conversation_returns_summary_block(client_a, admin_a):
    conv = _mkconv(admin_a, seed_messages=1)
    resp = client_a.get(f"/api/conversations/{conv.id}/messages")
    assert resp.status_code == 200
    body = resp.json()
    assert "summary" in body
    assert set(body["summary"].keys()) == {
        "summary", "upto_seq", "generated_at", "degraded", "stale",
    }
