"""Get-started checklist.

This module used to also host the connection-link flow: a magic link that let
someone outside BBS connect their own social accounts without having an account
here. It carried a second, parallel OAuth implementation — its own state signing,
its own callback, its own rate limiting — running alongside the normal connect
path in ``apps.social_accounts``. In BBS the owner connects every channel from
inside, so the whole flow is gone, and with it that duplication.
"""

import logging

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import OnboardingChecklist

logger = logging.getLogger(__name__)


@login_required
@require_GET
def checklist_partial(request, workspace_id):
    """Return the checklist partial for htmx polling."""
    from apps.onboarding.context_processors import onboarding_checklist

    ctx = onboarding_checklist(request)
    return render(request, "onboarding/partials/_checklist.html", ctx)


@login_required
@require_POST
def dismiss_checklist(request, workspace_id):
    """Dismiss the onboarding checklist for the current user + workspace."""
    OnboardingChecklist.objects.update_or_create(
        user=request.user,
        workspace_id=workspace_id,
        defaults={
            "is_dismissed": True,
            "dismissed_at": timezone.now(),
        },
    )

    if request.headers.get("HX-Request"):
        return HttpResponse(status=200)

    return redirect("calendar:calendar", workspace_id=workspace_id)
