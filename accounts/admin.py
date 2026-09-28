from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import AuditLog, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    ordering = ["email"]
    list_display = ["email", "username", "is_staff", "created_at"]
    search_fields = ["email", "username"]
    fieldsets = DjangoUserAdmin.fieldsets + (("RootPulse", {"fields": ("github_id", "timezone")}),)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ["action", "user", "target_type", "target_id", "created_at"]
    list_filter = ["action"]
    search_fields = ["user__email", "target_id"]
    readonly_fields = [f.name for f in AuditLog._meta.fields]
