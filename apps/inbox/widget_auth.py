"""Signed visitor-session tokens for the widget (Phase 2 minimal; Phase 3 hardens the
surrounding endpoints). The token is signed with the WORKSPACE'S `hmac_secret` — so the
signature itself is the tenancy derivation (I6/architecture §9); a forger without the
server-only secret cannot mint one.

Format: `<workspace_id>:<signed_blob>`. The plaintext workspace_id selects which secret
to verify against; the blob's signature proves authenticity.
"""

from django.conf import settings
from django.core import signing

SALT = "inbox.widget.session"


def mint_visitor_token(*, workspace, contact, conversation) -> str:
    blob = signing.dumps(
        {"c": str(contact.id), "conv": str(conversation.id)},
        key=workspace.hmac_secret,
        salt=SALT,
    )
    return f"{workspace.id}:{blob}"


def verify_visitor_token(token: str) -> dict | None:
    """Return {workspace_id, contact_id, conversation_id} or None if invalid/expired."""
    if not token or ":" not in token:
        return None
    from apps.accounts.models import Workspace

    workspace_id, blob = token.split(":", 1)
    workspace = Workspace.objects.filter(id=workspace_id).first()
    if workspace is None:
        return None
    max_age = int(getattr(settings, "WIDGET_TOKEN_TTL_HOURS", 24)) * 3600
    try:
        data = signing.loads(blob, key=workspace.hmac_secret, salt=SALT, max_age=max_age)
    except (signing.BadSignature, signing.SignatureExpired):
        return None
    return {
        "workspace_id": str(workspace.id),
        "contact_id": data["c"],
        "conversation_id": data["conv"],
    }
