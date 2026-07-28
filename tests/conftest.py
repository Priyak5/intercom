"""Test setup + fixtures.

The settings.py SECRET_KEY guard raises when DEBUG=False and the key is the insecure
default. Force DEBUG=1 (and a placeholder key) BEFORE Django is configured — this module
is imported during pytest's initial-conftest load, ahead of pytest-django's setup.
The tenancy assertions don't depend on production cookie flags.
"""

import os

os.environ.setdefault("DEBUG", "1")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

import pytest
from rest_framework.test import APIClient

from apps.accounts import services

PASSWORD = "Zq7!vx92Lm4p"


@pytest.fixture
def admin_a(db):
    """Admin Membership of Workspace A (created via the atomic sign_up path)."""
    return services.sign_up(
        email="admin-a@example.com", password=PASSWORD, name="Admin A",
        workspace_name="Workspace A",
    )


@pytest.fixture
def admin_b(db):
    return services.sign_up(
        email="admin-b@example.com", password=PASSWORD, name="Admin B",
        workspace_name="Workspace B",
    )


@pytest.fixture
def agent_a(db, admin_a):
    """A non-admin (agent) member of Workspace A, via the invite → accept flow."""
    invite = services.create_invite(
        workspace=admin_a.workspace, inviter=admin_a.user,
        email="agent-a@example.com", role="agent",
    )
    return services.accept_invite(token=invite.token, password=PASSWORD, name="Agent A")


def _client(user):
    client = APIClient()
    client.force_login(user)  # TenantMiddleware auto-selects the user's workspace
    return client


@pytest.fixture
def client_a(admin_a):
    return _client(admin_a.user)


@pytest.fixture
def client_b(admin_b):
    return _client(admin_b.user)


@pytest.fixture
def client_agent_a(agent_a):
    return _client(agent_a.user)


# --- fake Anthropic client (Phase 7) ---------------------------------------
#
# Tests monkeypatch `apps.inbox.ai._make_client` to return one of these fakes so
# no test ever touches the network. The `.messages.create` shape matches the SDK
# minimally: `.content[0].text` + `.usage.input_tokens/.output_tokens`.


class _FakeContentBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeUsage:
    def __init__(self, input_tokens=0, output_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeResponse:
    def __init__(self, text, in_tok=100, out_tok=50):
        self.content = [_FakeContentBlock(text)]
        self.usage = _FakeUsage(in_tok, out_tok)


class _FakeMessages:
    def __init__(self, behaviour):
        self._behaviour = behaviour

    def create(self, **_kwargs):
        return self._behaviour()


class _FakeAnthropic:
    """`behaviour` is a zero-arg callable that returns a _FakeResponse or raises.
    Tests pass a lambda so they can simulate success, malformed JSON, or timeouts.
    """

    def __init__(self, behaviour):
        self.messages = _FakeMessages(behaviour)


@pytest.fixture
def fake_anthropic(monkeypatch):
    """Return a factory: `fake_anthropic(behaviour)` monkey-patches ai._make_client
    to return a _FakeAnthropic bound to that behaviour. Behaviour is a callable
    invoked on every `messages.create()` — same test can raise-then-succeed by
    using a stateful closure.
    """
    from apps.inbox import ai

    def install(behaviour):
        client = _FakeAnthropic(behaviour)
        monkeypatch.setattr(ai, "_make_client", lambda: client)
        return client

    return install
