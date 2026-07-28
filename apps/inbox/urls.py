from django.urls import path

from apps.inbox import views

urlpatterns = [
    path("inbox/", views.inbox, name="inbox"),
    path("widget/demo/", views.widget_demo, name="widget_demo"),
]
