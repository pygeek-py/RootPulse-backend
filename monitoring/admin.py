from django.contrib import admin

from .models import AlertContact, MaintenanceWindow, Monitor, MonitorAlertContact


class MonitorAlertContactInline(admin.TabularInline):
    model = MonitorAlertContact
    extra = 0


@admin.register(Monitor)
class MonitorAdmin(admin.ModelAdmin):
    list_display = ["name", "type", "target", "status", "interval_seconds", "next_check_at", "user"]
    list_filter = ["type", "status"]
    search_fields = ["name", "target", "user__email"]
    readonly_fields = ["id", "heartbeat_token", "created_at", "updated_at"]
    inlines = [MonitorAlertContactInline]


@admin.register(AlertContact)
class AlertContactAdmin(admin.ModelAdmin):
    list_display = ["name", "channel", "user", "created_at"]
    list_filter = ["channel"]
    search_fields = ["name", "user__email"]


@admin.register(MaintenanceWindow)
class MaintenanceWindowAdmin(admin.ModelAdmin):
    list_display = ["name", "starts_at", "ends_at", "user"]
    search_fields = ["name", "user__email"]
