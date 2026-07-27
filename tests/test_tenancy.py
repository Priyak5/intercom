"""Cross-workspace isolation (CLAUDE.md I6). Phase 1 covers the member-management
surface; later phases append conversation/message/article/domain sections here.

Gate: workspace A cannot see/modify/enumerate workspace B via any endpoint, and an
invited agent is denied admin-only actions.
"""

import pytest

from apps.accounts.models import Invite

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

def test_admin_cannot_lock_themselves_out(client_a, admin_a):
    # The sole admin cannot self-remove or self-demote (self-guards raise 400 before
    # the last-admin check is even reached).
    assert client_a.delete(f"/api/members/{admin_a.id}").status_code == 400
    assert client_a.post(
        f"/api/members/{admin_a.id}/role", {"role": "agent"}, format="json"
    ).status_code == 400
