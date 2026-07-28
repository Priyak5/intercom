"""Agent-facing JSON API. Thin over the service layer; every query is workspace-scoped
(I6). Sends are REST (idempotent, response carries the assigned seq = the ack, I3/I4);
the WebSocket is receive-only.
"""

import base64
import logging

from django.db.models.functions import Coalesce
from django.db.models import Q
from django.utils.dateparse import parse_datetime
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsWorkspaceMember
from apps.inbox import services
from apps.inbox.models import Conversation, ConversationStatus, Message, SenderType

log = logging.getLogger("inbox.api")

PAGE_SIZE = 30


class MessageSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    seq = serializers.IntegerField()
    sender_type = serializers.CharField()
    sender_user_id = serializers.UUIDField(allow_null=True)
    body_text = serializers.CharField()
    client_msg_id = serializers.UUIDField()
    created_at = serializers.DateTimeField()


class ConversationSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    channel = serializers.CharField()
    status = serializers.CharField()
    subject = serializers.CharField()
    assignee_id = serializers.UUIDField(allow_null=True)
    last_seq = serializers.IntegerField()
    last_message_at = serializers.DateTimeField(allow_null=True)
    agent_last_read_seq = serializers.IntegerField()
    contact_name = serializers.SerializerMethodField()
    unread = serializers.SerializerMethodField()

    def get_contact_name(self, obj):
        return obj.contact.name or obj.contact.email or "Visitor"

    def get_unread(self, obj):
        return max(0, obj.last_seq - obj.agent_last_read_seq)


def _get_conversation(request, conversation_id):
    """Workspace-scoped fetch — cross-tenant ids 404, not 403 (I6)."""
    return Conversation.objects.filter(
        id=conversation_id, workspace=request.workspace
    ).select_related("contact").first()


def _encode_cursor(ots, cid) -> str:
    return base64.urlsafe_b64encode(f"{ots.isoformat()}|{cid}".encode()).decode()


def _decode_cursor(cursor):
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ots_str, cid = raw.split("|", 1)
        return parse_datetime(ots_str), cid
    except Exception:  # noqa: BLE001 — bad cursor → treat as no cursor
        return None


class ConversationListView(APIView):
    permission_classes = [IsWorkspaceMember]

    def get(self, request):
        qs = (
            Conversation.objects.filter(workspace=request.workspace)
            .select_related("contact")
            .annotate(ots=Coalesce("last_message_at", "created_at"))
            .order_by("-ots", "-id")
        )
        status = request.query_params.get("status")
        if status in ConversationStatus.values:
            qs = qs.filter(status=status)

        cursor = request.query_params.get("after")
        if cursor:
            decoded = _decode_cursor(cursor)
            if decoded:
                ots, cid = decoded
                qs = qs.filter(Q(ots__lt=ots) | (Q(ots=ots) & Q(id__lt=cid)))

        rows = list(qs[: PAGE_SIZE + 1])
        has_more = len(rows) > PAGE_SIZE
        rows = rows[:PAGE_SIZE]
        next_cursor = _encode_cursor(rows[-1].ots, rows[-1].id) if has_more and rows else None
        return Response(
            {"results": ConversationSerializer(rows, many=True).data, "next": next_cursor}
        )


class MessageListCreateView(APIView):
    permission_classes = [IsWorkspaceMember]

    def get(self, request, conversation_id):
        conv = _get_conversation(request, conversation_id)
        if conv is None:
            return Response({"error": "not_found"}, status=404)
        after_seq = int(request.query_params.get("after_seq", 0) or 0)
        msgs = conv.messages.filter(seq__gt=after_seq).order_by("seq")
        return Response(
            {"conversation_id": str(conv.id), "last_seq": conv.last_seq,
             "results": MessageSerializer(msgs, many=True).data}
        )

    def post(self, request, conversation_id):
        conv = _get_conversation(request, conversation_id)
        if conv is None:
            return Response({"error": "not_found"}, status=404)
        client_msg_id = request.data.get("client_msg_id")
        body_text = (request.data.get("body_text") or "").strip()
        if not client_msg_id or not body_text:
            return Response({"error": "validation", "detail": "client_msg_id and body_text required"}, status=400)
        already = Message.objects.filter(conversation=conv, client_msg_id=client_msg_id).exists()
        msg = services.post_message(
            conversation=conv,
            sender_type=SenderType.AGENT,
            sender_user=request.user,
            body_text=body_text,
            client_msg_id=client_msg_id,
        )
        return Response(MessageSerializer(msg).data, status=200 if already else 201)


class ReadView(APIView):
    permission_classes = [IsWorkspaceMember]

    def post(self, request, conversation_id):
        conv = _get_conversation(request, conversation_id)
        if conv is None:
            return Response({"error": "not_found"}, status=404)
        services.mark_read(conversation=conv, reader="agent", upto_seq=request.data.get("upto_seq", 0))
        return Response({"agent_last_read_seq": conv.agent_last_read_seq})


class AssignView(APIView):
    permission_classes = [IsWorkspaceMember]

    def post(self, request, conversation_id):
        conv = _get_conversation(request, conversation_id)
        if conv is None:
            return Response({"error": "not_found"}, status=404)
        assignee = None
        assignee_id = request.data.get("assignee_id")
        if assignee_id:
            from apps.accounts.models import Membership

            m = Membership.objects.filter(
                workspace=request.workspace, user_id=assignee_id
            ).select_related("user").first()
            if m is None:
                return Response({"error": "not_found", "detail": "assignee not in workspace"}, status=404)
            assignee = m.user
        services.assign(conversation=conv, assignee=assignee, actor=request.user)
        return Response({"assignee_id": str(conv.assignee_id) if conv.assignee_id else None})


class StatusView(APIView):
    permission_classes = [IsWorkspaceMember]

    def post(self, request, conversation_id):
        conv = _get_conversation(request, conversation_id)
        if conv is None:
            return Response({"error": "not_found"}, status=404)
        services.set_status(conversation=conv, status=request.data.get("status"), actor=request.user)
        return Response({"status": conv.status})
