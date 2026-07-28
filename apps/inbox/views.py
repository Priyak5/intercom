"""Inbox page views (server-rendered shells; realtime is driven by socket.js)."""

from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render


@login_required
def inbox(request):
    if request.membership is None:
        return redirect("dashboard")
    return render(request, "inbox/inbox.html", {})


def widget_demo(request):
    """Dev-only visitor chat page for the Phase 2 reconnect gate. Reads the workspace
    public_key from ?key=. Phase 3 replaces this with the real embeddable iframe widget.
    """
    return render(request, "inbox/widget_demo.html", {"public_key": request.GET.get("key", "")})
