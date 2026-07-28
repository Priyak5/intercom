from django.urls import path

from apps.inbox import views

urlpatterns = [
    path("inbox/", views.inbox, name="inbox"),
    path("widget/demo/", views.widget_demo, name="widget_demo"),
    path("widget/frame/", views.widget_frame, name="widget_frame"),
    path("widget/test/", views.widget_test, name="widget_test"),
]
