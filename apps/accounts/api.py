"""JSON API for auth introspection + team management. Thin DRF views: validate → call a
service → serialize. Tenancy/roles enforced by the permission classes; service-layer
guards do the rest. Explicit serializer fields only — hmac_secret is never exposed.
"""

from django.urls import reverse
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts import services
from apps.accounts.models import Role
from apps.accounts.permissions import IsWorkspaceAdmin, IsWorkspaceMember


class MemberSerializer(serializers.Serializer):
    id = serializers.UUIDField()                       # Membership.id (used by role/remove endpoints)
    user_id = serializers.UUIDField(source="user.id")  # User.id (used by conversation assign)
    email = serializers.EmailField(source="user.email")
    name = serializers.CharField(source="user.name")
    role = serializers.CharField()
    created_at = serializers.DateTimeField()


class InviteInputSerializer(serializers.Serializer):
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=Role.choices, default=Role.AGENT)


class RoleInputSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=Role.choices)


def _workspace_dict(workspace):
    return {
        "id": str(workspace.id),
        "name": workspace.name,
        "slug": workspace.slug,
        "public_key": workspace.public_key,  # safe to expose; hmac_secret is NOT
    }


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        u = request.user
        workspaces = [
            {
                "id": str(m.workspace_id),
                "name": m.workspace.name,
                "slug": m.workspace.slug,
                "role": m.role,
            }
            for m in u.memberships.select_related("workspace").order_by("created_at")
        ]
        return Response(
            {
                "user": {"id": str(u.id), "email": u.email, "name": u.name},
                "workspace": _workspace_dict(request.workspace) if request.workspace else None,
                "membership": (
                    {"id": str(request.membership.id), "role": request.membership.role}
                    if request.membership
                    else None
                ),
                "workspaces": workspaces,
            }
        )


class MembersView(APIView):
    permission_classes = [IsWorkspaceMember]

    def get(self, request):
        members = services.list_members(workspace=request.workspace)
        return Response(MemberSerializer(members, many=True).data)


class InviteView(APIView):
    permission_classes = [IsWorkspaceAdmin]

    def post(self, request):
        data = InviteInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        invite = services.create_invite(
            workspace=request.workspace,
            inviter=request.user,
            email=data.validated_data["email"],
            role=data.validated_data["role"],
        )
        accept_url = request.build_absolute_uri(reverse("invite_accept", args=[invite.token]))
        return Response(
            {"id": str(invite.id), "email": invite.email, "role": invite.role,
             "accept_url": accept_url},
            status=201,
        )


class MemberRoleView(APIView):
    permission_classes = [IsWorkspaceAdmin]

    def post(self, request, membership_id):
        data = RoleInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        membership = services.change_role(
            workspace=request.workspace,
            actor=request.membership,
            target_membership_id=str(membership_id),
            new_role=data.validated_data["role"],
        )
        return Response({"id": str(membership.id), "role": membership.role})


class MemberRemoveView(APIView):
    permission_classes = [IsWorkspaceAdmin]

    def delete(self, request, membership_id):
        services.remove_member(
            workspace=request.workspace,
            actor=request.membership,
            target_membership_id=str(membership_id),
        )
        return Response(status=204)


class WorkspaceSwitchView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        membership = services.set_selected_workspace(
            request=request, workspace_id=str(request.data.get("workspace_id", ""))
        )
        return Response({"workspace": _workspace_dict(membership.workspace)})
