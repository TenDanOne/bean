from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from apps.members.decorators import require_org_role
from apps.members.models import OrgMembership, WorkspaceMembership

from .models import Workspace


@login_required
def workspace_list(request):
    memberships = WorkspaceMembership.objects.filter(user=request.user).select_related("workspace")
    workspaces = [m.workspace for m in memberships if not m.workspace.is_archived]
    return render(request, "workspaces/list.html", {"workspaces": workspaces})


@login_required
@require_POST
@require_org_role(OrgMembership.OrgRole.ADMIN)
def workspace_create(request):
    """Create a new workspace in the user's organization."""
    name = request.POST.get("name", "").strip()
    if not name:
        return redirect("dashboard")

    if not request.org:
        return redirect("dashboard")

    workspace = Workspace.objects.create(
        organization=request.org,
        name=name,
    )

    WorkspaceMembership.objects.create(
        user=request.user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )

    # Set as current workspace
    request.user.last_workspace_id = workspace.id
    request.user.save(update_fields=["last_workspace_id"])

    return redirect("calendar:calendar", workspace_id=workspace.id)


@login_required
@require_http_methods(["GET", "POST"])
def workspace_settings(request, workspace_id):
    try:
        workspace = Workspace.objects.get(id=workspace_id)
    except Workspace.DoesNotExist:
        raise Http404 from None

    membership = WorkspaceMembership.objects.filter(user=request.user, workspace=workspace).first()
    if not membership:
        raise Http404

    # One key for every write on this page, replacing a hand-rolled role check.
    #
    # The role check guarded archiving and deleting and then the view *fell
    # through* to renaming and the logo with nothing guarding them at all — so
    # any member, a viewer included, could rename the brand and replace its
    # logo. The template disabled the field, which is decoration, not a check.
    is_owner_or_manager = membership.effective_permissions.get("manage_workspace_settings", False)

    if request.method == "POST":
        # Read once, up front: every branch below is a write, and a POST from
        # someone without the permission must not reach any of them.
        if not is_owner_or_manager:
            raise PermissionDenied("Tu rol no puede cambiar los ajustes de esta marca.")

        action = request.POST.get("action")

        if action == "archive_workspace":
            with transaction.atomic():
                active_count = (
                    Workspace.objects.select_for_update()
                    .filter(organization=workspace.organization, is_archived=False)
                    .count()
                )
                if active_count <= 1:
                    messages.error(request, "No se puede archivar el último espacio de trabajo de la organización.")
                    return redirect("workspaces:settings", workspace_id=workspace.id)
                workspace.is_archived = True
                workspace.save(update_fields=["is_archived"])
            messages.success(request, f'El espacio de trabajo "{workspace.name}" se archivó.')
            return redirect("organizations:workspaces")

        if action == "unarchive_workspace":
            workspace.is_archived = False
            workspace.save(update_fields=["is_archived"])
            messages.success(request, f'El espacio de trabajo "{workspace.name}" se restauró.')
            return redirect("workspaces:settings", workspace_id=workspace.id)

        if action == "delete_workspace":
            with transaction.atomic():
                active_count = (
                    Workspace.objects.select_for_update()
                    .filter(organization=workspace.organization, is_archived=False)
                    .count()
                )
                if active_count <= 1 and not workspace.is_archived:
                    messages.error(request, "No se puede eliminar el último espacio de trabajo de la organización.")
                    return redirect("workspaces:settings", workspace_id=workspace.id)
                workspace_name = workspace.name
                workspace.delete()
            messages.success(request, f'El espacio de trabajo "{workspace_name}" se eliminó de forma permanente.')
            return redirect("organizations:workspaces")

        if action == "banned_phrases":
            from apps.composer.quality import BANNED_PHRASES_KEY
            from apps.settings_manager.models import WorkspaceSetting

            phrases = list(
                dict.fromkeys(p.strip()[:120] for p in request.POST.get("banned_phrases", "").splitlines() if p.strip())
            )[:100]
            WorkspaceSetting.objects.update_or_create(
                workspace=workspace, key=BANNED_PHRASES_KEY, defaults={"value": phrases}
            )
            messages.success(request, "Frases prohibidas actualizadas.")
            return redirect("workspaces:settings", workspace_id=workspace.id)

        name = request.POST.get("name", "").strip()

        if name:
            workspace.name = name

        # Handle logo deletion
        if request.POST.get("delete_icon") == "1":
            if workspace.icon:
                workspace.icon.delete(save=False)
        # Handle logo upload
        elif "icon" in request.FILES:
            icon = request.FILES["icon"]

            # Validate file type
            allowed_types = ("image/jpeg", "image/png", "image/webp", "image/gif")
            if icon.content_type not in allowed_types:
                messages.error(request, "El logo debe ser una imagen JPEG, PNG, WebP o GIF.")
                return redirect("workspaces:settings", workspace_id=workspace.id)

            # Validate file size (2 MB max)
            max_size = 2 * 1024 * 1024
            if icon.size > max_size:
                messages.error(request, "El logo debe pesar menos de 2 MB.")
                return redirect("workspaces:settings", workspace_id=workspace.id)

            # Delete old icon before saving new one
            if workspace.icon:
                workspace.icon.delete(save=False)
            workspace.icon = icon

        workspace.save()
        messages.success(request, "Ajustes del espacio de trabajo actualizados.")
        return redirect("workspaces:settings", workspace_id=workspace.id)

    active_count = Workspace.objects.filter(organization=workspace.organization, is_archived=False).count()
    is_last_active = active_count <= 1 and not workspace.is_archived
    can_archive = is_owner_or_manager and not workspace.is_archived and not is_last_active
    can_delete = is_owner_or_manager and not is_last_active

    from apps.composer.quality import BANNED_PHRASES_KEY
    from apps.settings_manager.helpers import get_setting

    banned_phrases = get_setting(workspace.id, BANNED_PHRASES_KEY, workspace.organization_id) or []

    return render(
        request,
        "workspaces/settings.html",
        {
            "workspace": workspace,
            "settings_active": "general",
            "is_owner_or_manager": is_owner_or_manager,
            "can_archive": can_archive,
            "can_delete": can_delete,
            "banned_phrases_text": chr(10).join(banned_phrases),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def approvals_settings(request, workspace_id):
    try:
        workspace = Workspace.objects.get(id=workspace_id)
    except Workspace.DoesNotExist:
        raise Http404 from None

    membership = WorkspaceMembership.objects.filter(user=request.user, workspace=workspace).first()
    if not membership:
        raise Http404

    # Same key as workspace_settings — this is a workspace setting.
    is_owner_or_manager = membership.effective_permissions.get("manage_workspace_settings", False)

    if request.method == "POST":
        # PermissionDenied, not Http404: the page exists and they are looking at
        # it. A 404 here told them the wrong thing and produced the wrong page.
        if not is_owner_or_manager:
            raise PermissionDenied("Tu rol no puede cambiar el flujo de aprobación.")

        mode = request.POST.get("approval_workflow_mode", "")
        valid_modes = Workspace.ApprovalWorkflowMode.values
        if mode not in valid_modes:
            messages.error(request, "Modo de flujo de aprobación no válido.")
            return redirect("workspaces:approvals_settings", workspace_id=workspace.id)

        workspace.approval_workflow_mode = mode
        workspace.save(update_fields=["approval_workflow_mode", "updated_at"])
        messages.success(request, "Flujo de aprobación actualizado.")
        return redirect("workspaces:approvals_settings", workspace_id=workspace.id)

    return render(
        request,
        "workspaces/approvals_settings.html",
        {
            "workspace": workspace,
            "settings_active": "approvals",
            "is_owner_or_manager": is_owner_or_manager,
            "approval_modes": Workspace.ApprovalWorkflowMode,
        },
    )
