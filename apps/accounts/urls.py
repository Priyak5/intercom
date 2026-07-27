"""Server-rendered dashboard/auth pages."""

from django.urls import path

from apps.accounts import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("signup", views.signup, name="signup"),
    path("login", views.login, name="login"),
    path("logout", views.logout, name="logout"),
    path("team", views.team, name="team"),
    path("workspace/switch", views.workspace_switch, name="workspace_switch"),
    path("invite/<str:token>/accept", views.invite_accept, name="invite_accept"),
]
