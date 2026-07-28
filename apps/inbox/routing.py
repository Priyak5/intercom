from django.urls import path

from apps.inbox.consumers import AgentConsumer, WidgetConsumer

websocket_urlpatterns = [
    path("ws/agent/", AgentConsumer.as_asgi()),
    path("ws/widget/", WidgetConsumer.as_asgi()),
]
