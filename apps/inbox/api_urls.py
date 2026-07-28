"""Agent inbox JSON API routes (mounted under /api/)."""

from django.urls import path

from apps.inbox import api, widget_api

urlpatterns = [
    # Minimal visitor surface (Phase 2; promoted to apps/widget in Phase 3).
    path("widget/session", widget_api.WidgetSessionView.as_view(), name="api_widget_session"),
    path("widget/messages", widget_api.WidgetMessagesView.as_view(), name="api_widget_messages"),
    path("conversations", api.ConversationListView.as_view(), name="api_conversations"),
    path(
        "conversations/<uuid:conversation_id>/messages",
        api.MessageListCreateView.as_view(),
        name="api_messages",
    ),
    path("conversations/<uuid:conversation_id>/read", api.ReadView.as_view(), name="api_conv_read"),
    path("conversations/<uuid:conversation_id>/assign", api.AssignView.as_view(), name="api_conv_assign"),
    path("conversations/<uuid:conversation_id>/status", api.StatusView.as_view(), name="api_conv_status"),
]
