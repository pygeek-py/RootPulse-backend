from django.contrib import admin

from .models import ApiKey


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ["name", "prefix", "scope", "user", "last_used_at", "revoked_at"]
    exclude = ["key_hash"]
    readonly_fields = ["prefix", "last_used_at", "created_at"]
