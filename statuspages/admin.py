from django.contrib import admin

from .models import (
    StatusPage,
    StatusPageAnnouncement,
    StatusPageComponent,
    StatusPageEmail,
    StatusPageSubscriber,
)


@admin.register(StatusPage)
class StatusPageAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "user", "is_public", "created_at"]
    exclude = ["password_hash"]  # never shown, even to staff


admin.site.register(StatusPageComponent)
admin.site.register(StatusPageAnnouncement)


@admin.register(StatusPageSubscriber)
class StatusPageSubscriberAdmin(admin.ModelAdmin):
    list_display = ["email", "page", "confirmed_at", "created_at"]
    exclude = ["confirm_token", "unsubscribe_token"]


@admin.register(StatusPageEmail)
class StatusPageEmailAdmin(admin.ModelAdmin):
    list_display = ["kind", "subscriber", "status", "attempt_count", "created_at"]
    readonly_fields = [f.name for f in StatusPageEmail._meta.fields]
