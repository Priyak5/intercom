"""Visitor (widget) surface — Phase 3. Token-authed, no session/CSRF.

Endpoints:
  GET  /api/widget/config?key=<public_key>   — brand + online state (no token)
  POST /api/widget/session                   — mint token; accepts visitor_id for reuse
  GET  /api/widget/conversation              — history restore (token-authed)
  GET  /api/widget/messages?after_seq=       — incremental messages (token-authed)
  POST /api/widget/messages                  — send a message (token-authed, idempotent)

Tenancy (I6) is derived from the request: `public_key` selects the workspace on session
create; the signed token binds workspace+contact+conversation afterwards. A client-sent
`visitor_id` is verified against `workspace` before reuse, so a token/contact from another
tenant never crosses over.
"""

import logging

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Workspace
from apps.inbox import realtime, services, widget_auth
from apps.inbox.api import MessageSerializer
from apps.inbox.models import Contact, Conversation, ConversationStatus, Message, SenderType

log = logging.getLogger("inbox.widget")


def _cors(response, request):
    """Same-origin browsers embed the widget cross-origin, so widget endpoints must reply
    with permissive CORS. No credentials are used (token in a header, not a cookie), so
    `*` is safe.
    """
    origin = request.headers.get("Origin", "*")
    response["Access-Control-Allow-Origin"] = origin if origin != "null" else "*"
    response["Vary"] = "Origin"
    response["Access-Control-Allow-Headers"] = "Content-Type, X-Widget-Token"
    response["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


def _origin_allowed(workspace: Workspace, request) -> bool:
    """Origin allowlist check. Empty list = allow-any (dev default, README-documented)."""
    allowed = workspace.allowed_origins or []
    if not allowed:
        return True
    origin = request.headers.get("Origin", "")
    return origin in allowed


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


class _WidgetView(APIView):
    """Shared widget-endpoint base: no auth, allow-any, CORS on every response."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        return _cors(response, request)

    def options(self, request, *args, **kwargs):
        return Response(status=204)


class WidgetConfigView(_WidgetView):
    """Public read of widget branding + online state. No token; safe to cache briefly."""

    def get(self, request):
        workspace = Workspace.objects.filter(public_key=request.query_params.get("key", "")).first()
        if workspace is None:
            return Response({"error": "unknown_workspace"}, status=404)
        return Response(
            {
                "name": workspace.name,
                "brand_color": workspace.brand_color,
                "welcome_message": workspace.welcome_message,
                "online": realtime.workspace_has_online_agent(workspace.id),
            }
        )


class WidgetSessionView(_WidgetView):
    def post(self, request):
        workspace = Workspace.objects.filter(public_key=request.data.get("public_key")).first()
        if workspace is None:
            return Response({"error": "unknown_workspace"}, status=404)
        if not _origin_allowed(workspace, request):
            log.info(
                "widget_origin_denied workspace_id=%s origin=%s",
                workspace.id, request.headers.get("Origin", ""),
            )
            return Response({"error": "origin_not_allowed"}, status=403)

        # Returning visitor: reuse the contact if it belongs to this workspace. If the id
        # is unknown/tampered, silently fall through to a fresh contact — never leak that
        # a Contact exists in a different workspace (I6).
        contact = None
        visitor_id = (request.data.get("visitor_id") or "").strip()
        if visitor_id:
            contact = Contact.objects.filter(id=visitor_id, workspace=workspace).first()

        # Optional email capture from the offline-form branch: persist on the Contact so
        # a returning agent can reply by email once the poller is wired (Phase 4).
        email = (request.data.get("visitor_email") or "").strip()

        if contact is None:
            contact = Contact.objects.create(workspace=workspace, email=email)
        elif email and not contact.email:
            contact.email = email
            contact.save(update_fields=["email", "updated_at"])

        conversation = services.get_or_create_conversation(workspace=workspace, contact=contact)
        token = widget_auth.mint_visitor_token(
            workspace=workspace, contact=contact, conversation=conversation
        )
        log.info(
            "widget_session workspace_id=%s conv_id=%s contact_id=%s reused=%s",
            workspace.id, conversation.id, contact.id, bool(visitor_id and contact),
        )
        return Response(
            {
                "token": token,
                "visitor_id": str(contact.id),
                "conversation_id": str(conversation.id),
                "workspace": {
                    "name": workspace.name,
                    "brand_color": workspace.brand_color,
                    "welcome_message": workspace.welcome_message,
                },
                "online": realtime.workspace_has_online_agent(workspace.id),
            },
            status=201,
        )


class WidgetConversationView(_WidgetView):
    """Full conversation restore for a returning visitor. The iframe calls this on load
    before opening the socket so history is on-screen instantly.
    """

    def get(self, request):
        conv = _resolve_conversation(request)
        if conv is None:
            return Response({"error": "unauthorized"}, status=401)
        msgs = conv.messages.order_by("seq")
        return Response(
            {
                "conversation_id": str(conv.id),
                "last_seq": conv.last_seq,
                "status": conv.status,
                "assignee_name": (conv.assignee.name or conv.assignee.email) if conv.assignee_id else None,
                "results": MessageSerializer(msgs, many=True).data,
            }
        )


class WidgetKbSuggestView(_WidgetView):
    """Widget-visible KB search. Token-authed so the workspace derives from the token
    (I6): a stolen public_key cannot enumerate a workspace's KB by hitting this endpoint
    without also holding a valid signed session.
    """

    def get(self, request):
        conv = _resolve_conversation(request)
        if conv is None:
            return Response({"error": "unauthorized"}, status=401)
        q = (request.query_params.get("q") or "").strip()
        if len(q) < 2:
            return Response({"results": []})

        from apps.kb import search as kb_search

        articles = kb_search.search(workspace=conv.workspace, q=q, published_only=True, limit=3)
        results = [
            {
                "id": str(a.id),
                "title": a.title,
                "slug": a.slug,
                "url": f"/kb/{conv.workspace.slug}/a/{a.slug}/",
                "snippet": kb_search.snippet(a, q),
            }
            for a in articles
        ]
        return Response({"results": results})


class WidgetMessagesView(_WidgetView):
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
