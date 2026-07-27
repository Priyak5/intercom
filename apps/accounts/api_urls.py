"""JSON API routes (mounted under /api/)."""

from django.urls import path

from apps.accounts import api

urlpatterns = [
    path("auth/me", api.MeView.as_view(), name="api_me"),
    path("members", api.MembersView.as_view(), name="api_members"),
    path("members/invite", api.InviteView.as_view(), name="api_invite"),
    path("members/<uuid:membership_id>/role", api.MemberRoleView.as_view(), name="api_member_role"),
    path("members/<uuid:membership_id>", api.MemberRemoveView.as_view(), name="api_member_remove"),
    path("workspace/switch", api.WorkspaceSwitchView.as_view(), name="api_workspace_switch"),
]
