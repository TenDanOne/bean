from django.contrib import admin

from .models import OnboardingChecklist


@admin.register(OnboardingChecklist)
class OnboardingChecklistAdmin(admin.ModelAdmin):
    list_display = ("user", "workspace", "is_dismissed", "dismissed_at")
    list_filter = ("is_dismissed",)
    readonly_fields = ("id",)
