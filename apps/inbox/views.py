"""Inbox page views (server-rendered shells; realtime is driven by socket.js)."""

from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.clickjacking import xframe_options_exempt


@login_required
def inbox(request):
    if request.membership is None:
        return redirect("dashboard")
    return render(request, "inbox/inbox.html", {})


def widget_demo(request):
    """Dev-only visitor chat page for the Phase 2 reconnect gate. Kept alongside the
    Phase 3 iframe as a full-page smoke-test surface.
    """
    return render(request, "inbox/widget_demo.html", {"public_key": request.GET.get("key", "")})


@xframe_options_exempt
def widget_frame(request):
    """The real embeddable widget UI, served inside an iframe on a customer's site.
    xframe_options_exempt is required because Django's default X-Frame-Options: DENY
    would block the embed. Auth is handled inside the iframe by the visitor token flow.
    """
    return render(request, "inbox/widget_frame.html", {"public_key": request.GET.get("key", "")})


def widget_test(request):
    """Fake customer host page — the loader script is the only widget code here, so this
    exercises the same path a real embed would (bubble → lazy iframe → postMessage).
    Same-origin with the app, so it's not a CORS test; use `demo/index.html` for that.
    """
    return render(request, "inbox/widget_test.html", {"public_key": request.GET.get("key", "")})
