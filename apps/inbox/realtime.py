"""Realtime primitives shared by consumers, services, and the sweeper thread.

The load-bearing piece is `broadcast()`: `InMemoryChannelLayer` queues are bound to the
event loop that created them, so a background thread's `group_send` on a throwaway loop
never reaches consumers. We capture the ASGI main loop at startup (config/asgi.py lifespan)
and always schedule fanout onto it via `run_coroutine_threadsafe`, from any thread/sync
context. A broadcast failure is logged, never raised — the DB write is the source of
truth (I3/R1); clients heal via ?after_seq= backfill.

Channels group names may not contain ':', so groups are `ws.<id>` / `conv.<id>`.
"""

import asyncio
import logging
import threading
import time

from channels.layers import get_channel_layer
from django.db import close_old_connections

log = logging.getLogger("inbox.realtime")

PRESENCE_TTL = 45.0   # seconds an agent/contact stays "online" without a heartbeat
TYPING_TTL = 3.0      # seconds a typing indicator lingers
SWEEP_INTERVAL = 5.0  # sweeper period

_MAIN_LOOP: asyncio.AbstractEventLoop | None = None


# --- main-loop capture + broadcast ------------------------------------------

def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _MAIN_LOOP
    _MAIN_LOOP = loop
    log.info("main_loop_captured")


def clear_main_loop() -> None:
    global _MAIN_LOOP
    _MAIN_LOOP = None


def ws_group(workspace_id) -> str:
    return f"ws.{workspace_id}"


def conv_group(conversation_id) -> str:
    return f"conv.{conversation_id}"


def broadcast(group: str, envelope: dict) -> None:
    """Fan an event envelope out to a group's WebSocket consumers. Safe from any thread;
    a no-op (with a debug log) when no main loop is captured (e.g. unit tests).
    """
    loop = _MAIN_LOOP
    if loop is None:
        log.debug("broadcast_skipped_no_loop group=%s type=%s", group, envelope.get("type"))
        return
    try:
        layer = get_channel_layer()
        fut = asyncio.run_coroutine_threadsafe(
            layer.group_send(group, {"type": "fanout", "envelope": envelope}), loop
        )
        fut.result(timeout=2)
    except Exception as exc:  # noqa: BLE001 — fanout must never break the persisted write.
        log.warning("broadcast_failed group=%s error=%r", group, exc)


# --- event envelopes ({type, conversation_id, seq, data}) -------------------

def message_created_envelope(msg) -> dict:
    return {
        "type": "message.created",
        "conversation_id": str(msg.conversation_id),
        "seq": msg.seq,
        "data": {
            "id": str(msg.id),
            "seq": msg.seq,
            "sender_type": msg.sender_type,
            "sender_user_id": str(msg.sender_user_id) if msg.sender_user_id else None,
            "body_text": msg.body_text,
            "client_msg_id": str(msg.client_msg_id),
            "created_at": msg.created_at.isoformat(),
            # QUEUED at broadcast time for outbound email; the sender's REST response
            # carries the post-SMTP state (SENT/FAILED). Other viewers may see QUEUED
            # briefly — acceptable POC tradeoff.
            "delivery_state": msg.delivery_state,
        },
    }


def conversation_updated_envelope(conv) -> dict:
    return {
        "type": "conversation.updated",
        "conversation_id": str(conv.id),
        "seq": conv.last_seq,
        "data": {
            "id": str(conv.id),
            "status": conv.status,
            "channel": conv.channel,
            "subject": conv.subject,
            "assignee_id": str(conv.assignee_id) if conv.assignee_id else None,
            "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None,
            "snoozed_until": conv.snoozed_until.isoformat() if conv.snoozed_until else None,
            "contact_id": str(conv.contact_id),
        },
    }


def read_updated_envelope(conv, reader: str) -> dict:
    return {
        "type": "read.updated",
        "conversation_id": str(conv.id),
        "seq": conv.last_seq,
        "data": {
            "reader": reader,
            "agent_last_read_seq": conv.agent_last_read_seq,
            "contact_last_read_seq": conv.contact_last_read_seq,
        },
    }


