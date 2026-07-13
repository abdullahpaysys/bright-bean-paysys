"""Admin registration for the Slack analytics bot."""

from django.contrib import admin

from .models import SlackInboundEvent


@admin.register(SlackInboundEvent)
class SlackInboundEventAdmin(admin.ModelAdmin):
    list_display = ("event_id", "status", "team_id", "channel_id", "created_at")
    list_filter = ("status",)
    search_fields = ("event_id", "team_id", "channel_id", "user_id")
    readonly_fields = ("id", "created_at", "updated_at")
    ordering = ("-created_at",)
