"""Phase 5 unified-inbox filtering + snooze-expiry.

Exercises the /api/conversations query params (status, channel, assignee_id, q) plus
services.reopen_expired_snoozes(). Tenancy stays inherited from workspace-scoping in
ConversationListView — an extra cross-workspace case lives in test_tenancy.py.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.inbox import services
from apps.inbox.models import (
    Channel,
    Contact,
    Conversation,
    ConversationStatus,
)

pytestmark = pytest.mark.django_db


def _mk_conv(*, workspace, contact=None, channel=Channel.CHAT, status=ConversationStatus.OPEN,
             subject="", assignee=None, snoozed_until=None, last_message_at=None):
    contact = contact or Contact.objects.create(workspace=workspace, email=f"c-{subject or 'x'}@example.com")
    return Conversation.objects.create(
        workspace=workspace, contact=contact, channel=channel, status=status,
        subject=subject, assignee=assignee, snoozed_until=snoozed_until,
        last_message_at=last_message_at or timezone.now(),
    )


# --- default filter (no status query) hides RESOLVED -------------------------


def test_default_hides_resolved(client_a, admin_a):
    ws = admin_a.workspace
    _mk_conv(workspace=ws, subject="Open one")
    _mk_conv(workspace=ws, status=ConversationStatus.RESOLVED, subject="Done long ago")

    r = client_a.get("/api/conversations")
    assert r.status_code == 200
    subjects = [c["subject"] for c in r.json()["results"]]
    assert "Open one" in subjects
    assert "Done long ago" not in subjects


def test_status_all_returns_everything(client_a, admin_a):
    ws = admin_a.workspace
    _mk_conv(workspace=ws, subject="O")
    _mk_conv(workspace=ws, status=ConversationStatus.RESOLVED, subject="R")

    r = client_a.get("/api/conversations?status=all")
    subjects = [c["subject"] for c in r.json()["results"]]
    assert "O" in subjects and "R" in subjects


def test_status_resolved_returns_only_resolved(client_a, admin_a):
    ws = admin_a.workspace
    _mk_conv(workspace=ws, subject="Open")
    _mk_conv(workspace=ws, status=ConversationStatus.RESOLVED, subject="Resolved")

    r = client_a.get("/api/conversations?status=resolved")
    subjects = [c["subject"] for c in r.json()["results"]]
    assert subjects == ["Resolved"]


# --- channel filter ---------------------------------------------------------


def test_channel_filter(client_a, admin_a):
    ws = admin_a.workspace
    _mk_conv(workspace=ws, channel=Channel.CHAT, subject="chat-one")
    _mk_conv(workspace=ws, channel=Channel.EMAIL, subject="email-one")

    r = client_a.get("/api/conversations?channel=email")
    subjects = [c["subject"] for c in r.json()["results"]]
    assert subjects == ["email-one"]


# --- assignee filter --------------------------------------------------------


def test_assignee_filter_by_user_id(client_a, admin_a, agent_a):
    ws = admin_a.workspace
    _mk_conv(workspace=ws, subject="unassigned")
    _mk_conv(workspace=ws, subject="mine", assignee=agent_a.user)

    r = client_a.get(f"/api/conversations?assignee_id={agent_a.user.id}")
    subjects = [c["subject"] for c in r.json()["results"]]
    assert subjects == ["mine"]


def test_assignee_none_filter_returns_only_unassigned(client_a, admin_a, agent_a):
    ws = admin_a.workspace
    _mk_conv(workspace=ws, subject="unassigned")
    _mk_conv(workspace=ws, subject="mine", assignee=agent_a.user)

    r = client_a.get("/api/conversations?assignee_id=none")
    subjects = [c["subject"] for c in r.json()["results"]]
    assert subjects == ["unassigned"]


# --- q= search --------------------------------------------------------------


def test_q_matches_subject_case_insensitive(client_a, admin_a):
    ws = admin_a.workspace
    _mk_conv(workspace=ws, subject="Refund inquiry")
    _mk_conv(workspace=ws, subject="Other")

    r = client_a.get("/api/conversations?q=REFUND")
    subjects = [c["subject"] for c in r.json()["results"]]
    assert subjects == ["Refund inquiry"]


def test_q_matches_contact_email(client_a, admin_a):
    ws = admin_a.workspace
    c = Contact.objects.create(workspace=ws, email="findme@example.com", name="")
    _mk_conv(workspace=ws, contact=c, subject="Hi")
    _mk_conv(workspace=ws, subject="Nope")

    r = client_a.get("/api/conversations?q=findme")
    subjects = [c["subject"] for c in r.json()["results"]]
    assert subjects == ["Hi"]


def test_q_matches_contact_name(client_a, admin_a):
    ws = admin_a.workspace
    c = Contact.objects.create(workspace=ws, email="a@example.com", name="Priya Kaushal")
    _mk_conv(workspace=ws, contact=c, subject="A")
    _mk_conv(workspace=ws, subject="B")

    r = client_a.get("/api/conversations?q=priya")
    subjects = [c["subject"] for c in r.json()["results"]]
    assert subjects == ["A"]


# --- filter composition -----------------------------------------------------


def test_filters_compose(client_a, admin_a):
    ws = admin_a.workspace
    _mk_conv(workspace=ws, channel=Channel.EMAIL, subject="Billing issue")
    _mk_conv(workspace=ws, channel=Channel.CHAT, subject="Billing rare")
    _mk_conv(workspace=ws, channel=Channel.EMAIL, subject="Other")

    r = client_a.get("/api/conversations?status=open&channel=email&q=billing")
    subjects = [c["subject"] for c in r.json()["results"]]
    assert subjects == ["Billing issue"]


# --- StatusView with snoozed_until ------------------------------------------


def test_status_snoozed_requires_future_snoozed_until(client_a, admin_a):
    ws = admin_a.workspace
    conv = _mk_conv(workspace=ws, subject="need-snooze", last_message_at=timezone.now())

    # Missing snoozed_until → 400 (service raises ValidationError).
    r = client_a.post(f"/api/conversations/{conv.id}/status", {"status": "snoozed"}, format="json")
    assert r.status_code == 400

    # Past → 400.
    past = (timezone.now() - timedelta(hours=1)).isoformat()
    r = client_a.post(
        f"/api/conversations/{conv.id}/status",
        {"status": "snoozed", "snoozed_until": past}, format="json",
    )
    assert r.status_code == 400

    # Future → 200, `snoozed_until` echoed back.
    future = (timezone.now() + timedelta(hours=1)).isoformat()
    r = client_a.post(
        f"/api/conversations/{conv.id}/status",
        {"status": "snoozed", "snoozed_until": future}, format="json",
    )
    assert r.status_code == 200
    conv.refresh_from_db()
    assert conv.status == ConversationStatus.SNOOZED
    assert conv.snoozed_until is not None


def test_reopen_clears_snoozed_until(client_a, admin_a):
    ws = admin_a.workspace
    conv = _mk_conv(
        workspace=ws, status=ConversationStatus.SNOOZED,
        snoozed_until=timezone.now() + timedelta(hours=1),
        subject="wake-me", last_message_at=timezone.now(),
    )
    r = client_a.post(f"/api/conversations/{conv.id}/status", {"status": "open"}, format="json")
    assert r.status_code == 200
    conv.refresh_from_db()
    assert conv.status == ConversationStatus.OPEN
    assert conv.snoozed_until is None


# --- snooze-expiry sweep ----------------------------------------------------


def test_reopen_expired_snoozes_flips_only_past(admin_a):
    ws = admin_a.workspace
    past = _mk_conv(
        workspace=ws, status=ConversationStatus.SNOOZED,
        snoozed_until=timezone.now() - timedelta(minutes=1),
        subject="past", last_message_at=timezone.now(),
    )
    future = _mk_conv(
        workspace=ws, status=ConversationStatus.SNOOZED,
        snoozed_until=timezone.now() + timedelta(minutes=30),
        subject="future", last_message_at=timezone.now(),
    )

    count = services.reopen_expired_snoozes()
    assert count == 1

    past.refresh_from_db(); future.refresh_from_db()
    assert past.status == ConversationStatus.OPEN
    assert past.snoozed_until is None
    assert future.status == ConversationStatus.SNOOZED
    assert future.snoozed_until is not None


# --- serializer exposes snoozed_until ---------------------------------------


def test_serializer_exposes_snoozed_until_and_user_id(client_a, admin_a, agent_a):
    ws = admin_a.workspace
    when = timezone.now() + timedelta(hours=2)
    _mk_conv(
        workspace=ws, status=ConversationStatus.SNOOZED, snoozed_until=when,
        assignee=agent_a.user, subject="s",
    )
    r = client_a.get("/api/conversations?status=snoozed")
    row = r.json()["results"][0]
    assert row["snoozed_until"] is not None
    assert row["assignee_id"] == str(agent_a.user.id)


def test_members_endpoint_exposes_user_id(client_a, admin_a):
    r = client_a.get("/api/members")
    assert r.status_code == 200
    assert all("user_id" in m for m in r.json())
    assert all(m["user_id"] for m in r.json())
