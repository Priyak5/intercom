"""Minimal visitor (widget) surface — Phase 2. Token-authed, no session/CSRF.

Just enough for the agent↔visitor reconnect gate: mint a signed session, list/post
messages as the contact. Phase 3 promotes this to `apps/widget` with the iframe/loader,
config endpoint, origin allowlist, HMAC identity, and rate limits.
"""

import logging

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Workspace
from apps.inbox import services, widget_auth
from apps.inbox.api import MessageSerializer
from apps.inbox.models import Contact, Conversation, Message, SenderType

log = logging.getLogger("inbox.widget")


def _resolve_conversation(request):
    """Resolve the token-bound conversation, or None. Token via X-Widget-Token header,
    or `token` in the query/body.
    """
    token = (
        request.headers.get("X-Widget-Token")
        or request.query_params.get("token")
        or request.data.get("token")
    )
    info = widget_auth.verify_visitor_token(token)
    if info is None:
        return None
    return (
        Conversation.objects.filter(
            id=info["conversation_id"],
            workspace_id=info["workspace_id"],
            contact_id=info["contact_id"],
        )
        .select_related("workspace", "contact")
        .first()
    )


class WidgetSessionView(APIView):
    authentication_classes = []  # no session/CSRF; token-authed surface
    permission_classes = [AllowAny]

    def post(self, request):
        workspace = Workspace.objects.filter(public_key=request.data.get("public_key")).first()
        if workspace is None:
            return Response({"error": "unknown_workspace"}, status=404)
        # Phase 2: a fresh anonymous contact + conversation per session. Returning-visitor
        # reuse (localStorage token) is Phase 3.
        contact = Contact.objects.create(workspace=workspace)
        conversation = services.get_or_create_conversation(workspace=workspace, contact=contact)
        token = widget_auth.mint_visitor_token(
            workspace=workspace, contact=contact, conversation=conversation
        )
        log.info(
            "widget_session workspace_id=%s conv_id=%s contact_id=%s",
            workspace.id, conversation.id, contact.id,
        )
        return Response(
            {
                "token": token,
                "conversation_id": str(conversation.id),
                "workspace": {"name": workspace.name},
            },
            status=201,
        )


class WidgetMessagesView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        conv = _resolve_conversation(request)
        if conv is None:
            return Response({"error": "unauthorized"}, status=401)
        after_seq = int(request.query_params.get("after_seq", 0) or 0)
        msgs = conv.messages.filter(seq__gt=after_seq).order_by("seq")
        return Response(
            {"conversation_id": str(conv.id), "last_seq": conv.last_seq,
             "results": MessageSerializer(msgs, many=True).data}
        )

    def post(self, request):
        conv = _resolve_conversation(request)
        if conv is None:
            return Response({"error": "unauthorized"}, status=401)
        client_msg_id = request.data.get("client_msg_id")
        body_text = (request.data.get("body_text") or "").strip()
        if not client_msg_id or not body_text:
            return Response({"error": "validation"}, status=400)
        already = Message.objects.filter(conversation=conv, client_msg_id=client_msg_id).exists()
        msg = services.post_message(
            conversation=conv,
            sender_type=SenderType.CONTACT,
            body_text=body_text,
            client_msg_id=client_msg_id,
        )
        return Response(MessageSerializer(msg).data, status=200 if already else 201)
