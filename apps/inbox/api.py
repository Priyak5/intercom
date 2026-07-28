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
from apps.inbox.models import Channel, Conversation, ConversationStatus, Message, SenderType

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
    delivery_state = serializers.CharField()


class ConversationSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    channel = serializers.CharField()
    status = serializers.CharField()
    subject = serializers.CharField()
    assignee_id = serializers.UUIDField(allow_null=True)
    last_seq = serializers.IntegerField()
    last_message_at = serializers.DateTimeField(allow_null=True)
    agent_last_read_seq = serializers.IntegerField()
    snoozed_until = serializers.DateTimeField(allow_null=True)
    contact_name = serializers.SerializerMethodField()
    contact_email = serializers.SerializerMethodField()
    unread = serializers.SerializerMethodField()
    summary = serializers.CharField(allow_blank=True)
    summary_upto_seq = serializers.IntegerField()
    summary_generated_at = serializers.DateTimeField(allow_null=True)
    summary_degraded = serializers.BooleanField()

    def get_contact_name(self, obj):
        return obj.contact.name or obj.contact.email or "Visitor"

    def get_contact_email(self, obj):
        return obj.contact.email or ""

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

        # Status filter. Absent → open + snoozed (hide resolved by default so a busy
        # inbox doesn't drown in resolved conversations).
        status = request.query_params.get("status")
        if status == "all":
            pass
        elif status in ConversationStatus.values:
            qs = qs.filter(status=status)
        else:
            qs = qs.filter(status__in=[ConversationStatus.OPEN, ConversationStatus.SNOOZED])

        # Channel filter.
        channel = request.query_params.get("channel")
        if channel in Channel.values:
            qs = qs.filter(channel=channel)

        # Assignee filter. "none" means unassigned; a UUID means that member.
        assignee_id = request.query_params.get("assignee_id")
        if assignee_id == "none":
            qs = qs.filter(assignee__isnull=True)
        elif assignee_id:
            qs = qs.filter(assignee_id=assignee_id)

        # Free-text search on subject + contact name/email (I6-safe: workspace filter
        # is already applied above). Bodies aren't searched here — FTS5 lands with the
        # KB in Phase 6, documented as a Known Limitation.
        q = (request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(subject__icontains=q)
                | Q(contact__name__icontains=q)
                | Q(contact__email__icontains=q)
            )

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
        # Piggy-back the summary on the open-thread fetch so the client renders
        # the card without a second round-trip. `stale=True` when the summary
        # doesn't yet cover the tail messages.
        from django.conf import settings

        threshold = getattr(settings, "AI_ENQUEUE_THRESHOLD", 5)
        summary_block = {
            "summary": conv.summary,
            "upto_seq": conv.summary_upto_seq,
            "generated_at": conv.summary_generated_at.isoformat() if conv.summary_generated_at else None,
            "degraded": conv.summary_degraded,
            "stale": (conv.last_seq - conv.summary_upto_seq) >= threshold,
        }
        return Response(
            {"conversation_id": str(conv.id), "last_seq": conv.last_seq,
             "results": MessageSerializer(msgs, many=True).data,
             "summary": summary_block}
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
        status = request.data.get("status")
        snoozed_until = None
        raw = request.data.get("snoozed_until")
        if raw:
            snoozed_until = parse_datetime(raw)
            if snoozed_until is None:
                return Response(
                    {"error": "validation", "detail": "snoozed_until is not a valid ISO datetime"},
                    status=400,
                )
        services.set_status(
            conversation=conv, status=status, actor=request.user, snoozed_until=snoozed_until
        )
        return Response(
            {
                "status": conv.status,
                "snoozed_until": conv.snoozed_until.isoformat() if conv.snoozed_until else None,
            }
        )


class SummaryRefreshView(APIView):
    """POST /api/conversations/<id>/summary/refresh — force-enqueue a summary job
    even if the message-delta hasn't crossed AI_ENQUEUE_THRESHOLD. Used by the
    "Refresh" button in the summary card.
    """

    permission_classes = [IsWorkspaceMember]

    def post(self, request, conversation_id):
        conv = _get_conversation(request, conversation_id)
        if conv is None:
            return Response({"error": "not_found"}, status=404)
        from apps.inbox import ai

        job = ai.enqueue_summary(conversation=conv, force=True)
        return Response(
            {
                "enqueued": job is not None,
                "job_id": str(job.id) if job is not None else None,
            },
            status=202 if job is not None else 200,
        )
