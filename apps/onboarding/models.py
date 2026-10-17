import uuid

from django.conf import settings
from django.db import models


class OnboardingChecklist(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="onboarding_checklists",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="onboarding_checklists",
    )
    is_dismissed = models.BooleanField(default=False)
    dismissed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "onboarding_checklist"
        unique_together = [("user", "workspace")]

    def __str__(self):
        status = "dismissed" if self.is_dismissed else "active"
        return f"Checklist for {self.user} in {self.workspace} ({status})"
