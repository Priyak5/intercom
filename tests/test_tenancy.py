"""Cross-workspace isolation (CLAUDE.md I6). Phase 1 covers the member-management
surface; later phases append conversation/message/article/domain sections here.

Gate: workspace A cannot see/modify/enumerate workspace B via any endpoint, and an
invited agent is denied admin-only actions.
"""

import uuid

import pytest

from apps.accounts.models import Invite
from apps.inbox.models import Channel, Contact, Conversation, Message, SenderType

pytestmark = pytest.mark.django_db


# --- accounts: member management -------------------------------------------

def test_member_list_scoped_to_workspace(client_a, admin_a, admin_b, agent_a):
    resp = client_a.get("/api/members")
    assert resp.status_code == 200
    emails = {m["email"] for m in resp.json()}
    assert "admin-a@example.com" in emails
    assert "agent-a@example.com" in emails
    # Workspace B's people must never appear.
    assert "admin-b@example.com" not in emails


def test_cannot_change_role_in_other_workspace(client_a, admin_a, admin_b):
    # admin_b's membership id belongs to Workspace B; the service scopes by request.workspace.
    resp = client_a.post(
        f"/api/members/{admin_b.id}/role", {"role": "agent"}, format="json"
    )
    assert resp.status_code == 404


def test_cannot_remove_member_in_other_workspace(client_a, admin_a, admin_b):
    resp = client_a.delete(f"/api/members/{admin_b.id}")
    assert resp.status_code == 404


def test_client_supplied_workspace_id_is_ignored(client_a, admin_a, admin_b):
    # Body carries Workspace B's id; the invite must still be created in A (I6).
    resp = client_a.post(
        "/api/members/invite",
        {"email": "new@example.com", "role": "agent", "workspace_id": str(admin_b.workspace_id)},
        format="json",
    )
    assert resp.status_code == 201
    invite = Invite.objects.get(email="new@example.com")
    assert invite.workspace_id == admin_a.workspace_id
    assert invite.workspace_id != admin_b.workspace_id


def test_switch_to_non_member_workspace_denied(client_a, admin_a, admin_b):
    resp = client_a.post(
        "/api/workspace/switch", {"workspace_id": str(admin_b.workspace_id)}, format="json"
    )
    assert resp.status_code == 404
    # Session unchanged: /api/auth/me still reports Workspace A.
    me = client_a.get("/api/auth/me").json()
    assert me["workspace"]["id"] == str(admin_a.workspace_id)


# --- roles: agent denied admin actions -------------------------------------

def test_agent_can_read_but_not_administer(client_agent_a, agent_a):
    assert client_agent_a.get("/api/members").status_code == 200
    assert client_agent_a.post(
        "/api/members/invite", {"email": "x@example.com", "role": "agent"}, format="json"
    ).status_code == 403
    assert client_agent_a.post(
        f"/api/members/{agent_a.id}/role", {"role": "admin"}, format="json"
    ).status_code == 403
    assert client_agent_a.delete(f"/api/members/{agent_a.id}").status_code == 403


# --- public / anonymous surfaces -------------------------------------------

