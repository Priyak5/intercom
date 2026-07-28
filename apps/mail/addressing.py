"""Plus-address token encode/decode.

Format: `<local>+c<conv_uuid_hex>.<hmac8>@<domain>`

The token binds an outbound Reply-To header to a specific Conversation so that when the
customer's client strips In-Reply-To/References we can still route the reply back
correctly (threading path 2). The 8-hex HMAC prevents forging conversation ids — a
malicious sender can't stuff a random UUID into their To: line and inject into another
conversation, because they don't know the workspace's `hmac_secret`.
"""

import hashlib
import hmac
import logging
import re
import uuid

log = logging.getLogger("mail.addressing")

_LOCAL_RE = re.compile(r"^(?P<local>[^+@]+)\+c(?P<conv>[0-9a-f]{32})\.(?P<hmac>[0-9a-f]{8})$", re.I)


def _hmac8(hmac_secret: str, conv_id: uuid.UUID) -> str:
    return hmac.new(
        hmac_secret.encode("utf-8"),
        conv_id.hex.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()[:8]


def encode(*, local: str, domain: str, workspace_hmac_secret: str, conversation_id) -> str:
    """Return the full plus-address for a conversation. `local` is the mailbox local-part
    (e.g. "support"); `domain` is MAIL_DOMAIN.
    """
    conv_uuid = conversation_id if isinstance(conversation_id, uuid.UUID) else uuid.UUID(str(conversation_id))
    tag = _hmac8(workspace_hmac_secret, conv_uuid)
    return f"{local}+c{conv_uuid.hex}.{tag}@{domain}"


def decode(address: str, workspace_hmac_secret: str) -> uuid.UUID | None:
    """Return the conversation UUID if the address encodes a valid, correctly-signed
    plus-token for the given workspace secret. Otherwise None (silently — the caller
    falls through to the next threading path).
    """
    if not address:
        return None
    address = address.strip().lower()
    if "@" not in address:
        return None
    local_part, _, _domain = address.partition("@")
    m = _LOCAL_RE.match(local_part)
    if not m:
        return None
    try:
        conv_uuid = uuid.UUID(m.group("conv"))
    except ValueError:
        return None
    expected = _hmac8(workspace_hmac_secret, conv_uuid)
    # hmac.compare_digest to avoid a timing side-channel on the tag byte comparison.
    if not hmac.compare_digest(expected, m.group("hmac").lower()):
        log.debug("plus_token_bad_hmac conv=%s", conv_uuid)
        return None
    return conv_uuid


def extract_all_addresses(*header_values: str) -> list[str]:
    """Pull every `local@domain` out of one or more raw header values (To, Cc, Delivered-To).
    Best-effort — used to try each recipient against decode(). We don't care about the
    display name.
    """
    if not header_values:
        return []
    joined = ",".join(v for v in header_values if v)
    # Grab "<addr>" bracketed form and bare addr forms.
    hits = re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+", joined)
    return [h.lower() for h in hits]
