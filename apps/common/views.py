"""Worker-less task processing: manual button + external cron endpoint."""

import hmac

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.common.background import count_pending_tasks, drain_pending_tasks
from apps.common.htmx import toast_response
from apps.members.decorators import require_org_role
from apps.members.models import OrgMembership


@login_required
@require_org_role(OrgMembership.OrgRole.ADMIN)
@require_POST
def process_pending(request):
    ran = drain_pending_tasks()
    left = count_pending_tasks()
    body = f"Quedan {left} pendientes (límite de tiempo); vuelve a procesar." if left else ""
    return toast_response(tone="warn" if left else "success", title=f"Se procesaron {ran} tareas", body=body)


@csrf_exempt
@require_POST
def cron_run(request):
    secret = settings.CRON_SECRET
    if not secret:
        return HttpResponse(status=503)
    if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), f"Bearer {secret}".encode()):
        return HttpResponse(status=401)
    ran = drain_pending_tasks()
    return JsonResponse({"ran": ran, "pending": count_pending_tasks()})
