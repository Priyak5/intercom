"""Template context processor: makes the current workspace + the switcher's membership
list available to every page without each view repeating the query. Guards anonymous.
"""


def workspace_context(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    return {
        "current_workspace": getattr(request, "workspace", None),
        "current_membership": getattr(request, "membership", None),
        "user_memberships": user.memberships.select_related("workspace").order_by("created_at"),
    }
