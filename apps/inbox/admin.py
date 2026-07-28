"""Django-admin surfaces for inbox models. Currently: SummaryJob change-list +
per-day cost dashboard (Phase 7 "cost awareness" criterion).

Staff-only by Django-admin default; a per-workspace dashboard is a Phase 10
production concern documented in README Known Limitations.
"""

from django.conf import settings
from django.contrib import admin
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDay
from django.shortcuts import render
from django.urls import path

from apps.inbox.models import SummaryJob


@admin.register(SummaryJob)
class SummaryJobAdmin(admin.ModelAdmin):
    list_display = (
        "conversation", "status", "attempts",
        "input_tokens", "output_tokens", "latency_ms", "created_at",
    )
    list_filter = ("status",)
    search_fields = ("conversation__id",)
    readonly_fields = tuple(f.name for f in SummaryJob._meta.fields)
    ordering = ("-created_at",)

    change_list_template = "admin/inbox/summaryjob/change_list.html"

    def get_urls(self):
        base = super().get_urls()
        return [
            path(
                "spend/",
                self.admin_site.admin_view(self.spend_view),
                name="inbox_summaryjob_spend",
            ),
        ] + base

    def spend_view(self, request):
        """Per-workspace / per-day token totals + USD projection using the
        configured per-1M-token list prices.
        """
        rows = (
            SummaryJob.objects
            .annotate(day=TruncDay("created_at"))
            .values("conversation__workspace__slug", "day")
            .annotate(
                jobs=Count("id"),
                degraded=Count("id", filter=Q(status="degraded")),
                input_tokens=Sum("input_tokens"),
                output_tokens=Sum("output_tokens"),
            )
            .order_by("-day", "conversation__workspace__slug")
        )

        in_price = getattr(settings, "AI_PRICE_INPUT_USD_PER_MTOKEN", 1.0)
        out_price = getattr(settings, "AI_PRICE_OUTPUT_USD_PER_MTOKEN", 5.0)

        enriched = []
        totals = {"jobs": 0, "degraded": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0.0}
        for r in rows:
            in_tok = r["input_tokens"] or 0
            out_tok = r["output_tokens"] or 0
            usd = (in_tok / 1_000_000) * in_price + (out_tok / 1_000_000) * out_price
            r["usd"] = usd
            enriched.append(r)
            totals["jobs"] += r["jobs"]
            totals["degraded"] += r["degraded"]
            totals["input_tokens"] += in_tok
            totals["output_tokens"] += out_tok
            totals["usd"] += usd

        ctx = {
            **self.admin_site.each_context(request),
            "title": "AI summary spend",
            "rows": enriched,
            "totals": totals,
            "input_price": in_price,
            "output_price": out_price,
            "model": getattr(settings, "AI_MODEL", "?"),
        }
        return render(request, "admin/inbox/summaryjob/spend.html", ctx)