def test_public_paths_ok_for_anonymous(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/login").status_code == 200


def test_api_requires_authentication(client):
    assert client.get("/api/members").status_code == 403


# --- guards ----------------------------------------------------------------

# --- kb (Phase 6): public-URL slug scoping + cross-workspace read isolation ---

def test_kb_public_article_scoped_by_workspace_slug(client, admin_a, admin_b):
    """Workspace B has a published article; requesting it under Workspace A's slug 404s."""
    from apps.kb import services as kb_services

    b_article = kb_services.create_article(
        workspace=admin_b.workspace, author=admin_b.user,
        title="B private title", body_html="<p>B body</p>",
    )
    kb_services.publish_article(article=b_article)

    # Correct workspace slug — 200.
    ok = client.get(f"/kb/{admin_b.workspace.slug}/a/{b_article.slug}/")
    assert ok.status_code == 200

    # Wrong workspace slug — 404, no content leak.
    wrong = client.get(f"/kb/{admin_a.workspace.slug}/a/{b_article.slug}/")
    assert wrong.status_code == 404


def test_kb_public_hides_drafts(client, admin_a):
    from apps.kb import services as kb_services

    draft = kb_services.create_article(
        workspace=admin_a.workspace, author=admin_a.user,
        title="Draft only", body_html="<p>hidden</p>",
    )
    resp = client.get(f"/kb/{admin_a.workspace.slug}/a/{draft.slug}/")
    assert resp.status_code == 404


def test_kb_unknown_workspace_slug_returns_404(client):
    resp = client.get("/kb/does-not-exist/")
    assert resp.status_code == 404


# --- widget (Phase 3): cross-tenant visitor_id / token isolation -----------

def test_widget_visitor_id_from_other_workspace_gets_fresh_contact(client, admin_a, admin_b):
    """A `visitor_id` belonging to workspace B, presented with workspace A's public_key,
    must not reuse the B-contact. The endpoint silently mints a fresh A-contact rather
    than returning 404 or 403 (no info-leak about foreign contacts). I6.
    """
    b_contact = Contact.objects.create(workspace=admin_b.workspace, email="")
    resp = client.post(
        "/api/widget/session",
        {
            "public_key": admin_a.workspace.public_key,
            "visitor_id": str(b_contact.id),
        },
        format="json",
    )
    assert resp.status_code == 201
    body = resp.json()
    new_visitor_id = body["visitor_id"]
    assert new_visitor_id != str(b_contact.id)
    # The fresh contact lives in workspace A, not B.
    new_contact = Contact.objects.get(id=new_visitor_id)
    assert new_contact.workspace_id == admin_a.workspace_id


def test_widget_config_scoped_to_public_key(client, admin_a, admin_b):
    """`/api/widget/config` returns only the workspace matching the public_key it's given."""
    resp_a = client.get(f"/api/widget/config?key={admin_a.workspace.public_key}")
    assert resp_a.status_code == 200
    assert resp_a.json()["name"] == admin_a.workspace.name

    resp_unknown = client.get("/api/widget/config?key=pk_does_not_exist")
    assert resp_unknown.status_code == 404


def test_widget_origin_allowlist_rejects_disallowed(client, admin_a):
    """Non-empty `allowed_origins` blocks session-create from other origins (403)."""
    admin_a.workspace.allowed_origins = ["https://only-this.example.com"]
    admin_a.workspace.save(update_fields=["allowed_origins"])
    resp = client.post(
        "/api/widget/session",
        {"public_key": admin_a.workspace.public_key},
        format="json",
        HTTP_ORIGIN="https://evil.example.com",
    )
    assert resp.status_code == 403


# --- mail (Phase 4): cross-workspace Message-Id must not leak ---------------

def test_mail_inbound_does_not_reuse_other_workspace_message_id(admin_a, admin_b):
    """A Message-Id that exists in Workspace B must not be picked up as a Path-1 match
    when the poller processes an inbound scoped to Workspace A. I6.
    """
    from apps.mail import inbound
    from email.utils import formatdate

    # Seed Workspace B with a conversation + message whose email_message_id we'll try
    # to reuse from an A-scoped inbound.
    b_contact = Contact.objects.create(workspace=admin_b.workspace, email="cust@example.com")
    b_conv = Conversation.objects.create(
        workspace=admin_b.workspace, contact=b_contact, channel=Channel.EMAIL,
        subject="B's private thread", last_seq=1,
    )
    shared_mid = "<shared-mid@mail.example.com>"
    Message.objects.create(
        conversation=b_conv, seq=1, sender_type=SenderType.CONTACT,
        body_text="B message", client_msg_id=uuid.uuid4(), email_message_id=shared_mid,
    )

    raw = (
        "From: cust@example.com\r\n"
        "To: support@example.com\r\n"
        f"Subject: Re: Anything\r\n"
        f"Date: {formatdate(localtime=True)}\r\n"
        f"In-Reply-To: {shared_mid}\r\n"
        "Message-ID: <a-inbound@mail.example.com>\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: text/plain; charset="utf-8"\r\n'
        "\r\ntrying to hop into B\r\n"
    ).encode("utf-8")

    inbound.process_inbound(raw, workspace=admin_a.workspace)

    # B is untouched.
    assert b_conv.messages.count() == 1
    # A gets a brand-new conversation (Path 4 fallthrough).
    a_convs = Conversation.objects.filter(workspace=admin_a.workspace, channel=Channel.EMAIL)
    assert a_convs.count() == 1


# --- inbox (Phase 5): filters never leak workspace B into workspace A -------


def test_filtered_list_does_not_leak_across_workspaces(client_a, admin_a, admin_b):
    """Even with the most permissive filters (?status=all), Workspace B's conversations
    must not appear in a query authenticated as Workspace A. I6.
    """
    from django.utils import timezone

    a_contact = Contact.objects.create(workspace=admin_a.workspace, email="a@example.com")
    b_contact = Contact.objects.create(workspace=admin_b.workspace, email="b@example.com")
    Conversation.objects.create(
        workspace=admin_a.workspace, contact=a_contact, channel=Channel.CHAT,
        subject="A-shared-term", last_message_at=timezone.now(),
    )
    Conversation.objects.create(
        workspace=admin_b.workspace, contact=b_contact, channel=Channel.CHAT,
        subject="B-shared-term", last_message_at=timezone.now(),
    )

    r = client_a.get("/api/conversations?status=all&q=shared-term")
    assert r.status_code == 200
    subjects = [c["subject"] for c in r.json()["results"]]
    assert "A-shared-term" in subjects
    assert "B-shared-term" not in subjects


def test_admin_cannot_lock_themselves_out(client_a, admin_a):
    # The sole admin cannot self-remove or self-demote (self-guards raise 400 before
    # the last-admin check is even reached).
    assert client_a.delete(f"/api/members/{admin_a.id}").status_code == 400
    assert client_a.post(
        f"/api/members/{admin_a.id}/role", {"role": "agent"}, format="json"
    ).status_code == 400
