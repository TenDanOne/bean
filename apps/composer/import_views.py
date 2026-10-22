"""Vista del importador por publicación (.zip -> borrador del composer). Lógica en ``importer.py``."""

import datetime as dt
import zoneinfo

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, QueryDict
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.members.channels import visible_accounts
from apps.members.decorators import require_permission
from apps.members.models import WorkspaceMembership
from apps.settings_manager.helpers import get_setting
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

from .delivery import A_MANO, BORRADOR, mode_of
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
from .models import Post
from .quality import BANNED_PHRASES_KEY, quality_warnings
from .views import _get_workspace, _require_may_edit, save_post

_LIVE = (SocialAccount.ConnectionStatus.CONNECTED, SocialAccount.ConnectionStatus.TOKEN_EXPIRING)
_TIPOS = {
    "video_vertical": "Video vertical", "video_horizontal": "Video horizontal", "imagen": "Imagen", "carrusel": "Carrusel",
}  # fmt: skip
_MODO_ES = {BORRADOR: "Borrador", A_MANO: "A mano"}


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


def _mmss(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}"


def _portadas(post, pps=None) -> dict | None:
    """Opciones del selector de portada: fotogramas (video; IG Reel y TikTok) o láminas (fotos de TikTok).

    Se calcula desde el post guardado, así la revisión y ``import_review`` (que valida contra esto) coinciden.
    «Del paquete» conserva los ms exactos por red que dejó el importador; no se reescribe si el usuario no cambia.
    """
    pps = pps if pps is not None else list(post.platform_posts.select_related("social_account"))
    pms = list(post.media_attachments.select_related("media_asset").order_by("position"))
    video = next((pm.media_asset for pm in pms if pm.media_asset.is_video), None)
    imgs = [pm for pm in pms if not pm.media_asset.is_video]
    plataformas = {pp.social_account.platform for pp in pps}
    extras = [pp.platform_extra or {} for pp in pps]
    if video is not None and video.duration >= 1:
        if not plataformas & {"instagram", "instagram_login", "tiktok"}:
            return None
        if any(e.get("ig_cover_asset_id") or e.get("thumbnail_asset_id") for e in extras):
            return None  # la imagen de portada del paquete manda sobre el fotograma
        d = int(video.duration)
        secs = sorted({0, d // 4, d // 2})
        ops = [{"v": s * 1000, "l": _mmss(s)} for s in secs]
        if any(e.get("thumb_offset") is not None or e.get("video_cover_timestamp_ms") is not None for e in extras):
            ops.insert(0, {"v": "paquete", "l": "Del paquete"})
        return {"tipo": "fotograma", "actual": 0, "opciones": ops}
    if video is None and len(imgs) > 1 and "tiktok" in plataformas:
        return {"tipo": "lámina", "actual": 0, "opciones": [{"v": i, "l": str(i + 1)} for i in range(len(imgs))]}
    return None


def _revision(post, piece, notices, n=1, total=1) -> dict:
    """Datos de la hoja de revisión rápida (lo que el importador acaba de crear), solo lectura."""
    ws = post.workspace
    tz = ws.effective_timezone or "UTC"
    pms = list(post.media_attachments.select_related("media_asset").order_by("position"))
    video = next((pm.media_asset for pm in pms if pm.media_asset.is_video), None)
    thumb = next((pm.media_asset for pm in pms if pm.media_asset.thumbnail or not pm.media_asset.is_video), None)
    thumb_url = ""
    if thumb is not None:
        thumb_url = thumb.thumbnail.url if thumb.thumbnail else (thumb.file.url if thumb.file else "")
    banned = get_setting(ws.id, BANNED_PHRASES_KEY, ws.organization_id) or []
    cuentas, avisos, ninos = [], [], None
    for pp in post.platform_posts.select_related("social_account").order_by("created_at"):
        acc, extra = pp.social_account, pp.platform_extra or {}
        m = extra.get("musica") or {}
        cuentas.append(
            {
                "plataforma": acc.platform,
                "red": acc.get_platform_display(),
                "nombre": acc.account_name,
                "tipo": pp.post_type_label or ("Video" if video else "Imagen"),
                "modo": _MODO_ES.get(mode_of(acc.platform, extra), "Directo"),
                "cancion": m.get("sugerencia", ""),
                "golpe": m.get("golpe_seg"),
            }
        )
        if acc.platform == "youtube" and extra.get("made_for_kids") is not None:
            ninos = {"cuenta": acc.account_name, "valor": "si" if extra["made_for_kids"] else "no"}
        for w in quality_warnings(
            platform=acc.platform,
            post_type=extra.get("post_type") or ("video" if acc.platform == "youtube" and video else ""),
            title=pp.platform_specific_title or post.title,
            caption=pp.effective_caption,
            first_comment=pp.effective_first_comment or "",
            duration=video.duration if video else None,
            width=video.width if video else None,
            height=video.height if video else None,
            banned_phrases=banned,
        ):
            avisos.append({"nivel": w["level"], "mensaje": w["message_es"], "red": acc.get_platform_display()})
    avisos += [{"nivel": "info", "mensaje": n, "red": ""} for n in notices]
    if post.internal_notes.startswith("Solo orgánico"):
        avisos.append({"nivel": "info", "mensaje": post.internal_notes.splitlines()[0], "red": ""})
    seg = piece.get("portada_seg")
    if piece.get("portada_ruta"):
        portada = "Imagen del paquete"
    elif seg is not None:
        portada = f"Fotograma en {str(seg).replace('.', ',')} s"
    else:
        portada = "Primer fotograma"
    manana = timezone.now().astimezone(zoneinfo.ZoneInfo(tz)) + dt.timedelta(days=1)
    return {
        "post_id": str(post.id),
        "pieza": [n, total],
        "portadas": _portadas(post),
        "tipo": _TIPOS.get(piece["tipo"], piece["tipo"]),
        "titulo": post.title or piece.get("titulo_alt") or piece["id"],
        "texto": post.caption[:240],
        "miniatura": thumb_url,
        "cuentas": cuentas,
        "ninos": ninos,
        "avisos": avisos,
        "portada": portada,
        "zona": tz,
        "fecha": manana.date().isoformat(),
        "hora": "09:00",
    }


@login_required
@require_POST
@require_permission("create_posts")
@require_permission("upload_media")
def import_package(request, workspace_id):
    """POST file (+ pieza, modo): responde JSON ``error`` | ``elegir`` | ``existe`` | ``redirect`` | ``revision``."""
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
    url = reverse("composer:compose_edit", kwargs={"workspace_id": target.id, "post_id": post.id})
    if request.POST.get("revisar"):  # hoja de revisión en el mismo diálogo; «Abrir en el editor» usa ``editor``
        return JsonResponse(
            {
                "revision": {
                    **_revision(post, piece, plan.notices, pieces.index(piece) + 1, len(pieces)),
                    "editor": url,
                    "espacio": str(target.id),
                }
            }
        )
    messages.info(request, text)
    response = JsonResponse({"redirect": url})
    response["HX-Redirect"] = url
    return response


@login_required
@require_POST
@require_permission("create_posts")
def import_review(request, workspace_id):
    """«Programar» / «Publicar ahora» desde la hoja de revisión: reutiliza ``save_post`` (mismas guardas y permisos)."""
    workspace = _get_workspace(request, workspace_id)
    post = get_object_or_404(
        Post.objects.for_workspace(workspace.id), id=request.POST.get("post_id", "").strip() or None
    )
    _require_may_edit(request, post)
    accion = {"programar": "schedule", "publicar": "publish_now"}.get(request.POST.get("accion", ""))
    if not accion:
        return JsonResponse({"errors": {"accion": "Acción no válida."}}, status=400)
    q = QueryDict(mutable=True)
    q.update(
        {
            "action": accion,
            "title": post.title,
            "caption": post.caption,
            "first_comment": post.first_comment,
            "internal_notes": post.internal_notes,
            "tags": ",".join(post.tags or []),
            "scheduled_date": request.POST.get("fecha", ""),
            "scheduled_time": request.POST.get("hora", ""),
        }
    )
    if post.category_id:
        q["category"] = str(post.category_id)
    pps = list(post.platform_posts.select_related("social_account"))
    q["selected_accounts"] = ",".join(str(pp.social_account_id) for pp in pps)
    cover = request.POST.get("portada", "").strip()
    if cover == "paquete":  # opción por defecto: conservar los ms por red del paquete
        cover = ""
    if cover:
        valid = {str(o["v"]) for o in (_portadas(post, pps) or {"opciones": []})["opciones"]} - {"paquete"}
        if cover not in valid:
            return JsonResponse({"errors": {"portada": "Esa portada no es válida para esta publicación."}}, status=400)
    cover = int(cover) if cover else None
    has_video = post.media_attachments.filter(media_asset__media_type="video").exists()
    for pp in pps:  # los overrides por red viajan en el formulario para que el control de marcadores {{…}} los revise
        for field, val in (
            ("title", pp.platform_specific_title),
            ("caption", pp.platform_specific_caption),
            ("comment", pp.platform_specific_first_comment),
        ):
            if val:
                q[f"override_{field}_{pp.social_account_id}"] = val
        extra, plat = pp.platform_extra or {}, pp.social_account.platform
        if cover is None:
            continue
        if has_video and plat == "tiktok" and extra.get("post_type") != "image":
            extra["video_cover_timestamp_ms"] = cover
        elif has_video and plat in ("instagram", "instagram_login") and extra.get("post_type") == "reel":
            if not extra.get("ig_cover_asset_id"):  # la imagen del paquete manda sobre el fotograma
                extra["thumb_offset"] = cover
        elif not has_video and plat == "tiktok" and extra.get("post_type") == "image":
            extra["photo_cover_index"] = cover
        else:
            continue
        pp.platform_extra = extra
        pp.save(update_fields=["platform_extra"])
    kids = {"si": True, "no": False}.get(request.POST.get("ninos", ""))
    for pp in pps:  # el bloque de YouTube de save_post reconstruye sus ajustes desde el formulario: se reenvían
        if pp.social_account.platform != "youtube":
            continue
        extra, i = pp.platform_extra or {}, str(pp.social_account_id)
        k = extra.get("made_for_kids") if kids is None else kids
        q.update(
            {
                f"yt_made_for_kids_{i}": "" if k is None else str(bool(k)).lower(),
                f"yt_privacy_status_{i}": extra.get("privacy_status", "public"),
                f"yt_tags_{i}": ",".join(extra.get("tags") or []),
                f"yt_thumbnail_asset_id_{i}": extra.get("thumbnail_asset_id") or "",
                f"yt_post_type_{i}": extra.get("post_type") or "",
            }
        )
    request.POST = q
    resp = save_post(request, workspace.id, post.id)
    if resp.status_code >= 400:
        return resp
    return JsonResponse({"ok": True, "post_id": str(post.id)})
