"""Vista del importador por publicación (.zip -> borrador del composer). Lógica en ``importer.py``."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.members.channels import visible_accounts
from apps.members.decorators import require_permission
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

from .importer import (
    NET_NAMES,
    PackageError,
    create_draft,
    find_existing,
    load_manifest,
    open_package,
    piece_summary,
    plan_import,
)
from .views import _get_workspace

_LIVE = (SocialAccount.ConnectionStatus.CONNECTED, SocialAccount.ConnectionStatus.TOKEN_EXPIRING)


def _may_import(membership) -> bool:
    perms = membership.effective_permissions if membership else {}
    return bool(perms.get("create_posts") and perms.get("upload_media"))


def _target(request, workspace, manifest):
    """Espacio sugerido por el paquete si el usuario es miembro con permisos; si no, el actual."""
    name = (manifest.get("espacio_sugerido") or "").strip()
    if name and name.lower() != workspace.name.lower():
        cand = Workspace.objects.filter(organization=workspace.organization, name__iexact=name).first()
        m = WorkspaceMembership.objects.filter(user=request.user, workspace=cand).first() if cand else None
        if m and _may_import(m):
            return cand, m
    return workspace, request.workspace_membership


def _error(exc: PackageError):
    return JsonResponse({"error": {"titulo": exc.titulo, "mensaje": exc.mensaje}}, status=400)


@login_required
@require_POST
@require_permission("create_posts")
@require_permission("upload_media")
def import_package(request, workspace_id):
    """POST file (+ pieza, modo): responde JSON ``error`` | ``elegir`` | ``existe`` | ``redirect``."""
    workspace = _get_workspace(request, workspace_id)
    upload = request.FILES.get("file")
    if not upload:
        return _error(PackageError("No llegó ningún archivo.", "Elige un .zip y vuelve a intentarlo."))
    try:
        zf = open_package(upload)
        manifest = load_manifest(zf)
        pieces = manifest["piezas"]
        pid = request.POST.get("pieza", "")
        if not pid and len(pieces) > 1:
            return JsonResponse({"elegir": [piece_summary(p) for p in pieces]})
        piece = next((p for p in pieces if p["id"] == pid), None) if pid else pieces[0]
        if piece is None:
            raise PackageError("Esa publicación no está en el paquete.", f"No existe la pieza «{pid[:60]}».")
        target, membership = _target(request, workspace, manifest)
        accounts = list(
            visible_accounts(
                membership, SocialAccount.objects.for_workspace(target.id), capability="can_publish"
            ).filter(connection_status__in=_LIVE)
        )
        plan = plan_import(zf, piece, accounts)
        source = {"proyecto": manifest["proyecto"], "pieza_id": piece["id"]}
        perms = membership.effective_permissions
        existing = find_existing(
            target, source, lambda p: p.author_id == request.user.id or perms.get("edit_others_posts", False)
        )
        modo = request.POST.get("modo", "")
        if existing and not modo:
            return JsonResponse({"existe": {"id": piece["id"], "fecha": existing.created_at.strftime("%d/%m/%Y")}})
        post = create_draft(
            workspace=target,
            user=request.user,
            zf=zf,
            plan=plan,
            project=manifest["proyecto"],
            update=existing if modo == "actualizar" else None,
        )
    except PackageError as exc:
        return _error(exc)
    n = len(plan.targets)
    text = f"Importado: {n} {'red' if n == 1 else 'redes'} ({', '.join(NET_NAMES[k] for k in plan.targets)}). Revisa cada pestaña y elige cuándo publicar."
    if target.id != workspace.id:
        text += f" Espacio: {target.name}."
    if plan.notices:
        text += " Pendiente: " + " ".join(plan.notices)
    messages.info(request, text)
    url = reverse("composer:compose_edit", kwargs={"workspace_id": target.id, "post_id": post.id})
    response = JsonResponse({"redirect": url})
    response["HX-Redirect"] = url
    return response
