"""WebSocket consumers — thin adapters over the service layer (I5). Sync consumers so
they call the sync ORM/services directly and use the same `realtime.broadcast` path.

Tenancy over WS (I6): the agent's workspace comes from the session but is verified by a
Membership DB check; a subscribed conversation is always resolved *within* that workspace;
the widget's workspace is derived from the token signature. A client-supplied conversation
id is never trusted beyond a workspace-scoped lookup.

Presence/typing are per-conversation and delivered to the conv group. `fanout` suppresses
echoing an actor's own presence/typing back to itself, so each party sees only the OTHER's
state. Send happens over REST (I3); these consumers only receive control frames.
"""

import logging
from urllib.parse import parse_qs

from asgiref.sync import async_to_sync
from channels.generic.websocket import JsonWebsocketConsumer

from apps.inbox import realtime, services, widget_auth
from apps.inbox.models import Conversation

log = logging.getLogger("inbox.consumers")


class _PresenceMixin:
    """Shared fanout that drops an actor's own presence/typing echo."""

    def fanout(self, event):
        env = event["envelope"]
        if env.get("type") in ("typing", "presence.updated"):
            if (env.get("data") or {}).get("actor") == self.actor_key:
                return  # don't show someone their own typing/presence
        self.send_json(env)

    def _join_conv(self, group):
        async_to_sync(self.channel_layer.group_add)(group, self.channel_name)
        # Tell this socket who is already present here (late-joiner snapshot).
        for actor in realtime.presence_actors(group):
            if actor != self.actor_key:
                self.send_json(realtime.presence_envelope(actor, True))
        realtime.touch_presence(group, self.actor_key)
        realtime.broadcast(group, realtime.presence_envelope(self.actor_key, True))

    def _leave_conv(self, group):
        realtime.drop_presence(group, self.actor_key)
        realtime.broadcast(group, realtime.presence_envelope(self.actor_key, False))
        async_to_sync(self.channel_layer.group_discard)(group, self.channel_name)


class AgentConsumer(_PresenceMixin, JsonWebsocketConsumer):
    def connect(self):
        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            self.close(code=4401)
            return
        session = self.scope.get("session")
        workspace_id = session.get("workspace_id") if session else None
        if not workspace_id:
            self.close(code=4403)
            return
        from apps.accounts.models import Membership

        if not Membership.objects.filter(user=user, workspace_id=workspace_id).exists():
            self.close(code=4403)
            return

        self.user = user
        self.workspace_id = str(workspace_id)
        self.actor_key = f"agent:{user.id}"
        self.conversation_id = None
        self.conv_group_name = None
        self.accept()
        # ws group carries inbox-list updates (conversation.updated); presence starts when
        # the agent actually opens a conversation.
        async_to_sync(self.channel_layer.group_add)(
            realtime.ws_group(self.workspace_id), self.channel_name
        )

    def disconnect(self, code):
        if getattr(self, "conv_group_name", None):
            self._leave_conv(self.conv_group_name)
        if getattr(self, "workspace_id", None):
            async_to_sync(self.channel_layer.group_discard)(
                realtime.ws_group(self.workspace_id), self.channel_name
            )

    def receive_json(self, content, **kwargs):
        action = content.get("action")
        if action == "subscribe":
            self._subscribe(content.get("conversation_id"))
        elif action == "typing":
            self._typing()
        elif action == "read":
            self._read(content.get("seq"))
        elif action == "ping":
            if self.conv_group_name:
                realtime.touch_presence(self.conv_group_name, self.actor_key)
            self.send_json({"type": "pong"})

    def _subscribe(self, conversation_id):
        conv = (
            Conversation.objects.filter(id=conversation_id, workspace_id=self.workspace_id).first()
            if conversation_id
            else None
        )
        if conv is None:
            self.send_json({"type": "error", "data": {"detail": "conversation not found"}})
            return
        if self.conv_group_name:
            self._leave_conv(self.conv_group_name)
        self.conversation_id = str(conv.id)
        self.conv_group_name = realtime.conv_group(conv.id)
        self._join_conv(self.conv_group_name)
        self.send_json(
            {"type": "subscribed", "conversation_id": str(conv.id), "data": {"last_seq": conv.last_seq}}
        )

    def _typing(self):
        if not self.conversation_id:
            return
        realtime.touch_typing(self.conv_group_name, self.actor_key)
        realtime.broadcast(
            self.conv_group_name,
            realtime.typing_envelope(
                self.conversation_id, self.actor_key, True, name=self.user.name or self.user.email
            ),
        )

    def _read(self, seq):
        if not self.conversation_id or seq is None:
            return
        conv = Conversation.objects.filter(
            id=self.conversation_id, workspace_id=self.workspace_id
        ).first()
        if conv is not None:
            services.mark_read(conversation=conv, reader="agent", upto_seq=seq)


class WidgetConsumer(_PresenceMixin, JsonWebsocketConsumer):
    def connect(self):
        info = widget_auth.verify_visitor_token(self._token())
        if info is None:
            self.close(code=4401)
            return
        self.workspace_id = info["workspace_id"]
        self.conversation_id = info["conversation_id"]
        self.contact_id = info["contact_id"]
        self.actor_key = f"contact:{self.contact_id}"
        conv = Conversation.objects.filter(
            id=self.conversation_id,
            workspace_id=self.workspace_id,
            contact_id=self.contact_id,
        ).first()
        if conv is None:
            self.close(code=4403)
            return
        self.accept()
        self.conv_group_name = realtime.conv_group(self.conversation_id)
        self._join_conv(self.conv_group_name)

    def disconnect(self, code):
        if getattr(self, "conv_group_name", None):
            self._leave_conv(self.conv_group_name)

    def receive_json(self, content, **kwargs):
        action = content.get("action")
        if action == "typing":
            realtime.touch_typing(self.conv_group_name, self.actor_key)
            realtime.broadcast(
                self.conv_group_name,
                realtime.typing_envelope(self.conversation_id, self.actor_key, True, name="Visitor"),
            )
        elif action == "read":
            seq = content.get("seq")
            if seq is not None:
                conv = Conversation.objects.filter(id=self.conversation_id).first()
                if conv is not None:
                    services.mark_read(conversation=conv, reader="contact", upto_seq=seq)
        elif action == "ping":
            realtime.touch_presence(self.conv_group_name, self.actor_key)
            self.send_json({"type": "pong"})

    def _token(self):
        qs = parse_qs(self.scope["query_string"].decode())
        vals = qs.get("session") or qs.get("token")
        return vals[0] if vals else None
