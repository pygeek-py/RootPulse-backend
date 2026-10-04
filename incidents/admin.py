from django.contrib import admin

from .models import Incident, IncidentComment, IncidentEvent


class IncidentEventInline(admin.TabularInline):
    model = IncidentEvent
    extra = 0
    readonly_fields = ["kind", "metadata", "created_at"]
    can_delete = False


@admin.register(Incident)
class IncidentAdmin(admin.ModelAdmin):
    list_display = [
        "monitor",
        "reason",
        "started_at",
        "ended_at",
        "resolution",
        "excluded_from_reports",
    ]
    list_filter = ["resolution", "excluded_from_reports", "reason"]
    search_fields = ["monitor__name", "monitor__target"]
    inlines = [IncidentEventInline]


@admin.register(IncidentComment)
class IncidentCommentAdmin(admin.ModelAdmin):
    list_display = ["incident", "author", "created_at", "visible_on_status_page"]