def typing_envelope(conversation_id, actor_key: str, is_typing: bool, name: str = "") -> dict:
    return {
        "type": "typing",
        "conversation_id": str(conversation_id),
        "seq": None,
        "data": {"actor": actor_key, "is_typing": is_typing, "name": name},
    }


def presence_envelope(actor_key: str, online: bool) -> dict:
    return {
        "type": "presence.updated",
        "conversation_id": None,
        "seq": None,
        "data": {"actor": actor_key, "online": online},
    }


# --- presence + typing state (in-process, ephemeral) ------------------------
#
# Both dicts are keyed by the DELIVERY GROUP (conv.<id>), so presence is per-conversation:
# "is this actor currently viewing this conversation?" Each participant sees the OTHER's
# state; consumers suppress echoing an actor's own presence/typing back to itself.

_presence: dict[str, dict[str, float]] = {}  # group -> {actor_key: expires_at}
_typing: dict[str, dict[str, float]] = {}    # group -> {actor_key: expires_at}
_state_lock = threading.Lock()


def touch_presence(group: str, actor_key: str) -> bool:
    """Mark an actor online in a group (or refresh). True if this is a new online transition."""
    with _state_lock:
        actors = _presence.setdefault(group, {})
        newly_online = actor_key not in actors
        actors[actor_key] = time.monotonic() + PRESENCE_TTL
    return newly_online


def drop_presence(group: str, actor_key: str) -> bool:
    with _state_lock:
        actors = _presence.get(group)
        if not actors or actor_key not in actors:
            return False
        del actors[actor_key]
        if not actors:
            del _presence[group]
    return True


def presence_actors(group: str) -> list[str]:
    """Currently-online actors in a group — sent to a late joiner so they learn who's here."""
    now = time.monotonic()
    with _state_lock:
        return [a for a, exp in _presence.get(group, {}).items() if exp > now]


def workspace_has_online_agent(workspace_id) -> bool:
    """True if any AgentConsumer holds live presence on ws.<workspace_id>. Drives the
    widget's online/offline UI: no agent → email capture instead of live chat.
    """
    for actor in presence_actors(ws_group(workspace_id)):
        if actor.startswith("agent:"):
            return True
    return False


def touch_typing(group: str, actor_key: str) -> None:
    with _state_lock:
        _typing.setdefault(group, {})[actor_key] = time.monotonic() + TYPING_TTL


# --- sweeper thread ---------------------------------------------------------

def _conv_id_from_group(group: str) -> str:
    return group[5:] if group.startswith("conv.") else group


def _sweep_once() -> None:
    now = time.monotonic()
    expired_presence: list[tuple[str, str]] = []
    expired_typing: list[tuple[str, str]] = []
    with _state_lock:
        for group, actors in list(_presence.items()):
            for actor, exp in list(actors.items()):
                if exp <= now:
                    del actors[actor]
                    expired_presence.append((group, actor))
            if not actors:
                del _presence[group]
        for group, actors in list(_typing.items()):
            for actor, exp in list(actors.items()):
                if exp <= now:
                    del actors[actor]
                    expired_typing.append((group, actor))
            if not actors:
                del _typing[group]
    # Broadcast outside the lock (broadcast may block on fut.result).
    for group, actor in expired_presence:
        broadcast(group, presence_envelope(actor, online=False))
    for group, actor in expired_typing:
        broadcast(group, typing_envelope(_conv_id_from_group(group), actor, is_typing=False))


def make_sweeper_thread() -> threading.Thread:
    """Daemon thread: expire presence/typing every SWEEP_INTERVAL seconds (I9: refresh
    DB connections each loop even though this work is in-memory — future sweeps touch DB).
    """

    def run():
        log.info("sweeper_loop_start interval=%s", SWEEP_INTERVAL)
        while True:
            close_old_connections()
            try:
                _sweep_once()
            except Exception as exc:  # noqa: BLE001 — a sweeper must never die.
                log.warning("sweeper_error error=%r", exc)
            time.sleep(SWEEP_INTERVAL)

    thread = threading.Thread(target=run, name="sweeper", daemon=True)
    thread.start()
    return thread
