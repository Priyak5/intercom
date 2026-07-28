"""WebSocket consumers — thin adapters over the service layer (I5). Sync consumers so
they call the sync ORM/services directly and use the same `realtime.broadcast` path.

Tenancy over WS (I6): the agent's workspace comes from the session but is verified by a
Membership DB check; a subscribed conversation is always resolved *within* that workspace;
the widget's workspace is derived from the token signature. A client-supplied conversation
id is never trusted beyond a workspace-scoped lookup.

Send happens over REST (I3); these consumers only receive control frames
(subscribe/typing/read/ping) and push notifications.
"""

import logging
from urllib.parse import parse_qs

from asgiref.sync import async_to_sync
from channels.generic.websocket import JsonWebsocketConsumer

from apps.inbox import realtime, services, widget_auth
from apps.inbox.models import Conversation

log = logging.getLogger("inbox.consumers")


class AgentConsumer(JsonWebsocketConsumer):
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
        async_to_sync(self.channel_layer.group_add)(
            realtime.ws_group(self.workspace_id), self.channel_name
        )
        realtime.touch_presence(self.workspace_id, self.actor_key)
        realtime.broadcast(
            realtime.ws_group(self.workspace_id),
            realtime.presence_envelope(self.workspace_id, self.actor_key, online=True),
        )

    def disconnect(self, code):
        if getattr(self, "conv_group_name", None):
            async_to_sync(self.channel_layer.group_discard)(
                self.conv_group_name, self.channel_name
            )
        if getattr(self, "workspace_id", None):
            async_to_sync(self.channel_layer.group_discard)(
                realtime.ws_group(self.workspace_id), self.channel_name
            )
            if realtime.drop_presence(self.workspace_id, self.actor_key):
                realtime.broadcast(
                    realtime.ws_group(self.workspace_id),
                    realtime.presence_envelope(self.workspace_id, self.actor_key, online=False),
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
            realtime.touch_presence(self.workspace_id, self.actor_key)
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
            async_to_sync(self.channel_layer.group_discard)(self.conv_group_name, self.channel_name)
        self.conversation_id = str(conv.id)
        self.conv_group_name = realtime.conv_group(conv.id)
        async_to_sync(self.channel_layer.group_add)(self.conv_group_name, self.channel_name)
        self.send_json(
            {"type": "subscribed", "conversation_id": str(conv.id), "data": {"last_seq": conv.last_seq}}
        )

    def _typing(self):
        if not self.conversation_id:
            return
        realtime.touch_typing(self.conversation_id, self.actor_key)
        realtime.broadcast(
            realtime.conv_group(self.conversation_id),
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

    def fanout(self, event):
        self.send_json(event["envelope"])


class WidgetConsumer(JsonWebsocketConsumer):
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
        async_to_sync(self.channel_layer.group_add)(self.conv_group_name, self.channel_name)
        realtime.touch_presence(self.workspace_id, self.actor_key)
        realtime.broadcast(
            realtime.conv_group(self.conversation_id),
            realtime.presence_envelope(self.workspace_id, self.actor_key, online=True),
        )

    def disconnect(self, code):
        if getattr(self, "conv_group_name", None):
            async_to_sync(self.channel_layer.group_discard)(self.conv_group_name, self.channel_name)
            realtime.drop_presence(self.workspace_id, self.actor_key)
            realtime.broadcast(
                realtime.conv_group(self.conversation_id),
                realtime.presence_envelope(self.workspace_id, self.actor_key, online=False),
            )

    def receive_json(self, content, **kwargs):
        action = content.get("action")
        if action == "typing":
            realtime.touch_typing(self.conversation_id, self.actor_key)
            realtime.broadcast(
                realtime.conv_group(self.conversation_id),
                realtime.typing_envelope(self.conversation_id, self.actor_key, True, name="Visitor"),
            )
        elif action == "read":
            seq = content.get("seq")
            if seq is not None:
                conv = Conversation.objects.filter(id=self.conversation_id).first()
                if conv is not None:
                    services.mark_read(conversation=conv, reader="contact", upto_seq=seq)
        elif action == "ping":
            realtime.touch_presence(self.workspace_id, self.actor_key)
            self.send_json({"type": "pong"})

    def fanout(self, event):
        self.send_json(event["envelope"])

    def _token(self):
        qs = parse_qs(self.scope["query_string"].decode())
        vals = qs.get("session") or qs.get("token")
        return vals[0] if vals else None
