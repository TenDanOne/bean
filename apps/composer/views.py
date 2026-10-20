"""Views for the Post Composer (F-2.1)."""

import base64
import contextlib
import io
import json
import logging
import re
import uuid
from datetime import datetime

import httpx
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, SuspiciousOperation, ValidationError
from django.core.files.base import ContentFile
from django.db import models, transaction
from django.http import FileResponse, Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.common.background import drain_pending_tasks
from apps.common.help_content import help_entries
from apps.common.validators import (
    parse_and_truncate_tag_string,
    parse_and_truncate_youtube_tag_string,
)
from apps.composer.builtin_templates import FORMAT_TEMPLATES
from apps.composer.quality import (
    BANNED_PHRASES_KEY,
    THUMBNAIL_MAX_BYTES,
    quality_warnings,
    visible_counter,
)
from apps.composer.services import media_duration_notice, media_error_for_platform
from apps.members.channels import visible_accounts
from apps.members.decorators import require_permission
from apps.members.models import WorkspaceMembership
from apps.publisher.engine import PublishEngine
from apps.settings_manager.helpers import get_setting
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from providers.tiktok import VALID_PRIVACY_LEVELS as TIKTOK_PRIVACY_LEVELS
from providers.youtube import YOUTUBE_CATEGORIES, YOUTUBE_CATEGORY_IDS, chapters_error

from .forms import ContentCategoryForm, PostForm
from .models import (
    ContentCategory,
    Idea,
    IdeaGroup,
    IdeaMedia,
    PlatformPost,
    Post,
    PostMedia,
    PostTemplate,
    PostVersion,
    Tag,
)

logger = logging.getLogger(__name__)

MAX_CSV_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB cap on CSV planner imports

# Shown when every posting slot within the lookahead horizon is already taken.
_QUEUE_FULL_MSG = (
    "No hay un horario de publicación libre dentro del periodo de programación: agrega horarios o libera uno."
)


def _get_workspace(request, workspace_id):
    """Resolve workspace and enforce membership check."""
    workspace = get_object_or_404(Workspace, id=workspace_id)
    if not request.user.is_authenticated:
        raise PermissionDenied("Se requiere autenticación.")
    has_membership = WorkspaceMembership.objects.filter(
        user=request.user,
        workspace=workspace,
    ).exists()
    if not has_membership:
        raise PermissionDenied("No eres miembro de este espacio de trabajo.")
    return workspace


def may_edit_post(request, post):
    """Whether this user may change someone's post: their own, or with the key.

    The same three lines were written out at four call sites and a fifth —
    ``post_delete`` — simply forgot them, so an editor could not *open* a
    colleague's post but could delete it outright from the same menu. Deleting
    being weaker than editing is the kind of gap that only shows up when the
    rules are restated rather than shared.

    Public because the templates need the same answer: an Edit button that leads
    to a refusal is what this is for.
    """
    membership = getattr(request, "workspace_membership", None)
    if post.author_id == request.user.id:
        return True
    permissions = membership.effective_permissions if membership else {}
    return bool(permissions.get("edit_others_posts", False))


def _require_may_edit(request, post):
    if not may_edit_post(request, post):
        raise PermissionDenied("Solo puedes modificar publicaciones que escribiste tú.")


def _is_valid_uuid(value):
    try:
        uuid.UUID(value)
    except (ValueError, TypeError):
        return False
    return True


def _preview_thumbnail_url(request, workspace, account):
    """URL of the custom thumbnail chosen for ``account`` in this form submission.

    The composer posts it as ``yt_thumbnail_asset_id_<account_id>`` — the same
    field ``_sync_platform_posts`` persists into ``platform_extra``. Scoped to the
    workspace so a crafted id can't pull an asset out of another tenant, and
    tolerant of a malformed id because this is a live-preview path that must never
    500 on half-typed input.
    """
    asset = _preview_thumbnail_asset(request, workspace, account)
    return asset.file.url if asset and asset.file else ""


def _preview_thumbnail_asset(request, workspace, account):
    """The workspace-scoped thumbnail asset posted for ``account``, or ``None`` (never raises)."""
    asset_id = request.POST.get(f"yt_thumbnail_asset_id_{account.id}", "").strip()
    if not asset_id:
        return None

    from apps.media_library.models import MediaAsset

    try:
        return MediaAsset.objects.filter(id=asset_id, workspace=workspace).first()
    except (ValueError, ValidationError):
        return None


def _workspace_asset_id(workspace, raw):
    """``raw`` as a string id when it is an asset this workspace may use (own or org-shared), else ``None``."""
    raw = (raw or "").strip()
    if not raw or not _is_valid_uuid(raw):
        return None
    from apps.media_library.models import MediaAsset

    ok = MediaAsset.objects.for_workspace_with_shared(workspace.id, workspace.organization_id).filter(id=raw).exists()
    return raw if ok else None


def _parse_selected_account_ids(raw):
    """Split a comma-separated ``selected_accounts`` value into account IDs.

    Non-UUID entries are dropped: they can only come from a crafted or
    corrupted request, and letting them reach a UUIDField lookup raises
    ValidationError (an unhandled 500) instead of being ignored.
    """
    return [s.strip() for s in (raw or "").split(",") if s.strip() and _is_valid_uuid(s.strip())]


def _get_account_scope(request):
    """Return the validated ``account_scope`` POST value, or ``None`` when unscoped.

    The hidden input is server-rendered from a UUID-validated ``?account=``
    param, so a malformed value means a crafted or corrupted request —
    reject it outright (HTTP 400) rather than guessing which rows to touch.
    """
    scope = request.POST.get("account_scope", "").strip()
    if not scope:
        return None
    if not _is_valid_uuid(scope):
        raise SuspiciousOperation("Malformed account_scope.")
    return scope


def _remove_deselected_platform_posts(request, post, selected_ids):
    """Delete PlatformPosts the user deselected in the composer form.

    When the composer was opened scoped to a single account
    (``?account=`` → hidden ``account_scope`` input), the form only renders
    that account, so ``selected_ids`` is NOT the complete desired set —
    restrict deletion to the scoped account to keep siblings intact.
    Published/publishing rows are never deleted by deselection (explicit
    post deletion remains the user's call — see PlatformPost.PROTECTED_STATUSES).
    """
    qs = post.platform_posts.exclude(social_account_id__in=selected_ids)
    scope = _get_account_scope(request)
    if scope:
        qs = qs.filter(social_account_id=scope)
    qs.exclude(status__in=PlatformPost.PROTECTED_STATUSES).delete()


def _existing_target_ids(post):
    """Account ids this post already targets, as strings.

    Used to protect targets a member cannot publish to from being removed as a
    side effect of them saving the post — see ``_sync_platform_posts``.
    """
    if post.pk is None:
        return []
    return [str(pk) for pk in post.platform_posts.values_list("social_account_id", flat=True)]


def _scoped_platform_post_ids(request, post):
    """PlatformPost IDs inside the composer's ``account_scope``, or ``None`` when unscoped."""
    scope = _get_account_scope(request)
    if not scope:
        return None
    return list(post.platform_posts.filter(social_account_id=scope).values_list("id", flat=True))


def _publishable_account_ids(request, workspace):
    """Ids (str) of the workspace accounts this member may post to (visible + ``can_publish``)."""
    return {
        str(pk)
        for pk in visible_accounts(
            request.workspace_membership,
            SocialAccount.objects.for_workspace(workspace.id),
            capability="can_publish",
        ).values_list("id", flat=True)
    }


def _is_read_only_published(post):
    """A post whose every platform copy is already published is history: no edits."""
    statuses = set(post.platform_posts.values_list("status", flat=True))
    return statuses == {PlatformPost.Status.PUBLISHED}


#: Nombre para personas (nunca el id interno tipo «instagram_login»).
PLATFORM_LABELS = {
    "youtube": "YouTube",
    "instagram": "Instagram",
    "instagram_login": "Instagram",
    "facebook": "Facebook",
    "tiktok": "TikTok",
}

#: Tipo elegido en el composer -> valor de ``platform_extra["post_type"]`` (los que entiende el motor).
#: «Video»/«Feed» no guardan nada: el motor lo deduce de los medios.
_POST_TYPE_FIELDS = {
    "youtube": ("yt_post_type_", {"short"}),
    "instagram": ("ig_post_type_", {"reel", "story", "carousel"}),
    "instagram_login": ("ig_post_type_", {"reel", "story", "carousel"}),
    "facebook": ("fb_post_type_", {"reel"}),
}


def _post_type_from_form(post_data, acc_id, platform) -> str | None:
    prefix, allowed = _POST_TYPE_FIELDS.get(platform, ("", set()))
    value = post_data.get(f"{prefix}{acc_id}", "").strip() if prefix else ""
    return value if value in allowed else None


_PLACEHOLDER_RE = re.compile(r"\{\{[^{}]*\}\}")

#: platform_extra keys de C2 (YouTube/Instagram/Facebook/«solo orgánico») que sobreviven a un guardado parcial.
_C2_EXTRA_KEYS = {
    "category_id", "playlist_id", "made_for_kids", "contains_synthetic_media", "chapters", "solo_organico",
    "ig_cover_asset_id", "thumb_offset", "collaborators", "share_to_feed", "link", "import_source", "photo_cover_index",
}  # fmt: skip
_IG_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_YT_PLAYLIST_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CHAPTER_LINE_RE = re.compile(r"^\s*((?:\d{1,2}:)?\d{1,2}:\d{2})\s+(.+?)\s*$")


def parse_chapters(raw):
    """Texto «mm:ss Título» por línea (o JSON ``[{t, title}]``) -> ``[{t, title}]``; líneas inválidas se omiten."""
    raw = (raw or "").strip()
    if raw.startswith("["):
        try:
            data = json.loads(raw)
            return [{"t": str(c.get("t", "")).strip(), "title": str(c.get("title", "")).strip()} for c in data][:50]
        except (ValueError, AttributeError, TypeError):
            return []
    rows = (_CHAPTER_LINE_RE.match(line) for line in raw.splitlines())
    return [{"t": m.group(1), "title": m.group(2)[:100]} for m in rows if m][:50]


def _apply_c2_extras(request, workspace, acc_id, platform, extra, prev=None):
    """Campos nuevos del composer por cuenta, validados y acotados al workspace.

    Solo se toca un campo si vino en el POST (un guardado parcial no borra lo anterior);
    un valor vacío lo quita. ``prev`` restaura claves C2 cuando otro bloque reemplazó el dict.
    """
    post = request.POST
    out = {**{k: v for k, v in (prev or {}).items() if k in _C2_EXTRA_KEYS}, **(extra or {})}

    def put(key, value):
        if value in (None, "", [], False):
            out.pop(key, None)
        else:
            out[key] = value

    def flag(name):
        return {"true": True, "false": False}.get(post.get(name, "").strip())

    if f"solo_organico_{acc_id}" in post:
        put("solo_organico", flag(f"solo_organico_{acc_id}"))

    if platform == "youtube":
        if f"yt_category_id_{acc_id}" in post:
            cat = post[f"yt_category_id_{acc_id}"].strip()
            put("category_id", cat if cat in YOUTUBE_CATEGORY_IDS else None)
        if f"yt_playlist_id_{acc_id}" in post:
            pl = post[f"yt_playlist_id_{acc_id}"].strip()
            put("playlist_id", pl if _YT_PLAYLIST_RE.match(pl) else None)
        if (kids := flag(f"yt_made_for_kids_{acc_id}")) is not None:
            out["made_for_kids"] = kids  # True y False son respuestas válidas; ausente = sin elegir
        if "made_for_kids" in out:
            out["self_declared_made_for_kids"] = out["made_for_kids"]
        if f"yt_synthetic_media_{acc_id}" in post:
            put("contains_synthetic_media", flag(f"yt_synthetic_media_{acc_id}"))
        if f"yt_chapters_{acc_id}" in post:
            put("chapters", parse_chapters(post[f"yt_chapters_{acc_id}"]))
    elif platform in ("instagram", "instagram_login"):
        if f"ig_cover_asset_id_{acc_id}" in post:
            put("ig_cover_asset_id", _workspace_asset_id(workspace, post[f"ig_cover_asset_id_{acc_id}"]))
        if f"ig_thumb_offset_{acc_id}" in post:
            raw = post[f"ig_thumb_offset_{acc_id}"].strip()
            put("thumb_offset", int(raw) if raw.isascii() and raw.isdigit() else None)
        if f"ig_collaborators_{acc_id}" in post:
            names = [n.strip().lstrip("@") for n in re.split(r"[,\s]+", post[f"ig_collaborators_{acc_id}"])]
            put("collaborators", list(dict.fromkeys(n for n in names if _IG_USERNAME_RE.match(n)))[:3])
        if (feed := flag(f"ig_share_to_feed_{acc_id}")) is not None:
            out["share_to_feed"] = feed
    elif platform == "facebook":
        if f"fb_post_type_{acc_id}" in post:
            put("post_type", _post_type_from_form(post, acc_id, platform))
        if f"fb_link_{acc_id}" in post:
            link = post[f"fb_link_{acc_id}"].strip()
            put("link", link if re.match(r"^https?://\S{1,2040}$", link) else None)
    return out


def _youtube_guard(request, workspace, post_id, account_ids):
    """400 JSON si un destino de YouTube no puede salir: «Hecho para niños» sin elegir, capítulos o miniatura inválidos.

    Lo usan programar/publicar/cola (``_scheduling_guard``) y el envío a aprobación, para que nada llegue a un
    aprobador con un dato que el servidor luego rechazaría.
    """
    from apps.media_library.models import MediaAsset

    youtube_ids = [
        str(i)
        for i in SocialAccount.objects.filter(id__in=account_ids, workspace=workspace, platform="youtube").values_list(
            "id", flat=True
        )
    ]
    for acc_id in youtube_ids:
        # «Hecho para niños» es obligatorio: nunca se asume un valor por omisión.
        kids = request.POST.get(f"yt_made_for_kids_{acc_id}", "").strip()
        if kids not in ("true", "false") and post_id and _is_valid_uuid(str(post_id)):
            known = (
                PlatformPost.objects.filter(post_id=post_id, social_account_id=acc_id)
                .values_list("platform_extra", flat=True)
                .first()
            )
            if (known or {}).get("made_for_kids") is not None:
                kids = "true"  # ya elegido en un guardado anterior
        if kids not in ("true", "false"):
            return JsonResponse(
                {"errors": {"made_for_kids": "YouTube exige indicar si el video es «Hecho para niños» (Sí o No)."}},
                status=400,
            )
        chapters_msg = chapters_error(parse_chapters(request.POST.get(f"yt_chapters_{acc_id}", "")))
        if chapters_msg:
            return JsonResponse({"errors": {"chapters": chapters_msg}}, status=400)
        # Misma regla que el revisor de calidad (aviso «block»): la miniatura no pasa de 2 MB.
        thumb_id = _workspace_asset_id(workspace, request.POST.get(f"yt_thumbnail_asset_id_{acc_id}", ""))
        if thumb_id:
            thumb = MediaAsset.objects.filter(id=thumb_id).first()
            if thumb and thumb.file_size and thumb.file_size > THUMBNAIL_MAX_BYTES:
                return JsonResponse(
                    {"errors": {"thumbnail": "La miniatura pesa más de 2 MB; YouTube la rechazará. Elige otra."}},
                    status=400,
                )
    return None


def _scheduling_guard(request, workspace, post_id, queue_id=None, *, check_approval=True):
    """400 JSON when this save may not go out: approval gate or media unfit for a platform.

    With mandatory approval, scheduling/queueing needs ``publish_directly``; everyone
    else must route the post through "Enviar a aprobación".
    """
    from apps.composer.services import _APPROVAL_MODES_BLOCKING_DIRECT_SCHEDULE

    if check_approval and workspace.approval_workflow_mode in _APPROVAL_MODES_BLOCKING_DIRECT_SCHEDULE:
        membership = request.workspace_membership
        perms = membership.effective_permissions if membership else {}
        if not perms.get("publish_directly", False):
            return JsonResponse(
                {"errors": {"approval": "Este espacio requiere aprobación: envía la publicación a aprobación."}},
                status=400,
            )

    # Un borrador puede guardar marcadores de plantilla; programar o publicar no.
    texts = [request.POST.get(k, "") for k in ("title", "caption", "first_comment")]
    texts += [v for k, v in request.POST.items() if k.startswith("override_")]
    if any(_PLACEHOLDER_RE.search(t) for t in texts):
        return JsonResponse(
            {
                "errors": {
                    "placeholders": "Hay marcadores {{…}} de la plantilla sin rellenar. Complétalos antes de programar o publicar."
                }
            },
            status=400,
        )

    from apps.media_library.models import MediaAsset

    media_rows = []  # (media_type, duration)
    if post_id:
        media_rows += list(
            PostMedia.objects.filter(post_id=post_id).values_list("media_asset__media_type", "media_asset__duration")
        )
    pending = request.session.get(f"pending_media_{workspace.id}", [])
    if pending:
        media_rows += list(
            MediaAsset.objects.filter(id__in=pending, workspace=workspace).values_list("media_type", "duration")
        )
    media_types = [t for t, _ in media_rows]
    video_durations = [d for t, d in media_rows if t == "video"]
    account_ids = set(_parse_selected_account_ids(request.POST.get("selected_accounts", "")))
    blocked = _youtube_guard(request, workspace, post_id, account_ids)
    if blocked is not None:
        return blocked
    targets = {
        (platform, _post_type_from_form(request.POST, str(acc_id), platform))
        for acc_id, platform in SocialAccount.objects.filter(id__in=account_ids, workspace=workspace).values_list(
            "id", "platform"
        )
    }
    if queue_id and _is_valid_uuid(queue_id):
        from apps.calendar.models import Queue

        q = Queue.objects.filter(id=queue_id, workspace=workspace).select_related("social_account").first()
        if q and q.social_account_id:
            targets.add((q.social_account.platform, None))
    errors = sorted(
        {e for e in (media_error_for_platform(p, media_types, t, video_durations) for p, t in targets) if e}
    )
    if errors:
        return JsonResponse({"errors": {"media": " ".join(errors)}}, status=400)
    return None


def _sync_platform_posts(request, post, workspace, initial_status=None):
    """Sync platform post selections from form data.

    When ``initial_status`` is given, newly-created PlatformPost rows are
    initialised to that status (e.g. ``"draft"``, ``"scheduled"``,
    ``"pending_review"``). Existing rows are not touched here — call
    ``_transition_post_children`` separately if you want to move them.
    """
    selected_ids = _parse_selected_account_ids(request.POST.get("selected_accounts", ""))

    # Every path that turns a selection into a target comes through here, which
    # is why the per-channel check goes here rather than in each caller. The
    # composer only offers channels they can see, but "can see" and "may post to"
    # are separate grants — and a hand-crafted POST never went through the form.
    allowed_ids = _publishable_account_ids(request, workspace)
    permitted_ids = [acc_id for acc_id in selected_ids if acc_id in allowed_ids]

    # Deletion is decided on the *unfiltered* selection on purpose. If a member
    # who cannot publish to a channel opens a post that already targets it,
    # filtering first would read as "deselected" and silently drop that target —
    # taking work away rather than declining to add it. They cannot create or
    # modify it; they also cannot destroy it by opening the page.
    keep_ids = list(dict.fromkeys(selected_ids + [i for i in _existing_target_ids(post) if i not in allowed_ids]))
    _remove_deselected_platform_posts(request, post, keep_ids)

    for acc_id in permitted_ids:
        try:
            account = SocialAccount.objects.get(id=acc_id, workspace=workspace)
        except SocialAccount.DoesNotExist:
            continue
        defaults = {}
        if initial_status:
            defaults["status"] = initial_status
        pp, _created = PlatformPost.objects.get_or_create(
            post=post,
            social_account=account,
            defaults=defaults,
        )
        # Solo se toca el override si el campo vino en el formulario: un guardado
        # parcial (sin esos inputs) no debe borrar lo personalizado antes.
        for field, attr in (
            ("title", "platform_specific_title"),
            ("caption", "platform_specific_caption"),
            ("comment", "platform_specific_first_comment"),
        ):
            key = f"override_{field}_{acc_id}"
            if key in request.POST:
                setattr(pp, attr, request.POST[key].strip() or None)

        # Per-platform extras
        prev_extra = dict(pp.platform_extra or {})
        post_type = _post_type_from_form(request.POST, acc_id, account.platform)
        if account.platform == "youtube":
            tags_list = parse_and_truncate_youtube_tag_string(request.POST.get(f"yt_tags_{acc_id}", ""))
            privacy_status = request.POST.get(f"yt_privacy_status_{acc_id}", "public")
            if privacy_status not in ("public", "unlisted", "private"):
                privacy_status = "public"
            thumb_id = _workspace_asset_id(workspace, request.POST.get(f"yt_thumbnail_asset_id_{acc_id}", ""))
            pp.platform_extra = {
                "privacy_status": privacy_status,
                "self_declared_made_for_kids": request.POST.get(f"yt_made_for_kids_{acc_id}") == "true",
                "tags": tags_list,
                "thumbnail_asset_id": thumb_id,
                **({"post_type": post_type} if post_type else {}),
            }

        elif account.platform in ("instagram", "instagram_login") and f"ig_post_type_{acc_id}" in request.POST:
            # Solo si el selector viajó en el formulario, para no borrar el tipo en otros guardados.
            extra = {k: v for k, v in (pp.platform_extra or {}).items() if k != "post_type"}
            if post_type:
                extra["post_type"] = post_type
            pp.platform_extra = extra

        elif account.platform == "pinterest":
            board_id = request.POST.get(f"pin_board_id_{acc_id}", "").strip()
            if not board_id:
                board_id = (pp.platform_extra or {}).get("board_id") or None
            pp.platform_extra = {
                "board_id": board_id,
                "link_url": request.POST.get(f"pin_link_url_{acc_id}", "").strip() or None,
                "alt_text": request.POST.get(f"pin_alt_text_{acc_id}", "").strip() or None,
                "tag_products": request.POST.get(f"pin_tag_products_{acc_id}", "").strip() or None,
                "allow_comments": request.POST.get(f"pin_allow_comments_{acc_id}") == "true",
                "show_similar_products": request.POST.get(f"pin_show_similar_{acc_id}") == "true",
                "cover_image_asset_id": request.POST.get(f"pin_cover_image_asset_id_{acc_id}", "").strip() or None,
            }

        elif account.platform == "tiktok" and f"tiktok_privacy_level_{acc_id}" in request.POST:
            # Only rebuild extras when the TikTok panel was part of the form,
            # so non-composer saves can't wipe a previously chosen privacy level.
            privacy = request.POST.get(f"tiktok_privacy_level_{acc_id}", "").strip()
            if privacy not in TIKTOK_PRIVACY_LEVELS:
                # An empty/invalid submit (required-validation bypassed) must
                # not wipe a previously saved choice.
                privacy = (pp.platform_extra or {}).get("privacy_level", "")
            # Comment / Duet / Stitch are independent interaction settings —
            # TikTok's UX guidelines require a separate toggle per interaction,
            # each greyed out on its own when the creator disabled it.
            extra = {
                **({"post_type": "image"} if prev_extra.get("post_type") == "image" else {}),  # modo fotos (importador)
                "disable_comment": request.POST.get(f"tiktok_allow_comment_{acc_id}") != "true",
                "disable_duet": request.POST.get(f"tiktok_allow_duet_{acc_id}") != "true",
                "disable_stitch": request.POST.get(f"tiktok_allow_stitch_{acc_id}") != "true",
                "brand_organic_toggle": request.POST.get(f"tiktok_brand_organic_{acc_id}") == "true",
                "brand_content_toggle": request.POST.get(f"tiktok_brand_content_{acc_id}") == "true",
                "is_aigc": request.POST.get(f"tiktok_is_aigc_{acc_id}") == "true",
            }
            if privacy:
                extra["privacy_level"] = privacy
            # Cover frame timestamp from the composer's frame picker; omitted
            # when blank/invalid so TikTok falls back to the first frame.
            # Parse with int() rather than str.isdigit() — isdigit() accepts
            # non-ASCII digits (e.g. "²", "١٢") that int() then rejects with an
            # unhandled ValueError.
            cover_ms = request.POST.get(f"tiktok_video_cover_timestamp_ms_{acc_id}", "").strip()
            if cover_ms:
                try:
                    cover_ms_val = int(cover_ms)
                except ValueError:
                    cover_ms_val = -1
                if cover_ms_val >= 0:
                    extra["video_cover_timestamp_ms"] = cover_ms_val
            pp.platform_extra = extra

        pp.platform_extra = _apply_c2_extras(
            request, workspace, acc_id, account.platform, pp.platform_extra, prev_extra
        )
        pp.save()


def _validate_pinterest_board_selection(request, post, workspace):
    """Selected Pinterest accounts need a board before composer save/submit."""
    selected_ids = _parse_selected_account_ids(request.POST.get("selected_accounts", ""))
    if not selected_ids:
        return None

    accounts = SocialAccount.objects.filter(id__in=selected_ids, workspace=workspace, platform="pinterest")
    for account in accounts:
        acc_id = str(account.id)
        board_id = request.POST.get(f"pin_board_id_{acc_id}", "").strip()
        if not board_id and post.pk:
            board_id = (
                PlatformPost.objects.filter(post=post, social_account=account)
                .values_list("platform_extra__board_id", flat=True)
                .first()
                or ""
            )
        if not board_id:
            return JsonResponse(
                {"errors": {"pinterest_board": f"Select a Pinterest board for {account.account_name}."}},
                status=400,
            )
    return None


def _save_version(post, user):
    """Create a PostVersion snapshot."""
    version_number = (post.versions.count()) + 1
    snapshot = {
        "title": post.title,
        "caption": post.caption,
        "first_comment": post.first_comment,
        "internal_notes": post.internal_notes,
        "tags": post.tags,
        "status": post.status,
        "scheduled_at": post.scheduled_at.isoformat() if post.scheduled_at else None,
        "platform_posts": [
            {
                "social_account_id": str(pp.social_account_id),
                "platform": pp.social_account.platform,
                "title_override": pp.platform_specific_title,
                "caption_override": pp.platform_specific_caption,
                "first_comment_override": pp.platform_specific_first_comment,
                "platform_extra": pp.platform_extra or {},
            }
            for pp in post.platform_posts.select_related("social_account")
        ],
        "media": [
            {
                "media_asset_id": str(pm.media_asset_id),
                "position": pm.position,
                "alt_text": pm.alt_text,
            }
            for pm in post.media_attachments.all()
        ],
    }
    PostVersion.objects.create(
        post=post,
        version_number=version_number,
        snapshot=snapshot,
        created_by=user,
    )


def _resolve_queues_for_post(queue_id, workspace, post_data):
    """Resolve the Queues to add a post to.

    Returns a list of Queue objects - one per selected social account when
    no explicit ``queue_id`` is supplied by the composer form. If an explicit
    queue_id is provided it is used exclusively.
    """
    from apps.calendar.models import Queue

    if queue_id:
        q = Queue.objects.filter(id=queue_id, workspace=workspace, is_active=True).first()
        return [q] if q else []

    account_ids = _parse_selected_account_ids(post_data.get("selected_accounts", ""))
    if not account_ids:
        return []

    queues = list(
        Queue.objects.filter(workspace=workspace, is_active=True, social_account_id__in=account_ids).order_by(
            "created_at"
        )
    )
    # De-duplicate by social_account (one queue per account)
    seen = set()
    unique = []
    for q in queues:
        if q.social_account_id in seen:
            continue
        seen.add(q.social_account_id)
        unique.append(q)
    return unique


def _resolve_template_data(template_id, workspace):
    """Resolve a ?template= value into a template_data dict.

    Accepts either a numeric ID for a built-in template (defined in
    apps.composer.builtin_templates.TEMPLATES) or a UUID for a saved
    PostTemplate row. Returns ``None`` if the value is missing, malformed,
    or does not match any template.
    """
    if not template_id:
        return None
    # Built-in templates use numeric IDs.
    try:
        numeric_id = int(template_id)
    except (TypeError, ValueError):
        numeric_id = None
    if numeric_id is not None:
        from apps.composer.builtin_templates import TEMPLATES as BUILTIN_TEMPLATES

        for tpl in BUILTIN_TEMPLATES:
            if tpl.get("id") == numeric_id:
                return {
                    "caption": tpl.get("body", ""),
                    "tags": list(tpl.get("tags", [])),
                    "formato": tpl.get("formato"),
                }
        return None
    # Fall back to PostTemplate UUID lookup.
    try:
        tpl = PostTemplate.objects.get(id=template_id, workspace=workspace)
    except (PostTemplate.DoesNotExist, ValidationError):
        return None
    return tpl.template_data


def _default_mode(request, post, prefill_is_proposed, initial_has_schedule, can_publish):
    """Modo del paso 3 al abrir el composer: ``now`` | ``schedule`` | ``draft`` (nunca uno que el rol no pueda usar)."""
    if request.GET.get("action") == "publish_now" and can_publish:
        return "now"
    if post is not None:
        return "schedule" if post.scheduled_at and not prefill_is_proposed else "draft"
    return "schedule" if initial_has_schedule else "draft"


def _infer_formato(template_data, platform_extras, char_limits):
    """Slug del formato (paso 1) que corresponde a la plantilla usada o a los tipos ya guardados; ``""`` si ninguno."""
    slug = ((template_data or {}).get("formato") or {}).get("slug")
    if slug:
        return slug
    types = {(char_limits.get(a, {}).get("platform"), (e or {}).get("post_type")) for a, e in platform_extras.items()}
    kinds = {t for _, t in types}
    if kinds & {"short", "reel"}:
        return "reel_short"
    if "carousel" in kinds:
        return "carrusel"
    if types and {p for p, _ in types} == {"youtube"}:
        return "youtube_largo"
    return ""


def _upload_limits():
    """Tope por tipo (bytes) y tipos aceptados, para validar al soltar el archivo antes de subirlo."""
    from apps.media_library.validators import ALL_ALLOWED_MIMES, MAX_FILE_SIZES

    return {
        "image": int(MAX_FILE_SIZES["image"]),
        "video": int(MAX_FILE_SIZES["video"]),
        "mimes": sorted(ALL_ALLOWED_MIMES),
    }


@login_required
@require_permission("create_posts")
def compose(request, workspace_id, post_id=None):
    """Render the full-page composer for creating or editing a post."""
    workspace = _get_workspace(request, workspace_id)

    # ?account= scopes the composer to one connected account (calendar links).
    # Validate it up front: a malformed value must behave as "unscoped" rather
    # than leak into UUID lookups (ValidationError → 500) or the hidden
    # account_scope input the save endpoints trust.
    account_filter = request.GET.get("account", "").strip()
    if account_filter and not _is_valid_uuid(account_filter):
        account_filter = ""

    # True when the Schedule Post panel is pre-filled from a draft's *proposed*
    # time rather than a committed schedule — the composer uses this to keep the
    # primary action as "Save Draft" instead of arming "schedule for real".
    schedule_prefill_is_proposed = False
    initial_has_schedule = False  # nuevo post abierto desde el «+» del calendario con fecha y hora

    # Load existing post or prepare a blank one
    if post_id:
        post = get_object_or_404(Post, id=post_id, workspace=workspace)
        _require_may_edit(request, post)
        form = PostForm(instance=post)
        # Prefer a committed schedule; fall back to a draft-stage proposal so
        # the Schedule Post panel shows whichever time the post carries.
        prefill_dt = post.scheduled_at or post.proposed_publish_at
        if prefill_dt:
            import zoneinfo

            tz = zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC")
            local_dt = prefill_dt.astimezone(tz)
            form.initial["scheduled_date"] = local_dt.strftime("%Y-%m-%d")
            form.initial["scheduled_time"] = local_dt.strftime("%H:%M")
            schedule_prefill_is_proposed = post.scheduled_at is None
        # One fetch serves selected ids, extras, and the status checks below.
        platform_post_list = list(post.platform_posts.select_related("social_account"))
        if account_filter:
            selected_account_ids = [
                pp.social_account_id for pp in platform_post_list if str(pp.social_account_id) == account_filter
            ]
        else:
            selected_account_ids = [pp.social_account_id for pp in platform_post_list]
        media_attachments = post.media_attachments.select_related("media_asset").all()
        platform_extras = {str(pp.social_account_id): (pp.platform_extra or {}) for pp in platform_post_list}
        platform_overrides = {
            str(pp.social_account_id): {
                "title": pp.platform_specific_title or "",
                "caption": pp.platform_specific_caption or "",
                "comment": pp.platform_specific_first_comment or "",
            }
            for pp in platform_post_list
        }
        template_data = None
    else:
        post = None
        # Pre-fill scheduled date/time from query params (e.g. when coming from calendar "+" CTA)
        initial = {}
        qs_date = request.GET.get("scheduled_date")
        qs_time = request.GET.get("scheduled_time")
        if qs_date:
            with contextlib.suppress(ValueError):
                initial["scheduled_date"] = datetime.strptime(qs_date, "%Y-%m-%d").date().isoformat()
        if qs_time:
            parsed_time = None
            for fmt in ("%H:%M", "%H:%M:%S"):
                try:
                    parsed_time = datetime.strptime(qs_time, fmt).time()
                    break
                except ValueError:
                    continue
            if parsed_time is not None:
                initial["scheduled_time"] = parsed_time.strftime("%H:%M")
        # Resolve ?template=<id> into template_data (supports both built-in int IDs
        # and PostTemplate UUIDs). Seed caption so it pre-fills the form.
        template_data = _resolve_template_data(request.GET.get("template"), workspace)
        if template_data and template_data.get("caption"):
            initial["caption"] = template_data["caption"]
        if template_data and template_data.get("first_comment"):
            initial["first_comment"] = template_data["first_comment"]
        if template_data and template_data.get("title"):
            initial["title"] = template_data["title"]
        initial_has_schedule = bool(initial.get("scheduled_date") and initial.get("scheduled_time"))
        form = PostForm(initial=initial)
        platform_post_list = []
        selected_account_ids = []
        media_attachments = []
        platform_extras = {}
        platform_overrides = {}

    # Clear any stale pending media from previous compose sessions.
    # Each compose page load starts fresh; the upload flow re-populates
    # the session as the user adds files.
    from apps.media_library.models import MediaAsset

    session_key = f"pending_media_{workspace.id}"
    request.session.pop(session_key, None)
    pending_assets = MediaAsset.objects.none()

    # Connected social accounts for this workspace
    social_accounts = (
        visible_accounts(
            request.workspace_membership,
            SocialAccount.objects.for_workspace(workspace.id),
        )
        .filter(
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        .order_by("platform", "account_name")
    )

    # A saved template also brings its accounts (only ones this member can see) and media.
    if template_data and not post_id and not account_filter:
        visible_ids = {str(pk) for pk in social_accounts.values_list("id", flat=True)}
        selected_account_ids = [a for a in template_data.get("platform_account_ids") or [] if a in visible_ids]
        template_media_ids = [m for m in template_data.get("media_asset_ids") or [] if _is_valid_uuid(m)]
        if template_media_ids:
            by_id = {str(a.id): a for a in MediaAsset.objects.filter(id__in=template_media_ids, workspace=workspace)}
            ordered = [by_id[m] for m in template_media_ids if m in by_id]
            request.session[session_key] = [str(a.id) for a in ordered]
            pending_assets = ordered
        # Per-account settings and custom texts for the accounts that were kept.
        for acc_id in selected_account_ids:
            extra = (template_data.get("platform_extras") or {}).get(acc_id)
            if extra:
                platform_extras[acc_id] = dict(extra)
            override = (template_data.get("platform_overrides") or {}).get(acc_id)
            if override:
                platform_overrides[acc_id] = override

    # When opening from calendar with a specific account, show only that account.
    if account_filter:
        social_accounts = social_accounts.filter(id=account_filter)
        if not post_id and social_accounts.exists():
            selected_account_ids = [account_filter]

    # Platform character limits for JS
    char_limits = {}
    for acc in social_accounts:
        acc.platform_label = PLATFORM_LABELS.get(acc.platform) or acc.get_platform_display()
        cfg = dict(acc.field_config)
        cfg["supports_first_comment"] = acc.supports_first_comment()
        char_limits[str(acc.id)] = {
            "platform": acc.platform,
            "platform_label": PLATFORM_LABELS.get(acc.platform) or acc.get_platform_display(),
            "limit": acc.char_limit,
            "name": acc.account_name or acc.account_handle,
            **cfg,
        }

    # Workspace defaults
    default_first_comment = workspace.default_first_comment
    default_hashtags = workspace.default_hashtags

    # Categories for dropdown
    categories = ContentCategory.objects.for_workspace(workspace.id)

    # Queues for "Add to Queue" action
    from apps.calendar.models import Queue

    queues = (
        Queue.objects.for_workspace(workspace.id).filter(is_active=True).select_related("social_account", "category")
    )

    # Permissions for action buttons
    membership = request.workspace_membership
    perms = membership.effective_permissions if membership else {}
    can_publish = perms.get("publish_directly", False)
    can_approve = perms.get("approve_posts", False)
    ws_role = membership.workspace_role if membership else None
    can_view_internal_notes = ws_role not in ("client", "viewer") if ws_role else True

    # Approval workflow context
    workflow_mode = workspace.approval_workflow_mode
    show_resubmit_button = any(pp.status in ("changes_requested", "rejected", "approved") for pp in platform_post_list)
    # Fresh drafts get "Submit for Approval"; posts already in the workflow
    # (changes-requested / rejected / approved-but-edited) get "Resubmit" instead.
    show_submit_button = workflow_mode != "none" and not show_resubmit_button
    # Once the post is committed to publishing, the Schedule Post panel re-times
    # a live schedule; while still a draft it captures a *proposed* time on save.
    # Mirror _capture_proposed_publish_at's guard exactly (scheduled_at OR a
    # committed child) so the hint, the JS schedule-arming, and the save path
    # never disagree.
    post_is_scheduled = post is not None and (
        post.scheduled_at is not None
        or any(pp.status in ("scheduled", "publishing", "published") for pp in platform_post_list)
    )

    # Approval history and comments for existing posts
    approval_history = []
    post_comments = []
    latest_feedback = None
    if post:
        from apps.approvals.models import ApprovalAction

        # Show recent history (bounded) — a heavily-cycled post can accumulate
        # unboundedly many actions; 50 is plenty for the timeline.
        approval_history = list(
            ApprovalAction.objects.filter(post=post).select_related("user").order_by("-created_at")[:50]
        )
        # Most recent reviewer feedback to surface in the edit banner.
        latest_feedback = next(
            (a for a in approval_history if a.action in ("changes_requested", "rejected") and a.comment),
            None,
        )
        from apps.approvals.comments import get_comments_for_post

        post_comments = get_comments_for_post(post, request.user)

    # Workspace tags for the tag dropdown
    all_tags = Tag.objects.for_workspace(workspace.id)

    # Failed platform posts with an error message — shown as a banner so the
    # user can see why a publish failed before retrying.
    failed_platform_posts = [
        pp for pp in platform_post_list if pp.status == PlatformPost.Status.FAILED and pp.publish_error
    ]

    # Build media_items for the initial preview render
    media_items = []
    for att in media_attachments:
        asset = att.media_asset
        media_items.append(
            {
                "url": asset.file.url if asset.file else "",
                "is_video": asset.is_video,
                "filename": asset.filename,
            }
        )
    if not media_items:
        for asset in pending_assets:
            media_items.append(
                {
                    "url": asset.file.url if asset.file else "",
                    "is_video": asset.is_video,
                    "filename": asset.filename,
                }
            )

    # Resolve thumbnail/cover image URLs for per-account assets already saved
    thumb_ids = [
        extra.get("thumbnail_asset_id") for extra in platform_extras.values() if extra.get("thumbnail_asset_id")
    ]
    cover_ids = [
        extra.get("cover_image_asset_id") for extra in platform_extras.values() if extra.get("cover_image_asset_id")
    ]
    all_asset_ids = [aid for aid in thumb_ids + cover_ids if aid]
    asset_url_map = {}
    if all_asset_ids:
        for asset in MediaAsset.objects.filter(id__in=all_asset_ids, workspace=workspace):
            url = ""
            if asset.thumbnail:
                url = asset.thumbnail.url
            elif asset.file:
                url = asset.file.url
            asset_url_map[str(asset.id)] = url
    for _acc_id, extra in platform_extras.items():
        tid = extra.get("thumbnail_asset_id")
        if tid and tid in asset_url_map:
            extra["thumbnail_url"] = asset_url_map[tid]
        elif tid:
            extra["thumbnail_asset_id"] = None  # not usable in this workspace (e.g. stale or foreign template)
        cid = extra.get("cover_image_asset_id")
        if cid and cid in asset_url_map:
            extra["cover_image_url"] = asset_url_map[cid]

    context = {
        "workspace": workspace,
        "post": post,
        "form": form,
        "social_accounts": social_accounts,
        "selected_account_ids": [str(aid) for aid in selected_account_ids],
        "platform_extras": platform_extras,
        "published_targets": [
            pp for pp in platform_post_list if pp.status == PlatformPost.Status.PUBLISHED and pp.platform_post_id
        ],
        "platform_overrides": platform_overrides,
        "media_attachments": media_attachments,
        "media_items": media_items,
        "char_limits": char_limits,
        "default_first_comment": default_first_comment,
        "default_hashtags": json.dumps(default_hashtags),
        "can_publish": can_publish,
        "can_approve": can_approve,
        "can_view_internal_notes": can_view_internal_notes,
        "is_edit": post is not None,
        "schedule_prefill_is_proposed": schedule_prefill_is_proposed,
        "post_is_scheduled": post_is_scheduled,
        "categories": categories,
        "queues": queues,
        "template_data_json": json.dumps(template_data) if template_data else "null",
        "initial_tags": post.tags if post else (template_data or {}).get("tags") or [],
        "initial_category_id": str(post.category_id)
        if post and post.category_id
        else (template_data or {}).get("category_id") or "",
        "workflow_mode": workflow_mode,
        "show_submit_button": show_submit_button,
        "show_resubmit_button": show_resubmit_button,
        "approval_history": approval_history,
        "latest_feedback": latest_feedback,
        "post_comments": post_comments,
        "pending_assets": pending_assets,
        "all_tags": all_tags,
        # When opened scoped to one account (?account=), the form only renders
        # that account — the save endpoints use this to leave siblings alone.
        "account_scope": account_filter if (post_id and account_filter) else "",
        "failed_platform_posts": failed_platform_posts,
        "unsplash_enabled": bool(settings.UNSPLASH_ACCESS_KEY),
        "youtube_categories": YOUTUBE_CATEGORIES,
        "format_templates": [
            {k: t[k] for k in ("id", "title", "description", "body", "tags", "formato")} for t in FORMAT_TEMPLATES
        ],
        "workspace_timezone": workspace.effective_timezone or "UTC",
        "initial_formato": _infer_formato(template_data, platform_extras, char_limits),
        "default_mode": _default_mode(request, post, schedule_prefill_is_proposed, initial_has_schedule, can_publish),
        "upload_limits": _upload_limits(),
        "help_entries": help_entries(),
    }
    return render(request, "composer/compose.html", context)


def _transition_post_children(post, target, *, allow_via_draft=True, only=None):
    """Transition every (or selected) PlatformPost child of *post* to *target*.

    Returns ``(moved, skipped)`` lists of PlatformPost instances. Children
    already in the target state are left alone (counted as moved). For states
    that don't allow a direct transition, an intermediate hop through
    ``draft`` is attempted when ``allow_via_draft`` is True.

    ``only`` may be an iterable of PlatformPost IDs to restrict the operation
    to a subset of children.
    """
    children = post.platform_posts.all()
    if only is not None:
        only_ids = {str(x) for x in only}
        children = [pp for pp in children if str(pp.id) in only_ids]
    moved, skipped = [], []
    for pp in children:
        if pp.status == target:
            moved.append(pp)
            continue
        try:
            if pp.can_transition_to(target):
                pp.transition_to(target)
            elif allow_via_draft and pp.can_transition_to("draft") and target != "draft":
                pp.transition_to("draft")
                if pp.can_transition_to(target):
                    pp.transition_to(target)
                else:
                    skipped.append(pp)
                    continue
            else:
                skipped.append(pp)
                continue
            pp.save(update_fields=["status", "published_at", "updated_at"])
            moved.append(pp)
        except ValueError:
            skipped.append(pp)
    return moved, skipped


def _base_content_snapshot(post):
    """Reviewable base content used to detect edits to an approved post."""
    return (post.title, post.caption, post.first_comment, tuple(post.tags or []))


def _revert_approved_to_review(post):
    """Option A: editing an approved post's content sends it back for re-approval.

    Silently reverts any ``approved`` children to ``pending_review`` so edited
    content can't publish without a fresh review. (The explicit "Resubmit for
    review" button additionally notifies reviewers; this is the safety net for
    plain saves/autosaves.) Returns the reverted children.
    """
    reverted = []
    for pp in post.platform_posts.all():
        if pp.status == "approved" and pp.can_transition_to("pending_review"):
            pp.transition_to("pending_review")
            pp.save(update_fields=["status", "published_at", "updated_at"])
            reverted.append(pp)
    return reverted


def _platform_status_map(post):
    """Return ``{platform_post_id: status}`` for HTMX response headers."""
    return {str(pp.id): pp.status for pp in post.platform_posts.all()}


def _combine_schedule_dt(workspace, sched_date, sched_time):
    """Combine the composer's date + time inputs into a workspace-tz-aware datetime.

    Returns ``None`` unless both parts are present. Shared by the real
    schedule (``action='schedule'``) and the draft-stage proposed time so both
    interpret the same Schedule Post panel inputs identically.
    """
    if not (sched_date and sched_time):
        return None
    import zoneinfo

    tz = zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC")
    return datetime.combine(sched_date, sched_time).replace(tzinfo=tz)


def _capture_proposed_publish_at(post, post_id, workspace, form, *, clear_when_blank=True):
    """Set ``post.proposed_publish_at`` from the composer's Schedule Post panel.

    Used by the draft-stage save actions (save draft, submit/resubmit for
    approval) so a proposed time entered in the panel is persisted and shown in
    the drafts/approval lists. Mutates ``post`` in place; the caller persists it.

    A no-op once the post is committed to publishing — ``post.scheduled_at`` is
    set or any child is scheduled/publishing/published — because then the
    date/time inputs reflect the live schedule (pre-filled from ``scheduled_at``)
    and must not be reinterpreted as a proposal.

    ``clear_when_blank`` (default True, for Save Draft where the panel *is* the
    proposed-time editor) clears the proposal when the panel is empty. The
    approval submit/resubmit actions pass False so routing a draft through
    approval with an untouched/absent panel can't silently wipe a proposal an
    agent set via the REST/MCP API.
    """
    already_scheduled = post.scheduled_at is not None or (
        bool(post_id) and post.platform_posts.filter(status__in=["scheduled", "publishing", "published"]).exists()
    )
    if already_scheduled:
        return
    proposed = _combine_schedule_dt(
        workspace,
        form.cleaned_data.get("scheduled_date"),
        form.cleaned_data.get("scheduled_time"),
    )
    if proposed is None and not clear_when_blank:
        return
    post.proposed_publish_at = proposed


@login_required
@require_permission("create_posts")
@require_POST
def save_post(request, workspace_id, post_id=None):
    """Save or update a post (draft, schedule, or publish action)."""
    workspace = _get_workspace(request, workspace_id)
    action = request.POST.get("action", "save_draft")

    if post_id:
        post = get_object_or_404(Post, id=post_id, workspace=workspace)
        _require_may_edit(request, post)
        if _is_read_only_published(post):
            return JsonResponse(
                {"errors": {"post": "Esta publicación ya se publicó y no se puede editar."}}, status=400
            )
        _orig_content = _base_content_snapshot(post)
        form = PostForm(request.POST, instance=post)
    else:
        _orig_content = None
        form = PostForm(request.POST)

    if not form.is_valid():
        return JsonResponse({"errors": form.errors}, status=400)

    post = form.save(commit=False)
    post.workspace = workspace
    if not post_id:
        post.author = request.user

    pinterest_board_error = _validate_pinterest_board_selection(request, post, workspace)
    if pinterest_board_error is not None:
        return pinterest_board_error

    # Handle action — note that Post itself no longer carries an editorial
    # status: every transition below operates on the PlatformPost children,
    # which is why we sync those before/after running it.
    pending_target = None  # what to transition existing children to after sync
    initial_status = "draft"  # default status for newly created PlatformPosts

    if action in ("submit_for_approval", "resubmit_for_approval"):
        blocked = _youtube_guard(
            request, workspace, post_id, set(_parse_selected_account_ids(request.POST.get("selected_accounts", "")))
        )
        if blocked is not None:
            return blocked

    if action in ("schedule", "publish_now", "add_to_queue", "add_to_queue_priority"):
        blocked = _scheduling_guard(
            request,
            workspace,
            post_id,
            request.POST.get("queue_id") if action.startswith("add_to_queue") else None,
            check_approval=action != "publish_now",
        )
        if blocked is not None:
            return blocked

    if action == "schedule":
        aware_dt = _combine_schedule_dt(
            workspace,
            form.cleaned_data.get("scheduled_date"),
            form.cleaned_data.get("scheduled_time"),
        )
        if aware_dt:
            if aware_dt <= timezone.now():
                return JsonResponse(
                    {"errors": {"schedule": "La fecha programada debe ser futura."}},
                    status=400,
                )
            post.scheduled_at = aware_dt
            # A committed schedule supersedes any draft-stage proposal.
            post.proposed_publish_at = None
            # Propagate the manually chosen time to every PlatformPost so all
            # selected platforms publish at the same moment.
            post._schedule_propagate_dt = aware_dt  # handled after post.save()
            pending_target = "scheduled"
            initial_status = "scheduled"
        else:
            return JsonResponse({"errors": {"schedule": "Se requiere fecha y hora."}}, status=400)
    elif action == "publish_now":
        # Server-side permission check - only roles with publish_directly can bypass approval
        membership = request.workspace_membership
        perms = membership.effective_permissions if membership else {}
        if not perms.get("publish_directly", False):
            raise PermissionDenied("No tienes permiso para publicar directamente.")
        now_dt = timezone.now()
        post.scheduled_at = now_dt
        post.proposed_publish_at = None
        post._schedule_propagate_dt = now_dt  # handled after post.save()
        pending_target = "scheduled"
        initial_status = "scheduled"
    elif action == "add_to_queue":
        from django.db import transaction

        from apps.calendar.services import QueueFullError, add_to_queue

        queue_id = request.POST.get("queue_id")
        queues = _resolve_queues_for_post(queue_id, workspace, request.POST)
        if not queues:
            return JsonResponse(
                {"errors": {"queue": "No se encontró una cola activa para el canal seleccionado."}}, status=400
            )
        # Queueing assigns real per-platform slots below — drop any proposal.
        post.proposed_publish_at = None
        post.save()
        # Ensure PlatformPost rows exist for every selected account before the
        # queue service writes per-platform scheduled_at values.
        _sync_platform_posts(request, post, workspace, initial_status="draft")
        # "Next Available" always places the post in the queue's soonest open
        # slot. It deliberately ignores the Schedule-panel date/time: those
        # inputs are prefilled from the post's own scheduled_at/proposed time
        # when editing, and treating them as a floor would push the post past
        # the true next slot.
        try:
            # One transaction across every queue: if a later queue is full, the
            # earlier queues' slot writes roll back instead of leaving a child
            # half-queued.
            with transaction.atomic():
                for q in queues:
                    add_to_queue(post, q)
                # Transition every child whose scheduled_at was filled in.
                _transition_post_children(post, "scheduled", only=_scoped_platform_post_ids(request, post))
        except QueueFullError:
            return JsonResponse({"errors": {"queue": _QUEUE_FULL_MSG}}, status=400)
        _save_version(post, request.user)
        if request.htmx:
            return HttpResponse(
                status=204,
                headers={
                    "HX-Trigger": json.dumps({"postSaved": {"postId": str(post.id), "status": post.status}}),
                    "X-Platform-Statuses": json.dumps(_platform_status_map(post)),
                },
            )
        return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post.id)
    elif action == "add_to_queue_priority":
        from django.db import transaction

        from apps.calendar.services import QueueFullError, add_to_queue

        queue_id = request.POST.get("queue_id")
        queues = _resolve_queues_for_post(queue_id, workspace, request.POST)
        if not queues:
            return JsonResponse(
                {"errors": {"queue": "No se encontró una cola activa para el canal seleccionado."}}, status=400
            )
        # Queueing assigns real per-platform slots below — drop any proposal.
        post.proposed_publish_at = None
        post.save()
        _sync_platform_posts(request, post, workspace, initial_status="draft")
        try:
            # One transaction across every queue (see add_to_queue above).
            with transaction.atomic():
                for q in queues:
                    add_to_queue(post, q, priority=True)
                _transition_post_children(post, "scheduled", only=_scoped_platform_post_ids(request, post))
        except QueueFullError:
            return JsonResponse({"errors": {"queue": _QUEUE_FULL_MSG}}, status=400)
        _save_version(post, request.user)
        if request.htmx:
            return HttpResponse(
                status=204,
                headers={
                    "HX-Trigger": json.dumps({"postSaved": {"postId": str(post.id), "status": post.status}}),
                    "X-Platform-Statuses": json.dumps(_platform_status_map(post)),
                },
            )
        return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post.id)
    elif action == "submit_for_approval":
        # Save post first so it has a PK, then delegate to approval service
        _capture_proposed_publish_at(post, post_id, workspace, form, clear_when_blank=False)
        post.save()
        # Sync platform posts before submitting
        _sync_platform_posts(request, post, workspace, initial_status="draft")
        _save_version(post, request.user)
        from apps.approvals.services import submit_for_review

        submit_for_review(post, request.user, workspace)
        if request.htmx:
            return HttpResponse(
                status=204,
                headers={
                    "HX-Trigger": json.dumps({"postSaved": {"postId": str(post.id), "status": post.status}}),
                    "X-Platform-Statuses": json.dumps(_platform_status_map(post)),
                },
            )
        return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post.id)
    elif action == "resubmit_for_approval":
        # Resubmit after changes requested or rejection
        _capture_proposed_publish_at(post, post_id, workspace, form, clear_when_blank=False)
        post.save()
        _sync_platform_posts(request, post, workspace, initial_status="draft")
        _save_version(post, request.user)
        from apps.approvals.services import resubmit_post

        resubmit_post(post, request.user, workspace)
        if request.htmx:
            return HttpResponse(
                status=204,
                headers={
                    "HX-Trigger": json.dumps({"postSaved": {"postId": str(post.id), "status": post.status}}),
                    "X-Platform-Statuses": json.dumps(_platform_status_map(post)),
                },
            )
        return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post.id)
    elif action == "save_draft":
        # The Schedule Post panel doubles as the proposed-time picker for a
        # draft; capture it (or clear it when blank) before the save below.
        _capture_proposed_publish_at(post, post_id, workspace, form)
    # Fall through (save_draft / unknown action): persist below; existing
    # children are left as-is, new children default to draft via initial_status.

    post.save()

    # Attach pending session media for new posts
    if not post_id:
        from apps.media_library.models import MediaAsset as _MediaAsset

        session_key = f"pending_media_{workspace.id}"
        pending_ids = request.session.get(session_key, [])
        if pending_ids:
            for idx, asset_id in enumerate(pending_ids):
                try:
                    asset = _MediaAsset.objects.get(id=asset_id, workspace=workspace)
                    PostMedia.objects.get_or_create(
                        post=post,
                        media_asset=asset,
                        defaults={"position": idx},
                    )
                except _MediaAsset.DoesNotExist:
                    continue
            del request.session[session_key]

    # Sync any new tags to the Tag model
    _sync_tags_to_model(workspace, post.tags)

    # Handle recurring post creation
    make_recurring = request.POST.get("make_recurring")
    if make_recurring and action == "schedule" and post.scheduled_at:
        from apps.calendar.models import RecurrenceRule

        frequency = request.POST.get("recurrence_frequency", "weekly")
        interval_str = request.POST.get("recurrence_interval", "1")
        end_date_str = request.POST.get("recurrence_end_date", "")
        try:
            interval_val = int(interval_str)
        except (ValueError, TypeError):
            interval_val = 1
        end_date_val = None
        if end_date_str:
            try:
                from datetime import date as date_cls

                end_date_val = date_cls.fromisoformat(end_date_str)
            except (ValueError, TypeError):
                pass
        RecurrenceRule.objects.update_or_create(
            post=post,
            defaults={
                "frequency": frequency,
                "interval": interval_val,
                "end_date": end_date_val,
                "is_active": True,
            },
        )

    # Sync platform posts (newly-created rows inherit ``initial_status`` for
    # this action — e.g. "scheduled" for schedule/publish_now, "draft" for
    # save_draft).
    _sync_platform_posts(request, post, workspace, initial_status=initial_status)

    # Propagate manually-chosen schedule/publish_now datetimes to every
    # PlatformPost now that they exist — except published/publishing rows
    # (their schedule is history) and, in scoped mode, siblings outside the
    # ``?account=`` scope.
    scoped_ids = _scoped_platform_post_ids(request, post)
    propagate_dt = getattr(post, "_schedule_propagate_dt", None)
    if propagate_dt is not None:
        propagate_qs = post.platform_posts.exclude(status__in=PlatformPost.PROTECTED_STATUSES)
        if scoped_ids is not None:
            propagate_qs = propagate_qs.filter(id__in=scoped_ids)
        propagate_qs.update(scheduled_at=propagate_dt)

    # Option A: editing an approved post's reviewable content sends it back for
    # re-approval so edits can't publish un-reviewed. Run this BEFORE applying any
    # publish target — otherwise a schedule/publish_now in the *same* save would
    # push the just-edited (no-longer-approved) content straight to scheduled.
    # Only ``approved`` children are reverted; anything else is a no-op.
    content_changed = _orig_content is not None and _base_content_snapshot(post) != _orig_content
    reverted_ids = {str(pp.id) for pp in _revert_approved_to_review(post)} if content_changed else set()

    # Move existing children to the requested target state (no-op for save_draft —
    # children that are already mid-workflow stay put). Children just reverted for
    # re-review are excluded so a same-save publish action can't drag them back
    # out of review.
    if pending_target:
        if reverted_ids:
            candidates = scoped_ids if scoped_ids is not None else [pp.id for pp in post.platform_posts.all()]
            target_only = [pid for pid in candidates if str(pid) not in reverted_ids]
        else:
            target_only = scoped_ids
        _transition_post_children(post, pending_target, only=target_only)

    # Save version
    _save_version(post, request.user)

    # BEAN runs without a 24/7 worker: publish-now would wait for one, so run due tasks here.
    if action == "publish_now":
        try:
            # Publish first so the post isn't stuck behind slow sync tasks in the queue.
            PublishEngine().poll_and_publish()
            drain_pending_tasks(max_seconds=20)
        except Exception:
            logger.exception("publish_now: draining pending tasks failed")

    # Return appropriate response
    if request.htmx:
        return HttpResponse(
            status=204,
            headers={
                "HX-Trigger": json.dumps(
                    {
                        "postSaved": {
                            "postId": str(post.id),
                            "status": post.status,
                        }
                    }
                ),
                "X-Platform-Statuses": json.dumps(_platform_status_map(post)),
            },
        )

    return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post.id)


#: Plataformas donde BEAN puede borrar una publicación ya publicada (TikTok no lo permite por API).
DELETABLE_PLATFORMS = ("facebook", "instagram", "instagram_login", "youtube")


@login_required
@require_POST
@require_permission("delete_published_posts")
def delete_published_post(request, workspace_id, post_id, platform_post_id):
    """Borra en la plataforma una publicación ya publicada y la marca como borrada en BEAN."""
    from apps.publisher.engine import _resolve_publish_credentials
    from providers import get_provider

    workspace = _get_workspace(request, workspace_id)
    pp = get_object_or_404(
        PlatformPost.objects.select_related("post", "social_account").filter(
            social_account__in=visible_accounts(request.workspace_membership)
        ),
        id=platform_post_id,
        post_id=post_id,
        post__workspace=workspace,
    )
    account = pp.social_account
    if account.platform not in DELETABLE_PLATFORMS:
        return HttpResponse("Esta plataforma no permite borrar la publicación desde BEAN.", status=400)
    if pp.status != PlatformPost.Status.PUBLISHED or not pp.platform_post_id:
        return HttpResponse("Solo se pueden borrar publicaciones ya publicadas.", status=400)
    if request.POST.get("confirm", "").strip() != "BORRAR":
        return HttpResponse("Escribe BORRAR para confirmar.", status=400)

    if not (pp.platform_extra or {}).get("deleted_on_platform_at"):
        try:
            provider = get_provider(account.platform, _resolve_publish_credentials(account))
            provider.delete_post(account.get_fresh_access_token(provider), pp.platform_post_id)
        except NotImplementedError:
            return HttpResponse("Esta plataforma aún no permite borrar desde BEAN.", status=400)
        except Exception as exc:
            logger.warning("Borrar publicación %s falló: %s", pp.id, exc)
            return HttpResponse(f"No se pudo borrar en {account.get_platform_display()}: {exc}", status=400)
        pp.platform_extra = {**(pp.platform_extra or {}), "deleted_on_platform_at": timezone.now().isoformat()}
        pp.save(update_fields=["platform_extra"])
    return render(request, "composer/partials/_published_target.html", {"workspace": workspace, "pp": pp})


@login_required
@require_POST
def transition_platform_post(request, workspace_id, post_id, platform_post_id):
    """Transition a single PlatformPost to a target editorial status.

    Used by the composer's per-account chip menu so the user can take a single
    social account in/out of draft, schedule, etc. without affecting siblings.
    Permission rules mirror save_post: ``approve_posts`` is required for any
    approval-stage target; ``publish_directly`` is required for scheduled.
    """
    workspace = _get_workspace(request, workspace_id)
    pp = get_object_or_404(
        PlatformPost.objects.select_related("post", "social_account"),
        id=platform_post_id,
        post_id=post_id,
        post__workspace=workspace,
    )
    target = (request.POST.get("target_status") or "").strip()
    if not target:
        return JsonResponse({"error": "Se requiere target_status"}, status=400)

    membership = request.workspace_membership
    perms = membership.effective_permissions if membership else {}
    approval_states = {"approved", "pending_review", "pending_client", "changes_requested", "rejected"}
    if target in ("scheduled", "publishing") and not perms.get("publish_directly", False):
        raise PermissionDenied("No tienes permiso para programar esta publicación.")
    if target in approval_states and not perms.get("approve_posts", False) and target != "pending_review":
        raise PermissionDenied("No tienes permiso para tomar decisiones de aprobación.")

    if pp.status == target:
        return JsonResponse({"ok": True, "status": pp.status, "noop": True})

    try:
        pp.transition_to(target)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    pp.save(update_fields=["status", "published_at", "updated_at"])
    # Committing a child to publishing obsoletes any draft-stage proposal.
    # Clear it directly rather than via sync_post_scheduled_at: this view sets
    # ``scheduled`` WITHOUT a ``scheduled_at``, and the publisher relies on the
    # ``Coalesce(scheduled_at, post__scheduled_at)`` fallback — recomputing the
    # Post.scheduled_at aggregate here could strand a post that depends on it.
    if target in ("scheduled", "publishing", "published") and pp.post.proposed_publish_at is not None:
        pp.post.proposed_publish_at = None
        pp.post.save(update_fields=["proposed_publish_at", "updated_at"])
    return JsonResponse({"ok": True, "status": pp.status, "platform_post_id": str(pp.id)})


@login_required
@require_permission("create_posts")
@require_POST
def autosave(request, workspace_id, post_id=None):
    """Auto-save endpoint called every 30 seconds via HTMX.

    On first save for a new post (no post_id), creates the draft and returns
    an HX-Trigger with the new post ID so the client can switch the autosave
    URL to the edit endpoint, preventing duplicate drafts on subsequent ticks.
    """
    workspace = _get_workspace(request, workspace_id)

    is_new = False
    orig_content = None
    if post_id:
        post = get_object_or_404(Post, id=post_id, workspace=workspace)
        _require_may_edit(request, post)
        if _is_read_only_published(post):
            return HttpResponse(status=204)
        orig_content = _base_content_snapshot(post)
    else:
        # Check if a previous autosave already created a draft for this session
        # by looking for the post_id passed from the client
        client_post_id = request.POST.get("_autosave_post_id", "").strip()
        if client_post_id:
            try:
                post = Post.objects.get(id=client_post_id, workspace=workspace)
                _require_may_edit(request, post)
                if _is_read_only_published(post):
                    return HttpResponse(status=204)
            except (Post.DoesNotExist, ValidationError):
                post = Post(workspace=workspace, author=request.user)
                is_new = True
        else:
            post = Post(workspace=workspace, author=request.user)
            is_new = True

    post.title = request.POST.get("title", "")
    post.caption = request.POST.get("caption", "")
    post.first_comment = request.POST.get("first_comment", "")
    post.internal_notes = request.POST.get("internal_notes", "")

    post.tags = parse_and_truncate_tag_string(request.POST.get("tags", ""))

    post.save()

    # Attach pending session media when creating a new post
    if is_new:
        from apps.media_library.models import MediaAsset

        session_key = f"pending_media_{workspace.id}"
        pending_ids = request.session.get(session_key, [])
        if pending_ids:
            for idx, asset_id in enumerate(pending_ids):
                try:
                    asset = MediaAsset.objects.get(id=asset_id, workspace=workspace)
                    PostMedia.objects.get_or_create(
                        post=post,
                        media_asset=asset,
                        defaults={"position": idx},
                    )
                except MediaAsset.DoesNotExist:
                    continue
            del request.session[session_key]

    # Sync platform selections
    # Same rules as _sync_platform_posts: only this workspace's accounts the member
    # may publish to are added, and targets they can't touch are never dropped.
    selected_ids = _parse_selected_account_ids(request.POST.get("selected_accounts", ""))
    allowed_ids = _publishable_account_ids(request, workspace)
    keep_ids = list(dict.fromkeys(selected_ids + [i for i in _existing_target_ids(post) if i not in allowed_ids]))
    _remove_deselected_platform_posts(request, post, keep_ids)
    platforms = {
        str(i): p
        for i, p in SocialAccount.objects.filter(id__in=selected_ids, workspace=workspace).values_list("id", "platform")
    }
    for acc_id in selected_ids:
        if acc_id in allowed_ids:
            pp, _ = PlatformPost.objects.get_or_create(
                post=post,
                social_account_id=acc_id,
            )
            new_extra = _apply_c2_extras(request, workspace, acc_id, platforms.get(acc_id, ""), pp.platform_extra)
            if new_extra != (pp.platform_extra or {}):
                pp.platform_extra = new_extra
                pp.save(update_fields=["platform_extra"])

    # Option A: an autosave that changed an approved post's content reverts it to
    # pending_review so edited content can't publish without a fresh review.
    if orig_content is not None and _base_content_snapshot(post) != orig_content:
        _revert_approved_to_review(post)

    return HttpResponse(
        f'<span class="text-xs text-gray-400">Guardado {timezone.now().strftime("%H:%M")}</span>',
        headers={"HX-Trigger": json.dumps({"autosaved": {"postId": str(post.id), "isNew": is_new}})},
    )


def _asset_url(workspace, raw):
    """URL del asset del workspace (o ``""``): para mostrar la portada elegida en la vista previa."""
    raw = (raw or "").strip()
    if not raw or not _is_valid_uuid(raw):
        return ""
    from apps.media_library.models import MediaAsset

    asset = MediaAsset.objects.filter(id=raw, workspace=workspace).first()
    return asset.file.url if asset and asset.file else ""


def _kind_label(platform, post_type, media_items, link=""):
    """Nombre del tipo para personas («Short», «Reel», «Foto»…), el mismo que usa la pestaña de la red."""
    if platform == "youtube":
        return "Short" if post_type == "short" else "Video"
    if platform in ("instagram", "instagram_login"):
        return {"reel": "Reel", "story": "Story", "carousel": "Carrusel"}.get(post_type, "Feed")
    if platform == "facebook":
        if post_type == "reel":
            return "Reel"
        if link:
            return "Enlace"
        if not media_items:
            return "Texto"
        if any(m["is_video"] for m in media_items):
            return "Video"
        return "Fotos" if len(media_items) > 1 else "Foto"
    return ""


@login_required
@require_POST
def preview(request, workspace_id):
    """Live preview endpoint - renders platform-specific preview from form state.

    Called via HTMX with debounced POST from the composer.
    Stateless - no DB queries except social account lookup.
    """
    workspace = _get_workspace(request, workspace_id)
    title = request.POST.get("title", "")
    caption = request.POST.get("caption", "")
    first_comment = request.POST.get("first_comment", "")
    selected_ids = _parse_selected_account_ids(request.POST.get("selected_accounts", ""))

    # Build preview data per platform
    previews = []
    if selected_ids:
        accounts = SocialAccount.objects.filter(
            id__in=selected_ids,
            workspace=workspace,
        ).order_by("platform")
        for account in accounts:
            override_title_key = f"override_title_{account.id}"
            override_key = f"override_caption_{account.id}"
            effective_title = request.POST.get(override_title_key, "") or title
            effective_caption = request.POST.get(override_key, "") or caption
            char_limit = account.char_limit
            field_config = account.field_config
            previews.append(
                {
                    "account": account,
                    "title": effective_title,
                    "caption": effective_caption,
                    "first_comment": first_comment,
                    "char_count": len(effective_caption),
                    "char_limit": char_limit,
                    "is_over_limit": len(effective_caption) > char_limit,
                    "truncated_caption": effective_caption[:char_limit]
                    if len(effective_caption) > char_limit
                    else effective_caption,
                    "needs_title": field_config["needs_title"],
                    # Without this the YouTube preview falls back to a frame of
                    # the video itself, so a custom thumbnail — the one thing a
                    # creator most wants to eyeball before publishing — never
                    # appeared anywhere except its own uploader box.
                    "thumbnail_url": _preview_thumbnail_url(request, workspace, account),
                    "post_type": _post_type_from_form(request.POST, account.id, account.platform) or "",
                    "platform_label": PLATFORM_LABELS.get(account.platform) or account.get_platform_display(),
                    "cover_text": request.POST.get("cover_text", "").strip(),
                    "link": request.POST.get(f"fb_link_{account.id}", "").strip(),
                    "ig_cover_url": _asset_url(workspace, request.POST.get(f"ig_cover_asset_id_{account.id}", "")),
                }
            )

    # Gather media for preview - check pending session media or post attachments
    from apps.media_library.models import MediaAsset

    media_items = []
    post_id_str = request.POST.get("_autosave_post_id", "")

    if post_id_str:
        try:
            post_obj = Post.objects.get(id=post_id_str, workspace=workspace)
            for att in post_obj.media_attachments.select_related("media_asset").all():
                asset = att.media_asset
                media_items.append(
                    {
                        "url": asset.file.url if asset.file else "",
                        "is_video": asset.is_video,
                        "filename": asset.filename,
                        "duration": asset.duration,
                        "width": asset.width,
                        "height": asset.height,
                    }
                )
        except Post.DoesNotExist:
            pass

    if not media_items:
        # Check session pending media
        session_key = f"pending_media_{workspace.id}"
        pending_ids = request.session.get(session_key, [])
        if pending_ids:
            by_id = {str(a.id): a for a in MediaAsset.objects.filter(id__in=pending_ids, workspace=workspace)}
            # Keep the order the user attached them in (id__in has none).
            for asset in (by_id[i] for i in pending_ids if i in by_id):
                media_items.append(
                    {
                        "url": asset.file.url if asset.file else "",
                        "is_video": asset.is_video,
                        "filename": asset.filename,
                        "duration": asset.duration,
                        "width": asset.width,
                        "height": asset.height,
                    }
                )

    # Duration rules (Short <= 3 min, Reel 3-90 s) shown live; unknown durations get a notice.
    videos = [m for m in media_items if m["is_video"]]
    if videos:
        durations = [m["duration"] for m in videos]
        for p in previews:
            p["format_notice"] = media_error_for_platform(
                p["account"].platform, ["video"], p["post_type"], durations
            ) or media_duration_notice(p["post_type"], durations)

    # Revisor de calidad + contador de caracteres visibles, por plataforma.
    banned = get_setting(workspace.id, BANNED_PHRASES_KEY, workspace.organization_id) or []
    first_video = videos[0] if videos else {}
    for p in previews:
        acc = p["account"]
        ptype = p["post_type"] or ("video" if acc.platform == "youtube" and videos else "")
        thumb = _preview_thumbnail_asset(request, workspace, acc)
        p["quality"] = quality_warnings(
            platform=acc.platform,
            post_type=ptype,
            title=p["title"],
            caption=p["caption"],
            first_comment=p["first_comment"],
            cover_text=request.POST.get("cover_text", ""),
            duration=first_video.get("duration"),
            width=first_video.get("width"),
            height=first_video.get("height"),
            thumbnail_bytes=thumb.file_size if thumb else None,
            banned_phrases=banned,
        )
        p["visible"] = visible_counter(acc.platform, ptype, p["title"], p["caption"])
        p["kind"] = _kind_label(acc.platform, p["post_type"], media_items, p["link"])

    blocks = [
        {"message_es": w["message_es"], "who": f"{p['platform_label']} · {p['account'].account_name}"}
        for p in previews
        for w in p["quality"]
        if w["level"] == "block"
    ]
    warns = [
        {"message_es": w["message_es"], "who": f"{p['platform_label']} · {p['account'].account_name}"}
        for p in previews
        for w in p["quality"]
        if w["level"] != "block"
    ]
    return render(
        request,
        "composer/partials/preview_panel.html",
        {
            "previews": previews,
            "workspace": workspace,
            "media_items": media_items,
            "oob_review": True,
            "review_blocks": blocks,
            "review_warns": warns,
            "review_blocks_json": json.dumps([b["message_es"] for b in blocks]),
        },
    )


@login_required
@require_GET
def media_picker(request, workspace_id, post_id=None):
    """Modal picker for selecting media from the library."""
    workspace = _get_workspace(request, workspace_id)
    from apps.media_library.models import MediaAsset

    post = None
    if post_id:
        post = get_object_or_404(Post, id=post_id, workspace=workspace)

    assets = MediaAsset.objects.for_workspace_with_shared(workspace.id, workspace.organization_id).order_by(
        "-created_at"
    )[:50]
    return render(
        request,
        "composer/partials/media_picker.html",
        {
            "assets": assets,
            "workspace": workspace,
            "post": post,
        },
    )


@login_required
@require_GET
def thumbnail_picker(request, workspace_id):
    """Modal picker for selecting an image asset as a YouTube thumbnail.

    Image-only, selection dispatches a client-side event; does not attach
    anything server-side.
    """
    workspace = _get_workspace(request, workspace_id)
    from apps.media_library.models import MediaAsset

    assets = (
        MediaAsset.objects.for_workspace(workspace.id)
        .filter(media_type=MediaAsset.MediaType.IMAGE)
        .order_by("-created_at")[:50]
    )
    return render(
        request,
        "composer/partials/thumbnail_picker.html",
        {"assets": assets, "workspace": workspace},
    )


_THUMBNAIL_MIMES = {"image/jpeg", "image/png"}
_THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024  # YouTube thumbnails.set limit
_THUMBNAIL_MAX_SIZE = (1280, 720)
_THUMBNAIL_MAX_PIXELS = 50_000_000


def _fit_thumbnail(uploaded_file):
    """Return ``(file, error)``: the thumbnail as-is when it already fits (<= 1280x720 and <= 2 MB),
    else resized to a JPEG with Pillow, else a Spanish error when it can't be fixed."""
    too_big = uploaded_file.size > _THUMBNAIL_MAX_BYTES
    try:
        from PIL import Image
    except ImportError:
        if too_big:
            return None, "La miniatura pesa más de 2 MB. Reduce su tamaño e inténtalo de nuevo."
        return uploaded_file, ""
    try:
        img = Image.open(uploaded_file)
        # Decompression bomb guard: size is in the header, check it before decoding.
        if img.width * img.height > _THUMBNAIL_MAX_PIXELS:
            return None, "La imagen de la miniatura es demasiado grande (máximo 50 megapíxeles)."
        img.load()
    except Exception:
        return None, "No se pudo leer la imagen de la miniatura."
    if not too_big and img.width <= _THUMBNAIL_MAX_SIZE[0] and img.height <= _THUMBNAIL_MAX_SIZE[1]:
        uploaded_file.seek(0)
        return uploaded_file, ""
    img = img.convert("RGB")
    img.thumbnail(_THUMBNAIL_MAX_SIZE)
    for quality in (90, 80, 70, 60, 50, 40):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        if buf.tell() <= _THUMBNAIL_MAX_BYTES:
            name = uploaded_file.name.rsplit(".", 1)[0] + ".jpg"
            return ContentFile(buf.getvalue(), name=name), ""
    return None, "No se pudo reducir la miniatura a menos de 2 MB."


@login_required
@require_POST
@require_permission("upload_media")
def thumbnail_upload(request, workspace_id):
    """Upload an image from the local machine to the media library and return
    its id + URL so the composer can wire it as a YouTube thumbnail.

    Image-only. Returns JSON with asset_id, url, filename.
    """
    workspace = _get_workspace(request, workspace_id)
    uploaded_file = request.FILES.get("file")

    if not uploaded_file:
        return JsonResponse({"error": "No se envió ningún archivo"}, status=400)

    from apps.media_library.quotas import StorageQuotaExceededError
    from apps.media_library.services import create_asset
    from apps.media_library.validators import sniff_mime

    # Real type from magic bytes (the client Content-Type is not trusted): only JPG/PNG.
    if sniff_mime(uploaded_file) not in _THUMBNAIL_MIMES:
        return JsonResponse({"error": "La miniatura debe ser una imagen JPG o PNG."}, status=400)
    uploaded_file, error = _fit_thumbnail(uploaded_file)
    if error:
        return JsonResponse({"error": error}, status=400)

    try:
        asset = create_asset(
            organization=workspace.organization,
            workspace=workspace,
            uploaded_file=uploaded_file,
            uploaded_by=request.user,
        )
    except ValidationError as exc:
        return JsonResponse({"error": " ".join(exc.messages)}, status=400)
    except StorageQuotaExceededError:
        return JsonResponse({"error": "Se alcanzó el límite de almacenamiento de la organización."}, status=413)

    url = ""
    if asset.thumbnail:
        url = asset.thumbnail.url
    elif asset.file:
        url = asset.file.url

    return JsonResponse(
        {
            "asset_id": str(asset.id),
            "url": url,
            "filename": asset.filename,
        }
    )


_RANGE_HEADER_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class _RangeFileIterator:
    """Iterate a bounded byte window of an already-positioned file handle."""

    def __init__(self, file_handle, remaining, chunk_size=64 * 1024):
        self.file_handle = file_handle
        self.remaining = remaining
        self.chunk_size = chunk_size

    def __iter__(self):
        return self

    def __next__(self):
        if self.remaining <= 0:
            raise StopIteration
        data = self.file_handle.read(min(self.chunk_size, self.remaining))
        if not data:
            raise StopIteration
        self.remaining -= len(data)
        return data

    def close(self):
        if hasattr(self.file_handle, "close"):
            self.file_handle.close()


@login_required
@require_GET
def media_stream(request, workspace_id, asset_id):
    """Stream a media asset through the app's own origin, with Range support.

    The composer's frame picker draws video frames onto a canvas, which the
    browser only allows for same-origin (or CORS-approved) media. Object
    storage like S3/R2 serves signed URLs from another origin, usually
    without CORS headers, so the raw file URL would taint the canvas.

    Byte-range requests matter here: without them the browser can't seek a
    <video> beyond what it has buffered (the scrubber and filmstrip clicks
    silently do nothing) and has to download the whole file up front.
    """
    workspace = _get_workspace(request, workspace_id)

    from apps.media_library.models import MediaAsset

    asset = get_object_or_404(
        MediaAsset.objects.for_workspace_with_shared(
            workspace_id=workspace.id,
            organization_id=workspace.organization_id,
        ),
        pk=asset_id,
    )
    if not asset.file:
        raise Http404
    # The DB row can outlive the stored object (lifecycle rule, manual S3
    # deletion); opening/stat-ing it then raises a backend error rather than
    # returning an empty FieldFile, so map that to 404 instead of a 500.
    try:
        size = asset.file.size
        file_handle = asset.file.open("rb")
    except Exception:  # noqa: BLE001 - storage backends raise varied errors (OSError, botocore ClientError)
        raise Http404 from None

    content_type = asset.mime_type or "application/octet-stream"
    range_match = _RANGE_HEADER_RE.match(request.headers.get("Range", ""))

    if range_match and size:
        start_str, end_str = range_match.groups()
        if not start_str:
            # Suffix range: the last N bytes.
            length = min(int(end_str or 0), size)
            start = size - length
            end = size - 1
        else:
            start = int(start_str)
            end = min(int(end_str), size - 1) if end_str else size - 1
        if start >= size or start > end:
            file_handle.close()
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{size}"
            return response
        file_handle.seek(start)
        response = StreamingHttpResponse(
            _RangeFileIterator(file_handle, end - start + 1),
            status=206,
            content_type=content_type,
        )
        response["Content-Length"] = str(end - start + 1)
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
    else:
        response = FileResponse(file_handle, content_type=content_type)

    response["Accept-Ranges"] = "bytes"
    # Asset files are immutable per id - let the browser cache the stream so
    # reopening the frame picker doesn't re-download the whole video.
    response["Cache-Control"] = "private, max-age=3600"
    return response


@login_required
@require_GET
def media_filmstrip(request, workspace_id, asset_id):
    """Return evenly-spaced thumbnail frames for the frame-picker filmstrip.

    Extracted server-side with ffmpeg, which seeks each frame via byte ranges
    instead of making the browser download (and decode) most of the video to
    build the strip client-side.
    """
    workspace = _get_workspace(request, workspace_id)

    import os
    import tempfile

    from apps.media_library.models import MediaAsset
    from apps.media_library.services import extract_video_frames, extract_video_metadata

    asset = get_object_or_404(
        MediaAsset.objects.for_workspace_with_shared(
            workspace_id=workspace.id,
            organization_id=workspace.organization_id,
        ),
        pk=asset_id,
    )
    if asset.media_type != MediaAsset.MediaType.VIDEO or not asset.file:
        raise Http404

    # Mirror media_library's video pipeline: pull the file to one local temp
    # file, then run all the ffmpeg seeks against it. Extracting each frame
    # straight from the (remote) signed URL re-opens the connection and
    # re-reads the moov atom every time - on R2 that was ~1.5s per frame.
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{asset.file_extension}", delete=False) as tmp:
            for chunk in asset.file.chunks():
                tmp.write(chunk)
            tmp_path = tmp.name
    except Exception:  # noqa: BLE001 - storage backends raise varied errors when the object is gone
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise Http404 from None

    try:
        duration = asset.duration or extract_video_metadata(tmp_path).get("duration_seconds") or 0
        if not duration:
            return JsonResponse({"error": "No se pudo leer la duración del video."}, status=502)

        count = 8
        timestamps = [round(duration * (i + 0.5) / count, 3) for i in range(count)]
        jpegs = extract_video_frames(tmp_path, timestamps, width=160)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)

    frames = [
        {"time": t, "dataUrl": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}
        for t, jpg in zip(timestamps, jpegs, strict=False)
        if jpg
    ]
    if not frames:
        return JsonResponse({"error": "No se pudieron extraer los fotogramas."}, status=502)
    return JsonResponse({"frames": frames, "duration": duration})


UNSPLASH_API_BASE = "https://api.unsplash.com"
UNSPLASH_IMAGE_HOST = "https://images.unsplash.com/"
UNSPLASH_NOT_CONFIGURED = (
    "Unsplash no está configurado. Agrega UNSPLASH_ACCESS_KEY al entorno para habilitar la búsqueda de fotos."
)
UNSPLASH_MAX_IMPORT = 10
UNSPLASH_MAX_IMAGE_BYTES = 15 * 1024 * 1024


def _as_str(value):
    """Coerce an external/client JSON field to a string.

    Client- and Unsplash-supplied payloads can carry ``null`` (or numbers)
    where we expect a URL/text, so guard before ``.startswith()`` or feeding
    a model's non-null string column - otherwise an ``AttributeError`` (or
    IntegrityError) turns a should-fail-gracefully path into a 500.
    """
    return value if isinstance(value, str) else ""


@login_required
@require_GET
def unsplash_search(request, workspace_id):
    """Proxy an Unsplash photo search so the API key stays server-side.

    Returns results trimmed to what the composer modal needs. Grid images are
    hotlinked from the Unsplash CDN per their API guidelines.
    """
    _get_workspace(request, workspace_id)

    if not settings.UNSPLASH_ACCESS_KEY:
        return JsonResponse({"error": UNSPLASH_NOT_CONFIGURED}, status=503)

    query = request.GET.get("q", "").strip()
    if not query:
        return JsonResponse({"error": "Falta el término de búsqueda"}, status=400)

    try:
        resp = httpx.get(
            f"{UNSPLASH_API_BASE}/search/photos",
            params={"query": query, "page": 1, "per_page": 24},
            headers={
                "Authorization": f"Client-ID {settings.UNSPLASH_ACCESS_KEY}",
                "Accept-Version": "v1",
            },
            timeout=10.0,
        )
    except httpx.RequestError:
        return JsonResponse({"error": "No se pudo conectar con Unsplash. Inténtalo de nuevo."}, status=502)

    if resp.status_code in (401, 403):
        return JsonResponse({"error": "Unsplash rechazó la clave de API. Revisa UNSPLASH_ACCESS_KEY."}, status=502)
    if resp.status_code == 429:
        return JsonResponse(
            {"error": "Se alcanzó el límite de Unsplash. Inténtalo de nuevo en unos minutos."}, status=429
        )
    if resp.status_code != 200:
        return JsonResponse({"error": "La búsqueda en Unsplash falló. Inténtalo de nuevo."}, status=502)

    # A 200 with a malformed body or an unexpected result shape (e.g. a bare
    # list or null instead of an object) must surface as a friendly 502, not
    # an unhandled 500.
    try:
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError("Unexpected Unsplash response shape")
        results = [
            {
                "id": p["id"],
                "thumb": p["urls"]["small"],
                "full": p["urls"]["regular"],
                "width": p.get("width"),
                "height": p.get("height"),
                "color": p.get("color"),
                "alt": p.get("alt_description") or p.get("description") or "",
                "photographer": p["user"]["name"],
                "photographer_url": p["user"]["links"]["html"],
                "photo_url": p["links"]["html"],
                "download_location": p["links"]["download_location"],
            }
            for p in data.get("results", [])
        ]
        total = data.get("total", 0)
    except (ValueError, KeyError, TypeError, AttributeError):
        return JsonResponse({"error": "Respuesta inesperada de Unsplash. Inténtalo de nuevo."}, status=502)
    return JsonResponse({"results": results, "total": total})


@login_required
@require_POST
@require_permission("upload_media")
def unsplash_import(request, workspace_id, post_id=None):
    """Download selected Unsplash photos server-side and attach them as media.

    Mirrors upload_media: attaches to the post when post_id is given, else
    queues in the pending-media session. Hits each photo's download_location
    first, as Unsplash's guidelines require when a photo is actually used.
    """
    workspace = _get_workspace(request, workspace_id)

    if not settings.UNSPLASH_ACCESS_KEY:
        return JsonResponse({"error": UNSPLASH_NOT_CONFIGURED}, status=503)

    try:
        payload = json.loads(request.body)
        photos = payload["photos"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return JsonResponse({"error": "Cuerpo de la solicitud no válido"}, status=400)

    if not isinstance(photos, list) or not photos:
        return JsonResponse({"error": "No se seleccionaron fotos"}, status=400)
    if len(photos) > UNSPLASH_MAX_IMPORT:
        return JsonResponse({"error": f"Selecciona máximo {UNSPLASH_MAX_IMPORT} fotos por importación."}, status=400)

    from django.core.files.base import ContentFile

    from apps.media_library.models import MediaAsset
    from apps.media_library.quotas import StorageQuotaExceededError, enforce_storage_quota

    post = None
    if post_id:
        post = get_object_or_404(Post, id=post_id, workspace=workspace)

    auth_headers = {"Authorization": f"Client-ID {settings.UNSPLASH_ACCESS_KEY}"}
    new_assets = []
    attachments = []
    failed = 0
    quota_exceeded = False

    # follow_redirects is OFF: the host allowlist below is only meaningful if a
    # 30x can't bounce the request to an internal address (SSRF). One pooled
    # client reuses connections across the per-photo registration + download.
    with httpx.Client(follow_redirects=False, timeout=30.0) as client:
        for photo in photos:
            if not isinstance(photo, dict):
                failed += 1
                continue
            photo_id = _as_str(photo.get("id")).strip()
            download_location = _as_str(photo.get("download_location"))
            fallback_url = _as_str(photo.get("full"))
            # The client supplies these URLs - only ever fetch from Unsplash hosts.
            if not photo_id or not download_location.startswith(f"{UNSPLASH_API_BASE}/"):
                failed += 1
                continue

            content = None
            content_type = ""
            try:
                # Register the download (required by Unsplash API guidelines);
                # the response carries the actual file URL to fetch. A non-JSON
                # body here must fail this one photo, not the whole request.
                dl_resp = client.get(download_location, headers=auth_headers, timeout=10.0)
                image_url = ""
                if dl_resp.status_code == 200:
                    # A non-JSON or non-object body must fail this one photo,
                    # not 500 the whole request.
                    with contextlib.suppress(ValueError, AttributeError):
                        image_url = _as_str(dl_resp.json().get("url"))
                if not image_url:
                    image_url = fallback_url
                if not image_url.startswith(UNSPLASH_IMAGE_HOST):
                    failed += 1
                    continue

                # Stream and cap the download so a huge (or mis-declared) body
                # can't be buffered whole - reject before exceeding the limit.
                with client.stream("GET", image_url) as img_resp:
                    content_type = img_resp.headers.get("content-type", "")
                    declared = img_resp.headers.get("content-length", "")
                    if (
                        img_resp.status_code != 200
                        or not content_type.startswith("image/")
                        or (declared.isdigit() and int(declared) > UNSPLASH_MAX_IMAGE_BYTES)
                    ):
                        failed += 1
                        continue
                    buf = bytearray()
                    for chunk in img_resp.iter_bytes():
                        buf += chunk
                        if len(buf) > UNSPLASH_MAX_IMAGE_BYTES:
                            break
                    if len(buf) > UNSPLASH_MAX_IMAGE_BYTES:
                        failed += 1
                        continue
                    content = bytes(buf)
            except httpx.HTTPError:
                failed += 1
                continue

            try:
                enforce_storage_quota(workspace.organization, len(content))
            except StorageQuotaExceededError:
                quota_exceeded = True
                break

            filename = f"unsplash-{photo_id}.jpg"
            asset = MediaAsset.objects.create(
                organization=workspace.organization,
                workspace=workspace,
                uploaded_by=request.user,
                file=ContentFile(content, name=filename),
                filename=filename,
                media_type=MediaAsset.MediaType.IMAGE,
                mime_type=content_type,
                file_size=len(content),
                source="unsplash",
                source_url=_as_str(photo.get("photo_url")),
                attribution=f"Photo by {_as_str(photo.get('photographer')) or 'Unknown'} on Unsplash",
                alt_text=_as_str(photo.get("alt")),
            )
            new_assets.append(asset)
            attachment = _attach_asset_for_composer(request, workspace, asset, post)
            if attachment is not None:
                attachments.append(attachment)

    if not new_assets:
        if quota_exceeded:
            return JsonResponse(
                {"error": "Se excedió la cuota de almacenamiento. Libera espacio o mejora tu plan."},
                status=413,
            )
        return JsonResponse({"error": "No se pudieron importar las fotos seleccionadas."}, status=502)

    if post is not None:
        html = render_to_string(
            "composer/partials/media_list.html",
            {"media_attachments": attachments, "post": post, "workspace": workspace},
            request=request,
        )
    else:
        html = render_to_string(
            "composer/partials/media_list_pending.html",
            {"pending_assets": new_assets, "workspace": workspace},
            request=request,
        )

    return JsonResponse(
        {
            "html": html,
            "assets": [{"id": str(a.id), "url": a.file.url} for a in new_assets],
            "failed": failed,
        }
    )


@login_required
@require_GET
def pinterest_boards(request, workspace_id, account_id):
    """Fetch Pinterest boards for board selection in the composer."""
    workspace = _get_workspace(request, workspace_id)
    account = get_object_or_404(SocialAccount, id=account_id, workspace=workspace, platform="pinterest")

    from apps.credentials.models import resolve_platform_credentials
    from providers import get_provider

    # .env is dominant; admin-entered org credentials are the fallback.
    credentials = resolve_platform_credentials("pinterest", workspace.organization_id)

    provider = get_provider("pinterest", credentials)

    # Refresh token if expiring
    access_token = account.oauth_access_token
    if account.token_expires_at and account.is_token_expiring_soon:
        try:
            access_token = account.refresh_oauth_token(provider)
        except Exception:
            return JsonResponse({"error": "Falló la renovación del token"}, status=502)

    try:
        boards = provider.get_boards(access_token)
    except Exception:
        return JsonResponse({"error": "No se pudieron obtener los tableros"}, status=502)

    return JsonResponse({"boards": [{"id": b.get("id"), "name": b.get("name")} for b in boards]})


@login_required
@require_GET
@require_permission("create_posts")
def youtube_playlists(request, workspace_id, account_id):
    """Listas de reproducción del canal para el selector del composer.

    Acotado a la cuenta de YouTube de este workspace y a quien puede publicar en ese canal.
    """
    workspace = _get_workspace(request, workspace_id)
    account = get_object_or_404(SocialAccount, id=account_id, workspace=workspace, platform="youtube")
    if str(account.id) not in _publishable_account_ids(request, workspace):
        raise PermissionDenied("No puedes publicar en este canal.")

    from apps.credentials.models import resolve_platform_credentials
    from providers import get_provider

    try:
        provider = get_provider("youtube", resolve_platform_credentials("youtube", workspace.organization_id))
        access_token = account.oauth_access_token
        if account.token_expires_at and account.is_token_expiring_soon:
            access_token = account.refresh_oauth_token(provider)
        playlists = provider.list_playlists(access_token, cache_id=str(account.id))
    except Exception:
        logger.warning("No se pudieron listar las playlists de %s", account.id, exc_info=True)
        return JsonResponse({"error": "No se pudieron obtener las listas de reproducción"}, status=502)
    return JsonResponse({"playlists": [{"id": p["id"], "title": p["title"]} for p in playlists]})


@login_required
@require_POST
@require_permission("upload_media")
def reorder_media(request, workspace_id, post_id=None):
    """Guarda el orden de los medios (``order`` = ids de asset separados por coma): manda el primero como portada."""
    workspace = _get_workspace(request, workspace_id)
    order = [i for i in request.POST.get("order", "").split(",") if _is_valid_uuid(i)]
    if post_id:
        post = get_object_or_404(Post, id=post_id, workspace=workspace)
        _require_may_edit(request, post)
        attachments = {str(pm.media_asset_id): pm for pm in post.media_attachments.all()}
        ordered = [attachments[i] for i in dict.fromkeys(order) if i in attachments]
        ordered += [pm for pm in attachments.values() if pm not in ordered]
        changed = False
        for position, pm in enumerate(ordered):
            if pm.position != position:
                pm.position = position
                pm.save(update_fields=["position"])
                changed = True
        if changed:
            _revert_approved_to_review(post)
    else:
        key = f"pending_media_{workspace.id}"
        pending = request.session.get(key, [])
        ordered_ids = [i for i in dict.fromkeys(order) if i in pending]
        request.session[key] = ordered_ids + [i for i in pending if i not in ordered_ids]
    return HttpResponse(status=204, headers={"HX-Trigger": "previewUpdate"})


@login_required
@require_GET
def tiktok_creator_info(request, workspace_id, account_id):
    """Fetch TikTok creator info for the composer's TikTok settings panel.

    TikTok's integration guidelines require querying this fresh before each
    post: the allowed privacy levels depend on the app's audit status and the
    creator's account settings.
    """
    workspace = _get_workspace(request, workspace_id)
    account = get_object_or_404(SocialAccount, id=account_id, workspace=workspace, platform="tiktok")

    from apps.credentials.models import resolve_platform_credentials
    from providers import get_provider

    credentials = resolve_platform_credentials("tiktok", workspace.organization_id)
    provider = get_provider("tiktok", credentials)

    # This endpoint enriches the composer panel; the real privacy/audit gate is
    # enforced at publish time. When the creator-info lookup can't run (no
    # token, expired token, TikTok API down), degrade to a 200 with empty
    # options and let the panel fall back to its defaults, rather than logging
    # a 5xx in the browser on every composer load.
    def _unavailable(reason):
        return JsonResponse(
            {
                "available": False,
                "error": reason,
                "creator_nickname": "",
                "privacy_level_options": [],
                "comment_disabled": False,
                "duet_disabled": False,
                "stitch_disabled": False,
                "max_video_post_duration_sec": None,
            }
        )

    # Refresh token if expiring
    access_token = account.oauth_access_token
    if account.token_expires_at and account.is_token_expiring_soon:
        try:
            access_token = account.refresh_oauth_token(provider)
        except Exception:
            return _unavailable("Falló la renovación del token")

    try:
        info = provider.query_creator_info(access_token)
    except Exception:
        return _unavailable("No se pudo obtener la información del creador")

    return JsonResponse(
        {
            "available": True,
            "creator_nickname": info.get("creator_nickname", ""),
            "privacy_level_options": info.get("privacy_level_options") or [],
            "comment_disabled": bool(info.get("comment_disabled")),
            "duet_disabled": bool(info.get("duet_disabled")),
            "stitch_disabled": bool(info.get("stitch_disabled")),
            "max_video_post_duration_sec": info.get("max_video_post_duration_sec"),
        }
    )


@login_required
@require_POST
@require_permission("upload_media")
def attach_media(request, workspace_id, post_id):
    """Attach a media asset to a post."""
    workspace = _get_workspace(request, workspace_id)
    post = get_object_or_404(Post, id=post_id, workspace=workspace)
    media_asset_id = request.POST.get("media_asset_id")

    if not media_asset_id:
        return JsonResponse({"error": "Se requiere media_asset_id"}, status=400)

    from apps.media_library.models import MediaAsset

    asset = get_object_or_404(
        MediaAsset.objects.for_workspace_with_shared(workspace.id, workspace.organization_id),
        id=media_asset_id,
    )

    max_pos = post.media_attachments.aggregate(models.Max("position"))["position__max"]
    position = (max_pos or 0) + 1

    attachment, _ = PostMedia.objects.get_or_create(
        post=post,
        media_asset=asset,
        defaults={"position": position},
    )
    _sync_own_media(post, asset.id, add=True)

    # Option A: changing media on an approved post sends it back for re-approval.
    _revert_approved_to_review(post)

    response = render(
        request,
        "composer/partials/media_list.html",
        {
            "media_attachments": [attachment],
            "post": post,
            "workspace": workspace,
        },
    )
    response["HX-Trigger"] = "previewUpdate"
    return response


@login_required
@require_POST
@require_permission("upload_media")
def attach_pending_media(request, workspace_id):
    """Attach a library media asset as pending (before post is saved)."""
    workspace = _get_workspace(request, workspace_id)
    media_asset_id = request.POST.get("media_asset_id")

    if not media_asset_id:
        return JsonResponse({"error": "Se requiere media_asset_id"}, status=400)

    from apps.media_library.models import MediaAsset

    asset = get_object_or_404(
        MediaAsset.objects.for_workspace_with_shared(workspace.id, workspace.organization_id),
        id=media_asset_id,
    )

    session_key = f"pending_media_{workspace.id}"
    pending = request.session.get(session_key, [])
    asset_id_str = str(asset.id)
    if asset_id_str not in pending:
        pending.append(asset_id_str)
        request.session[session_key] = pending

    response = render(
        request,
        "composer/partials/media_list_pending.html",
        {
            "pending_assets": [asset],
            "workspace": workspace,
        },
    )
    response["HX-Trigger"] = "previewUpdate"
    return response


def _sync_own_media(post, asset_id, *, add):
    """Mantiene al día los medios propios por red (importador): suma o quita el asset en cada lista no nula."""
    sid = str(asset_id)
    for pp in post.platform_posts.all():
        own = pp.platform_specific_media
        if not isinstance(own, list) or (sid in own) == add:
            continue
        pp.platform_specific_media = [*own, sid] if add else [i for i in own if i != sid]
        pp.save(update_fields=["platform_specific_media", "updated_at"])


def _attach_asset_for_composer(request, workspace, asset, post=None):
    """Attach an asset to a post, or queue it in the pending-media session.

    Post path: migrates any session-pending media first (can happen when the
    media picker still uses attach_pending_media after autosave created the
    post and updated the upload URL), then appends the asset at the next
    position. Returns the PostMedia attachment, or None on the pending path.
    """
    from apps.media_library.models import MediaAsset

    session_key = f"pending_media_{workspace.id}"

    if post is not None:
        pending_ids = request.session.get(session_key, [])
        if pending_ids:
            existing_pos = post.media_attachments.aggregate(models.Max("position"))["position__max"] or 0
            for idx, pid in enumerate(pending_ids):
                try:
                    pending_asset = MediaAsset.objects.get(id=pid, workspace=workspace)
                    PostMedia.objects.get_or_create(
                        post=post,
                        media_asset=pending_asset,
                        defaults={"position": existing_pos + idx + 1},
                    )
                except MediaAsset.DoesNotExist:
                    continue
            del request.session[session_key]

        max_pos = post.media_attachments.aggregate(models.Max("position"))["position__max"]
        position = (max_pos or 0) + 1
        attachment = PostMedia.objects.create(post=post, media_asset=asset, position=position)
        _sync_own_media(post, asset.id, add=True)
        return attachment

    # No post yet - store pending media IDs in session so they can be
    # attached when the post is eventually saved.
    pending = request.session.get(session_key, [])
    pending.append(str(asset.id))
    request.session[session_key] = pending
    return None


@login_required
@require_POST
@require_permission("upload_media")
def upload_media(request, workspace_id, post_id=None):
    """Upload a file directly from the composer and optionally attach to a post."""
    workspace = _get_workspace(request, workspace_id)
    uploaded_file = request.FILES.get("file")
    upload_id = request.POST.get("upload_id")  # direct-to-R2 upload already in storage

    if not uploaded_file and not upload_id:
        return JsonResponse({"error": "No se envió ningún archivo"}, status=400)

    from apps.media_library.models import PendingUpload
    from apps.media_library.quotas import StorageQuotaExceededError
    from apps.media_library.services import create_asset, finalize_pending_upload
    from apps.media_library.tasks import process_media_asset
    from apps.media_library.views import finalize_upload_error

    # Same validation as the library: sniffed type, size cap, org quota.
    try:
        if upload_id:
            asset = finalize_pending_upload(workspace=workspace, upload_id=upload_id, uploaded_by=request.user)
        else:
            asset = create_asset(
                organization=workspace.organization,
                workspace=workspace,
                uploaded_file=uploaded_file,
                uploaded_by=request.user,
            )
    except (
        PendingUpload.DoesNotExist,
        ValidationError,
        StorageQuotaExceededError,
        ValueError,
        FileNotFoundError,
    ) as exc:
        message, status = finalize_upload_error(exc)
        return JsonResponse({"error": message}, status=status)
    process_media_asset(str(asset.id))  # metadata, duration and thumbnail

    if post_id:
        post = get_object_or_404(Post, id=post_id, workspace=workspace)
        attachment = _attach_asset_for_composer(request, workspace, asset, post)
        # Option A: changing media on an approved post sends it back for re-approval.
        _revert_approved_to_review(post)
        response = render(
            request,
            "composer/partials/media_list.html",
            {
                "media_attachments": [attachment],
                "post": post,
                "workspace": workspace,
            },
        )
    else:
        _attach_asset_for_composer(request, workspace, asset)
        response = render(
            request,
            "composer/partials/media_list_pending.html",
            {
                "pending_assets": [asset],
                "workspace": workspace,
            },
        )

    response["X-Uploaded-Asset-Id"] = str(asset.id)
    response["X-Uploaded-Asset-Url"] = asset.file.url
    return response


@login_required
@require_POST
@require_permission("upload_media")
def remove_media(request, workspace_id, post_id, media_id):
    """Remove a media attachment from a post."""
    workspace = _get_workspace(request, workspace_id)
    post = get_object_or_404(Post, id=post_id, workspace=workspace)
    removed = PostMedia.objects.filter(id=media_id, post=post).first()
    if removed:
        _sync_own_media(post, removed.media_asset_id, add=False)
        removed.delete()

    # Option A: changing media on an approved post sends it back for re-approval.
    _revert_approved_to_review(post)

    response = render(
        request,
        "composer/partials/media_list.html",
        {
            "media_attachments": post.media_attachments.select_related("media_asset").all(),
            "post": post,
            "workspace": workspace,
        },
    )
    response["HX-Trigger"] = "previewUpdate"
    return response


@login_required
@require_POST
@require_permission("upload_media")
def remove_pending_media(request, workspace_id, asset_id):
    """Drop an asset the user attached but has not saved with a post yet.

    Gated like its sibling ``remove_media``, which was decorated and this one was
    not — the asymmetry is what made the hole below reachable by anyone.
    """
    workspace = _get_workspace(request, workspace_id)

    from apps.media_library.models import MediaAsset
    from apps.media_library.services import delete_asset

    session_key = f"pending_media_{workspace.id}"
    pending = request.session.get(session_key, [])
    asset_id_str = str(asset_id)

    # The delete used to sit *outside* this block, so posting any asset id in the
    # workspace destroyed it and its files — whether or not it was pending, and
    # with the error swallowed. Only something this session actually staged may
    # be removed here; deleting library assets is `asset_delete`'s job, behind
    # `delete_media`.
    if asset_id_str not in pending:
        return _render_pending_media(request, workspace, pending)

    pending.remove(asset_id_str)
    request.session[session_key] = pending

    asset = MediaAsset.objects.filter(id=asset_id, workspace=workspace).first()
    if asset:
        with contextlib.suppress(Exception):
            delete_asset(asset)

    return _render_pending_media(request, workspace, pending)


def _render_pending_media(request, workspace, pending):
    """The staged-media strip, redrawn. Shared by both exits above."""
    from apps.media_library.models import MediaAsset

    response = render(
        request,
        "composer/partials/media_list_pending.html",
        {
            "pending_assets": MediaAsset.objects.filter(id__in=pending, workspace=workspace),
            "workspace": workspace,
        },
    )
    response["HX-Trigger"] = "previewUpdate"
    return response


@login_required
@require_GET
def drafts_list(request, workspace_id):
    """List all drafts for this workspace."""
    workspace = _get_workspace(request, workspace_id)
    # A post is a "draft" when at least one of its PlatformPost children is in
    # the draft state and none have moved into a more advanced workflow stage.
    # Easiest correct query: any post whose only child statuses are "draft".
    drafts = (
        Post.objects.for_workspace(workspace.id)
        .filter(platform_posts__status="draft")
        .exclude(
            platform_posts__status__in=[
                "pending_review",
                "pending_client",
                "approved",
                "scheduled",
                "publishing",
                "published",
            ]
        )
        .distinct()
        .select_related("author")
        .prefetch_related("platform_posts__social_account")
        .order_by("-updated_at")
    )

    return render(
        request,
        "composer/drafts_list.html",
        {
            "workspace": workspace,
            "drafts": drafts,
        },
    )


@login_required
@require_POST
@require_permission("create_posts")
def post_delete(request, workspace_id, post_id):
    """Delete a post or a single platform post via HTMX.

    When an ``account`` query parameter is provided, only the PlatformPost for
    that social account is removed.  If it was the last PlatformPost the parent
    Post is deleted as well.  Without the parameter the entire Post (and all
    its PlatformPosts) is deleted.
    """
    workspace = _get_workspace(request, workspace_id)
    post = get_object_or_404(Post, id=post_id, workspace=workspace)
    _require_may_edit(request, post)

    account_id = request.GET.get("account") or request.POST.get("account")
    if account_id:
        pp = get_object_or_404(PlatformPost, post=post, social_account_id=account_id)
        pp.delete()
        # If no platform posts remain, clean up the parent post too.
        if not post.platform_posts.exists():
            post.delete()
    else:
        post.delete()

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "postChanged"},
    )


@login_required
@require_POST
def clone_post_view(request, workspace_id, post_id):
    """Clone a post into a fresh draft (Clone / Repost) and open the copy.

    The composer makes published/publishing posts read-only; this is the escape
    hatch to repost — the duplicate starts as a draft with the same content but
    no schedule, so it can be edited and re-queued without touching the original.
    """
    from django.urls import reverse

    from apps.composer.services import clone_post

    workspace = _get_workspace(request, workspace_id)
    post = get_object_or_404(Post, id=post_id, workspace=workspace)

    membership = request.workspace_membership
    perms = membership.effective_permissions if membership else {}
    if not perms.get("create_posts", False):
        raise PermissionDenied("No tienes permiso para crear publicaciones.")

    new_post = clone_post(post, author=request.user)
    target = reverse("composer:compose_edit", kwargs={"workspace_id": workspace.id, "post_id": new_post.id})
    if request.htmx:
        return HttpResponse(status=204, headers={"HX-Redirect": target})
    return redirect(target)


# ---------------------------------------------------------------------------
# Create landing page & Idea CRUD
# ---------------------------------------------------------------------------


def _idea_columns(workspace, tag=None):
    """Build Kanban columns from IdeaGroup for a workspace, optionally filtered by tag."""
    groups = IdeaGroup.objects.for_workspace(workspace.id).order_by("position", "created_at")

    # Ensure default groups exist for this workspace
    if not groups.exists():
        created_groups = {}
        for name, pos in [("Unassigned", 0), ("To Do", 1), ("In Progress", 2), ("Done", 3)]:
            created_groups[name] = IdeaGroup.objects.create(workspace=workspace, name=name, position=pos)
        # Seed an introductory idea in the Unassigned column
        Idea.objects.create(
            workspace=workspace,
            group=created_groups["Unassigned"],
            title="Aquí planeas \u270d\ufe0f tu contenido",
            description="Guarda tus ideas antes de convertirlas en publicaciones. Piensa, planea y mantén todo organizado en un solo lugar.",
            status=Idea.Status.UNASSIGNED,
            position=0,
        )
        groups = IdeaGroup.objects.for_workspace(workspace.id).order_by("position", "created_at")

    ideas_qs = (
        Idea.objects.for_workspace(workspace.id)
        .select_related("author", "media_asset")
        .prefetch_related("media_attachments__media_asset")
        .order_by("position", "-created_at")
    )
    if tag:
        ideas_qs = ideas_qs.filter(tags__contains=[tag])

    grouped_ideas = {str(grp.id): [] for grp in groups}
    for idea in ideas_qs:
        _prepare_idea_for_kanban(idea)

        group_key = str(idea.group_id) if idea.group_id else ""
        if group_key in grouped_ideas:
            grouped_ideas[group_key].append(idea)

    columns = []
    for grp in groups:
        columns.append(
            {
                "id": str(grp.id),
                "key": str(grp.id),
                "label": grp.name,
                "ideas": grouped_ideas.get(str(grp.id), []),
            }
        )

    # All workspace tags from the Tag model
    all_tags = list(Tag.objects.for_workspace(workspace.id).values_list("name", flat=True))

    return columns, all_tags


def _parse_media_asset_ids(raw_ids):
    """Parse ordered media ids from CSV, preserving order and removing duplicates."""
    ordered = []
    seen = set()
    for token in (raw_ids or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            normalized = str(uuid.UUID(token))
        except (ValueError, TypeError, AttributeError):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _build_idea_media_payload(idea):
    """Build ordered media payload and attachment list for Kanban rendering."""
    attachments = [att for att in idea.media_attachments.all() if att.media_asset_id and att.media_asset]
    media_payload = []
    for att in attachments:
        asset = att.media_asset
        media_payload.append(
            {
                "asset_id": str(asset.id),
                "url": asset.file.url if asset.file else "",
                "filename": asset.filename,
                "media_type": asset.media_type,
                "position": att.position,
            }
        )

    # Legacy fallback for ideas that only have the old single media pointer.
    if not media_payload and idea.media_asset_id and idea.media_asset:
        media_payload.append(
            {
                "asset_id": str(idea.media_asset_id),
                "url": idea.media_asset.file.url if idea.media_asset.file else "",
                "filename": idea.media_asset.filename,
                "media_type": idea.media_asset.media_type,
                "position": 0,
            }
        )
    return media_payload, attachments


def _prepare_idea_for_kanban(idea):
    """Attach computed media/tag fields used by Kanban templates."""
    media_payload, attachments = _build_idea_media_payload(idea)
    idea.media_payload_json = json.dumps(media_payload)
    idea.tags_payload_json = json.dumps(idea.tags or [])
    idea.media_count = len(media_payload)
    idea.cover_media = attachments[0].media_asset if attachments else idea.media_asset
    return idea


def _render_idea_card_fragment(request, idea):
    """Render a single Kanban idea card fragment."""
    _prepare_idea_for_kanban(idea)
    return render_to_string(
        "composer/partials/idea_card.html",
        {
            "idea": idea,
            "group_id": str(idea.group_id) if idea.group_id else "",
        },
        request=request,
    )


def _render_kanban_column_fragment(request, group):
    """Render a single Kanban column fragment."""
    return render_to_string(
        "composer/partials/kanban_column.html",
        {
            "col": {
                "id": str(group.id),
                "key": str(group.id),
                "label": group.name,
                "ideas": [],
            },
        },
        request=request,
    )


def _wants_json_response(request):
    """Detect whether mutation endpoint should return JSON payload."""
    accept = request.headers.get("Accept", "")
    return "application/json" in accept.lower()


def _normalize_media_asset_ids(raw_ids="", extra_ids=None):
    """Normalize media IDs from mixed inputs while preserving first-seen order."""
    chunks = []
    if isinstance(raw_ids, str):
        chunks.append(raw_ids)
    elif raw_ids:
        chunks.extend(str(item) for item in raw_ids)
    if extra_ids:
        chunks.extend(str(item) for item in extra_ids)
    return _parse_media_asset_ids(",".join(chunks))


def _sync_idea_media_attachments(idea, workspace, ordered_asset_ids):
    """Synchronize IdeaMedia rows to match ordered ids and keep cover pointer aligned."""
    from apps.media_library.models import MediaAsset

    normalized_ids = _normalize_media_asset_ids(ordered_asset_ids)
    if normalized_ids:
        valid_assets = set(
            str(aid)
            for aid in MediaAsset.objects.filter(workspace=workspace, id__in=normalized_ids).values_list(
                "id", flat=True
            )
        )
        ordered_ids = [aid for aid in normalized_ids if aid in valid_assets]
    else:
        ordered_ids = []

    existing_attachments = list(idea.media_attachments.all())
    existing = {str(att.media_asset_id): att for att in existing_attachments}
    ids_to_keep = set(ordered_ids)
    ids_to_delete = [att.id for att in existing_attachments if str(att.media_asset_id) not in ids_to_keep]
    if ids_to_delete:
        IdeaMedia.objects.filter(id__in=ids_to_delete).delete()

    to_create = []
    to_update = []
    now = timezone.now()
    for position, asset_id in enumerate(ordered_ids):
        attachment = existing.get(asset_id)
        if attachment:
            if attachment.position != position:
                attachment.position = position
                attachment.updated_at = now
                to_update.append(attachment)
            continue
        to_create.append(
            IdeaMedia(
                idea=idea,
                media_asset_id=asset_id,
                position=position,
            )
        )

    if to_create:
        IdeaMedia.objects.bulk_create(to_create)
    if to_update:
        IdeaMedia.objects.bulk_update(to_update, ["position", "updated_at"])

    cover_id = ordered_ids[0] if ordered_ids else None
    current_cover_id = str(idea.media_asset_id) if idea.media_asset_id else None
    if current_cover_id != cover_id:
        idea.media_asset_id = cover_id
        idea.save(update_fields=["media_asset", "updated_at"])


def _create_idea_media_asset(workspace, user, uploaded_file):
    """Create a MediaAsset from an uploaded file for Idea create/edit flows."""
    from apps.media_library.models import MediaAsset

    content_type = uploaded_file.content_type or ""
    if content_type == "image/gif":
        media_type = MediaAsset.MediaType.GIF
    elif content_type.startswith("image/"):
        media_type = MediaAsset.MediaType.IMAGE
    elif content_type.startswith("video/"):
        media_type = MediaAsset.MediaType.VIDEO
    else:
        media_type = MediaAsset.MediaType.DOCUMENT

    return MediaAsset.objects.create(
        organization=workspace.organization,
        workspace=workspace,
        uploaded_by=user,
        file=uploaded_file,
        filename=uploaded_file.name,
        media_type=media_type,
        mime_type=content_type,
        file_size=uploaded_file.size,
        source="upload",
    )


@login_required
@require_permission("create_posts")
def create_landing(request, workspace_id):
    """Render the Create landing page with Ideas Kanban board."""
    from apps.composer.builtin_templates import (
        CATEGORIES,
        get_all_templates,
        get_featured_templates,
    )

    workspace = _get_workspace(request, workspace_id)
    tab = request.GET.get("tab", "ideas")
    tag = request.GET.get("tag")

    columns, all_tags = _idea_columns(workspace, tag)

    context = {
        "workspace": workspace,
        "tab": tab,
        "columns": columns,
        "all_tags": all_tags,
        "active_tag": tag,
        "featured_templates": get_featured_templates(),
        "builtin_templates": get_all_templates(),
        "template_categories": CATEGORIES,
    }
    return render(request, "composer/create_landing.html", context)


@login_required
@require_permission("create_posts")
@require_POST
def idea_upload_media(request, workspace_id):
    """Upload media for Idea modals and return an asset id for reliable save binding."""
    workspace = _get_workspace(request, workspace_id)
    uploaded_file = request.FILES.get("file") or request.FILES.get("media")
    if not uploaded_file:
        return JsonResponse({"error": "No se envió ningún archivo"}, status=400)

    asset = _create_idea_media_asset(workspace, request.user, uploaded_file)
    return JsonResponse(
        {
            "asset_id": str(asset.id),
            "filename": asset.filename,
            "url": asset.file.url if asset.file else "",
            "size": asset.file_size,
            "media_type": asset.media_type,
        }
    )


@login_required
@require_permission("create_posts")
@require_POST
def idea_create(request, workspace_id):
    """Create a new idea via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    title = request.POST.get("title", "").strip()
    description = request.POST.get("description", "").strip()
    tags = parse_and_truncate_tag_string(request.POST.get("tags", ""))

    if not title:
        return HttpResponse("El título es obligatorio.", status=400)

    # Assign to the specified group or default to the first group
    group_id = request.POST.get("group")
    if group_id:
        group = IdeaGroup.objects.filter(id=group_id, workspace=workspace).first()
    else:
        group = IdeaGroup.objects.for_workspace(workspace.id).order_by("position").first()

    has_multi_media_payload = "media_asset_ids" in request.POST
    media_asset_ids = _normalize_media_asset_ids(request.POST.get("media_asset_ids", ""))

    # Handle media: pre-uploaded IDs (preferred), direct multipart upload fallback,
    # or legacy single pre-uploaded asset id.
    uploaded_file = request.FILES.get("media") or request.FILES.get("file")
    media_asset_id = request.POST.get("media_asset_id", "").strip()
    if uploaded_file:
        uploaded_asset = _create_idea_media_asset(workspace, request.user, uploaded_file)
        media_asset_ids.append(str(uploaded_asset.id))
    elif not media_asset_ids and media_asset_id:
        from apps.media_library.models import MediaAsset

        legacy_asset = MediaAsset.objects.filter(id=media_asset_id, workspace=workspace).first()
        if legacy_asset:
            media_asset_ids.append(str(legacy_asset.id))

    media_asset_ids = _normalize_media_asset_ids(media_asset_ids)

    with transaction.atomic():
        idea = Idea.objects.create(
            workspace=workspace,
            author=request.user,
            title=title,
            description=description,
            tags=tags,
            group=group,
            status=Idea.Status.UNASSIGNED,
            media_asset_id=media_asset_ids[0] if media_asset_ids else None,
        )

        if has_multi_media_payload or media_asset_ids:
            _sync_idea_media_attachments(idea, workspace, media_asset_ids)

    # Sync any new tags to the Tag model
    _sync_tags_to_model(workspace, tags)

    if _wants_json_response(request):
        idea = (
            Idea.objects.for_workspace(workspace.id)
            .select_related("media_asset")
            .prefetch_related("media_attachments__media_asset")
            .get(id=idea.id)
        )
        return JsonResponse(
            {
                "ok": True,
                "idea_id": str(idea.id),
                "group_id": str(idea.group_id) if idea.group_id else "",
                "tags": idea.tags or [],
                "card_html": _render_idea_card_fragment(request, idea),
            }
        )

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "ideaChanged"},
    )


@login_required
@require_permission("create_posts")
@require_POST
def idea_edit(request, workspace_id, idea_id):
    """Edit an existing idea via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    idea = get_object_or_404(Idea, id=idea_id, workspace=workspace)
    previous_group_id = str(idea.group_id) if idea.group_id else ""

    idea.title = request.POST.get("title", idea.title).strip()
    idea.description = request.POST.get("description", idea.description).strip()
    idea.tags = parse_and_truncate_tag_string(request.POST.get("tags", ""))

    group_id = request.POST.get("group", "").strip()
    if group_id:
        group = IdeaGroup.objects.filter(id=group_id, workspace=workspace).first()
        if group:
            idea.group = group

    # Handle media attachments:
    # - preferred: ordered media_asset_ids list
    # - fallback: legacy single media_asset_id + remove_media
    has_multi_media_payload = "media_asset_ids" in request.POST
    media_asset_ids = _normalize_media_asset_ids(request.POST.get("media_asset_ids", ""))
    uploaded_file = request.FILES.get("media") or request.FILES.get("file")
    uploaded_asset = None
    if uploaded_file:
        uploaded_asset = _create_idea_media_asset(workspace, request.user, uploaded_file)
        media_asset_ids.append(str(uploaded_asset.id))
    media_asset_ids = _normalize_media_asset_ids(media_asset_ids)

    with transaction.atomic():
        idea.save(update_fields=["title", "description", "tags", "group", "updated_at"])

        if has_multi_media_payload:
            _sync_idea_media_attachments(idea, workspace, media_asset_ids)
        else:
            media_asset_id = request.POST.get("media_asset_id", "").strip()
            remove_media = request.POST.get("remove_media") == "true"

            if uploaded_asset:
                _sync_idea_media_attachments(idea, workspace, [str(uploaded_asset.id)])
            elif media_asset_id:
                from apps.media_library.models import MediaAsset

                asset = MediaAsset.objects.filter(id=media_asset_id, workspace=workspace).first()
                if asset:
                    _sync_idea_media_attachments(idea, workspace, [str(asset.id)])
            elif remove_media:
                _sync_idea_media_attachments(idea, workspace, [])

    # Sync any new tags to the Tag model
    _sync_tags_to_model(workspace, idea.tags)

    if _wants_json_response(request):
        idea = (
            Idea.objects.for_workspace(workspace.id)
            .select_related("media_asset")
            .prefetch_related("media_attachments__media_asset")
            .get(id=idea.id)
        )
        return JsonResponse(
            {
                "ok": True,
                "idea_id": str(idea.id),
                "previous_group_id": previous_group_id,
                "group_id": str(idea.group_id) if idea.group_id else "",
                "tags": idea.tags or [],
                "card_html": _render_idea_card_fragment(request, idea),
            }
        )

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "ideaChanged"},
    )


@login_required
@require_permission("create_posts")
@require_POST
def idea_create_post(request, workspace_id, idea_id):
    """Create a new draft post from an idea and return composer redirect metadata."""
    workspace = _get_workspace(request, workspace_id)
    idea = get_object_or_404(
        Idea.objects.for_workspace(workspace.id)
        .select_related("media_asset")
        .prefetch_related("media_attachments__media_asset"),
        id=idea_id,
    )

    tags = []
    if isinstance(idea.tags, list):
        tags = [tag.strip() for tag in idea.tags if isinstance(tag, str) and tag.strip()]

    ordered_media_asset_ids = []
    seen_media_ids = set()
    for attachment in idea.media_attachments.all():
        if not attachment.media_asset_id:
            continue
        media_id = str(attachment.media_asset_id)
        if media_id in seen_media_ids:
            continue
        seen_media_ids.add(media_id)
        ordered_media_asset_ids.append(media_id)

    # Legacy fallback: old ideas may only have the single media pointer set.
    if not ordered_media_asset_ids and idea.media_asset_id:
        ordered_media_asset_ids.append(str(idea.media_asset_id))

    connected_accounts = list(
        SocialAccount.objects.for_workspace(workspace.id)
        .filter(connection_status=SocialAccount.ConnectionStatus.CONNECTED)
        .order_by("platform", "account_name", "id")
    )

    with transaction.atomic():
        post = Post.objects.create(
            workspace=workspace,
            author=request.user,
            title=idea.title or "",
            caption=idea.description or "",
            tags=tags,
        )

        if ordered_media_asset_ids:
            PostMedia.objects.bulk_create(
                [
                    PostMedia(
                        post=post,
                        media_asset_id=asset_id,
                        position=index,
                    )
                    for index, asset_id in enumerate(ordered_media_asset_ids)
                ]
            )

        if connected_accounts:
            PlatformPost.objects.bulk_create(
                [
                    PlatformPost(
                        post=post,
                        social_account=account,
                    )
                    for account in connected_accounts
                ]
            )

        idea.post = post
        idea.save(update_fields=["post", "updated_at"])

    from django.urls import reverse

    compose_url = reverse("composer:compose_edit", kwargs={"workspace_id": workspace.id, "post_id": post.id})
    return JsonResponse(
        {
            "ok": True,
            "post_id": str(post.id),
            "compose_url": compose_url,
        }
    )


@login_required
@require_permission("create_posts")
@require_POST
def idea_delete(request, workspace_id, idea_id):
    """Delete an idea via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    idea = get_object_or_404(Idea, id=idea_id, workspace=workspace)
    group_id = str(idea.group_id) if idea.group_id else ""
    idea.delete()

    if _wants_json_response(request):
        return JsonResponse(
            {
                "ok": True,
                "idea_id": str(idea_id),
                "group_id": group_id,
            }
        )

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "ideaChanged"},
    )


@login_required
@require_permission("create_posts")
@require_POST
def idea_move(request, workspace_id, idea_id):
    """Move an idea to a new column/position via HTMX (drag-and-drop)."""
    workspace = _get_workspace(request, workspace_id)
    idea = get_object_or_404(Idea, id=idea_id, workspace=workspace)
    new_group_id = request.POST.get("group")
    new_position = request.POST.get("position")

    # Support both group-based and legacy status-based moves
    if new_group_id:
        group = IdeaGroup.objects.filter(id=new_group_id, workspace=workspace).first()
        if group:
            idea.group = group
    else:
        new_status = request.POST.get("status")
        if new_status and new_status in dict(Idea.Status.choices):
            idea.status = new_status
    if new_position is not None:
        with contextlib.suppress(ValueError, TypeError):
            idea.position = int(new_position)
    idea.save()

    # No HX-Trigger here - the frontend handles the move optimistically
    return HttpResponse(status=204)


@login_required
@require_permission("create_posts")
@require_GET
def idea_board(request, workspace_id):
    """Return the Kanban board partial for HTMX refresh."""
    workspace = _get_workspace(request, workspace_id)
    tag = request.GET.get("tag")
    columns, all_tags = _idea_columns(workspace, tag)

    return render(
        request,
        "composer/partials/kanban_board.html",
        {
            "workspace": workspace,
            "columns": columns,
            "all_tags": all_tags,
            "active_tag": tag,
        },
    )


# ---------------------------------------------------------------------------
# Idea Group CRUD (Kanban columns)
# ---------------------------------------------------------------------------


@login_required
@require_permission("create_posts")
@require_POST
def idea_group_create(request, workspace_id):
    """Create a new Kanban column via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    name = request.POST.get("name", "").strip()
    if not name:
        if _wants_json_response(request):
            return JsonResponse({"error": "El nombre es obligatorio."}, status=400)
        return HttpResponse("El nombre es obligatorio.", status=400)

    max_pos = IdeaGroup.objects.for_workspace(workspace.id).aggregate(models.Max("position"))["position__max"] or 0
    group = IdeaGroup.objects.create(workspace=workspace, name=name, position=max_pos + 1)

    if _wants_json_response(request):
        return JsonResponse(
            {
                "ok": True,
                "group_id": str(group.id),
                "group_name": group.name,
                "column_html": _render_kanban_column_fragment(request, group),
            }
        )

    return HttpResponse(status=204, headers={"HX-Trigger": "ideaChanged"})


@login_required
@require_permission("create_posts")
@require_POST
def idea_group_delete(request, workspace_id, group_id):
    """Delete an empty Kanban column via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    group = get_object_or_404(IdeaGroup, id=group_id, workspace=workspace)

    if group.ideas.exists():
        if _wants_json_response(request):
            return JsonResponse({"error": "La columna debe estar vacía antes de eliminarla."}, status=400)
        return HttpResponse("La columna debe estar vacía antes de eliminarla.", status=400)

    deleted_group_id = str(group.id)
    group.delete()
    if _wants_json_response(request):
        return JsonResponse({"ok": True, "group_id": deleted_group_id})
    return HttpResponse(status=204, headers={"HX-Trigger": "ideaChanged"})


@login_required
@require_permission("create_posts")
@require_POST
def idea_group_reorder(request, workspace_id):
    """Reorder Kanban columns. Expects JSON body: {"order": ["uuid1", "uuid2", ...]}."""
    workspace = _get_workspace(request, workspace_id)
    try:
        data = json.loads(request.body)
        order = data.get("order", [])
    except (json.JSONDecodeError, AttributeError):
        return HttpResponse("JSON no válido.", status=400)

    if not order:
        return HttpResponse(status=204)

    groups = {str(g.id): g for g in IdeaGroup.objects.for_workspace(workspace.id)}
    for position, group_id in enumerate(order):
        group = groups.get(group_id)
        if group and group.position != position:
            group.position = position
            group.save(update_fields=["position"])

    return HttpResponse(status=204)


# ---------------------------------------------------------------------------
# Content Categories CRUD
# ---------------------------------------------------------------------------


@login_required
def category_list(request, workspace_id):
    """Settings page for managing content categories."""
    workspace = _get_workspace(request, workspace_id)
    categories = ContentCategory.objects.for_workspace(workspace.id)
    form = ContentCategoryForm()

    return render(
        request,
        "composer/categories.html",
        {
            "workspace": workspace,
            "categories": categories,
            "form": form,
        },
    )


@login_required
@require_POST
@require_permission("create_posts")
def category_create(request, workspace_id):
    """Create a new content category via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    form = ContentCategoryForm(request.POST)

    if not form.is_valid():
        return HttpResponse("Datos no válidos.", status=400)

    category = form.save(commit=False)
    category.workspace = workspace
    max_pos = ContentCategory.objects.for_workspace(workspace.id).aggregate(models.Max("position"))["position__max"]
    category.position = (max_pos or 0) + 1
    category.save()

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "categoryChanged"},
    )


@login_required
@require_POST
@require_permission("create_posts")
def category_edit(request, workspace_id, category_id):
    """Edit a content category via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    category = get_object_or_404(ContentCategory, id=category_id, workspace=workspace)
    form = ContentCategoryForm(request.POST, instance=category)

    if not form.is_valid():
        return HttpResponse("Datos no válidos.", status=400)

    form.save()
    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "categoryChanged"},
    )


@login_required
@require_POST
@require_permission("create_posts")
def category_delete(request, workspace_id, category_id):
    """Delete a content category via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    category = get_object_or_404(ContentCategory, id=category_id, workspace=workspace)
    category.delete()

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "categoryChanged"},
    )


# ---------------------------------------------------------------------------
# Post Templates
# ---------------------------------------------------------------------------


@login_required
def template_list(request, workspace_id):
    """Settings page for managing post templates."""
    workspace = _get_workspace(request, workspace_id)
    templates = PostTemplate.objects.for_workspace(workspace.id).select_related("created_by")

    return render(
        request,
        "composer/templates_list.html",
        {
            "workspace": workspace,
            "templates": templates,
        },
    )


#: platform_extra keys that are user settings (the rest is publish output) and travel with a template.
_TEMPLATE_EXTRA_KEYS = {
    "privacy_status", "self_declared_made_for_kids", "tags", "thumbnail_asset_id", "post_type", "board_id",
    "link_url", "alt_text", "tag_products", "allow_comments", "show_similar_products", "cover_image_asset_id",
    "disable_comment", "disable_duet", "disable_stitch", "brand_organic_toggle", "brand_content_toggle",
    "is_aigc", "privacy_level", "video_cover_timestamp_ms", "category_id", "playlist_id",
    "contains_synthetic_media", "solo_organico", "collaborators", "share_to_feed",
}  # fmt: skip


@login_required
@require_POST
@require_permission("create_posts")
def save_as_template(request, workspace_id, post_id):
    """Save the current post as a reusable template."""
    workspace = _get_workspace(request, workspace_id)
    post = get_object_or_404(Post, id=post_id, workspace=workspace)

    name = request.POST.get("template_name", "").strip()
    if not name:
        name = f"Plantilla de {post.caption_snippet or 'publicación'}"

    description = request.POST.get("template_description", "").strip()

    platform_posts = list(post.platform_posts.all())
    template_data = {
        "title": post.title,
        "caption": post.caption,
        "first_comment": post.first_comment,
        "category_id": str(post.category_id) if post.category_id else None,
        "tags": post.tags,
        "platform_account_ids": [str(pp.social_account_id) for pp in platform_posts],
        "media_asset_ids": [str(pm.media_asset_id) for pm in post.media_attachments.all()],
        # Per-account settings; publish results merged into platform_extra (ids, etags…) are left out.
        "platform_extras": {
            str(pp.social_account_id): {k: v for k, v in (pp.platform_extra or {}).items() if k in _TEMPLATE_EXTRA_KEYS}
            for pp in platform_posts
        },
        "platform_overrides": {
            str(pp.social_account_id): {
                "title": pp.platform_specific_title or "",
                "caption": pp.platform_specific_caption or "",
                "comment": pp.platform_specific_first_comment or "",
            }
            for pp in platform_posts
        },
    }

    PostTemplate.objects.create(
        workspace=workspace,
        name=name,
        description=description,
        template_data=template_data,
        created_by=request.user,
    )

    if request.htmx:
        return HttpResponse(
            status=204,
            headers={"HX-Trigger": "templateSaved"},
        )
    return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post.id)


@login_required
@require_POST
@require_permission("create_posts")
def template_delete(request, workspace_id, template_id):
    """Delete a post template."""
    workspace = _get_workspace(request, workspace_id)
    tpl = get_object_or_404(PostTemplate, id=template_id, workspace=workspace)
    tpl.delete()

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "templateChanged"},
    )


@login_required
@require_GET
def template_picker(request, workspace_id):
    """HTMX partial returning list of templates for the picker modal."""
    workspace = _get_workspace(request, workspace_id)
    templates = PostTemplate.objects.for_workspace(workspace.id).select_related("created_by")

    return render(
        request,
        "composer/partials/template_picker.html",
        {
            "workspace": workspace,
            "templates": templates,
        },
    )


@login_required
@require_GET
def use_template(request, workspace_id, template_id):
    """Redirect to composer with template data pre-filled."""
    workspace = _get_workspace(request, workspace_id)
    get_object_or_404(PostTemplate, id=template_id, workspace=workspace)

    from django.urls import reverse

    compose_url = reverse("composer:compose", kwargs={"workspace_id": workspace.id})
    return redirect(f"{compose_url}?template={template_id}")


# ---------------------------------------------------------------------------
# CSV Import
# ---------------------------------------------------------------------------


def _read_csv_rows(raw: bytes) -> list[list[str]]:
    """Parse CSV bytes from Excel or Sheets: UTF-8 (BOM) or cp1252, delimiter ``,`` ``;`` or tab."""
    import csv
    import io

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")  # Excel en español guarda así
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;	")
    except csv.Error:
        dialect = csv.excel
    return list(csv.reader(io.StringIO(text), dialect))


def _csv_cell(row, mapping, field):
    idx = mapping.get(field)
    return row[idx].strip() if idx is not None and idx < len(row) else ""


def _import_media_from_url(workspace, user, url):
    """Download a public http(s) image/video and add it to the media library (validated like an upload)."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    from apps.common.validators import is_safe_url
    from apps.media_library.services import create_asset

    if not is_safe_url(url):
        raise ValueError("URL de medio no válida")
    max_bytes = 50 * 1024 * 1024
    data = bytearray()
    try:
        with httpx.stream("GET", url, timeout=20, follow_redirects=False) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_bytes():
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise ValueError("El medio es demasiado grande")
    except httpx.HTTPError as exc:
        raise ValueError("No se pudo descargar el medio") from exc
    name = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "medio"
    return create_asset(
        organization=workspace.organization,
        workspace=workspace,
        uploaded_file=SimpleUploadedFile(name, bytes(data)),
        uploaded_by=user,
    )


@login_required
@require_permission("create_posts")
def csv_upload(request, workspace_id):
    """Render CSV upload page or handle file upload and show column mapping."""
    workspace = _get_workspace(request, workspace_id)

    if request.method == "POST" and request.FILES.get("csv_file"):
        csv_file = request.FILES["csv_file"]
        if csv_file.size and csv_file.size > MAX_CSV_UPLOAD_BYTES:
            return render(
                request,
                "composer/csv_import.html",
                {"workspace": workspace, "error": "El archivo CSV es demasiado grande (máx. 5 MB)."},
            )
        rows = _read_csv_rows(csv_file.read())

        if not rows:
            return render(
                request,
                "composer/csv_import.html",
                {"workspace": workspace, "error": "El archivo CSV está vacío."},
            )

        headers = rows[0]
        preview_rows = rows[1:6]  # First 5 data rows

        # Auto-detect column mapping
        field_map = {
            "date": ["date", "publish_date", "scheduled_date"],
            "time": ["time", "publish_time", "scheduled_time"],
            "platforms": ["platform", "platforms", "channel", "channels"],
            "caption": ["caption", "text", "content", "message", "body"],
            "media_url": ["media_url", "media", "image_url", "image", "video_url"],
            "category": ["category", "content_category", "type"],
            "tags": ["tags", "labels", "tag"],
            "first_comment": ["first_comment", "comment"],
        }

        auto_mapping = {}
        for col_idx, header in enumerate(headers):
            header_lower = header.strip().lower().replace(" ", "_")
            for field, aliases in field_map.items():
                if header_lower in aliases:
                    auto_mapping[field] = col_idx
                    break

        # Store CSV in session for the next step
        request.session[f"csv_import_{workspace.id}"] = {
            "headers": headers,
            "rows": rows[1:],  # Exclude header
            "filename": csv_file.name,
        }

        return render(
            request,
            "composer/partials/csv_mapping.html",
            {
                "workspace": workspace,
                "headers": headers,
                "preview_rows": preview_rows,
                "auto_mapping": auto_mapping,
                "field_choices": list(field_map.keys()),
            },
        )

    return render(
        request,
        "composer/csv_import.html",
        {"workspace": workspace},
    )


@login_required
@require_permission("create_posts")
@require_POST
def csv_preview(request, workspace_id):
    """Validate CSV rows with the selected column mapping and show preview."""
    workspace = _get_workspace(request, workspace_id)
    csv_data = request.session.get(f"csv_import_{workspace.id}")

    if not csv_data:
        return HttpResponse("No se encontraron datos del CSV. Vuelve a subir el archivo.", status=400)

    # Parse column mapping from POST
    mapping = {}
    for field in ["date", "time", "platforms", "caption", "media_url", "category", "tags", "first_comment"]:
        col_idx = request.POST.get(f"map_{field}", "")
        if col_idx != "":
            with contextlib.suppress(ValueError, TypeError):
                mapping[field] = int(col_idx)

    rows = csv_data["rows"]
    errors = []
    valid_count = 0

    from apps.credentials.models import PlatformCredential
    from apps.social_accounts.models import SocialAccount

    valid_platforms = {p[0].lower() for p in PlatformCredential.Platform.choices}
    connected_accounts = set(
        SocialAccount.objects.for_workspace(workspace.id)
        .filter(connection_status=SocialAccount.ConnectionStatus.CONNECTED)
        .values_list("platform", flat=True)
    )

    for row_idx, row in enumerate(rows, start=2):  # Row 2 = first data row
        row_errors = []

        # Validate date
        if "date" in mapping:
            date_val = _csv_cell(row, mapping, "date")
            if date_val:
                try:
                    datetime.strptime(date_val, "%Y-%m-%d")
                except ValueError:
                    row_errors.append(f"Fecha no válida '{date_val}' (formato esperado AAAA-MM-DD)")
            else:
                row_errors.append("La fecha está vacía")
            time_val = _csv_cell(row, mapping, "time")
            if time_val:
                try:
                    datetime.strptime(time_val, "%H:%M")
                except ValueError:
                    row_errors.append(f"Hora no válida '{time_val}' (formato esperado HH:MM)")

        # Validate platforms: every row needs at least one connected account.
        platforms_val = _csv_cell(row, mapping, "platforms")
        platforms = [p.strip().lower() for p in platforms_val.split(",") if p.strip()]
        if not platforms:
            row_errors.append("La fila no tiene plataformas")
        for p in platforms:
            if p not in valid_platforms:
                row_errors.append(f"Plataforma desconocida '{p}'")
            elif p not in connected_accounts:
                row_errors.append(f"La plataforma '{p}' no está conectada")

        # Validate caption
        if not _csv_cell(row, mapping, "caption"):
            row_errors.append("El texto está vacío")

        if row_errors:
            errors.append({"row": row_idx, "errors": row_errors})
        else:
            valid_count += 1

    # Store mapping in session
    request.session[f"csv_mapping_{workspace.id}"] = mapping

    return render(
        request,
        "composer/partials/csv_validation.html",
        {
            "workspace": workspace,
            "total_rows": len(rows),
            "valid_count": valid_count,
            "errors": errors[:50],  # Show max 50 errors
            "has_more_errors": len(errors) > 50,
        },
    )


@login_required
@require_permission("create_posts")
@require_POST
def csv_confirm_import(request, workspace_id):
    """Import the validated CSV rows; a bad row is skipped (and reported) without touching the rest."""
    workspace = _get_workspace(request, workspace_id)
    csv_data = request.session.get(f"csv_import_{workspace.id}")
    mapping = request.session.get(f"csv_mapping_{workspace.id}")

    if not csv_data or not mapping:
        return HttpResponse("No se encontraron datos del CSV. Vuelve a subir el archivo.", status=400)

    import zoneinfo
    from datetime import time as time_cls

    from apps.composer.services import _APPROVAL_MODES_BLOCKING_DIRECT_SCHEDULE
    from apps.social_accounts.models import SocialAccount

    # With mandatory approval, only members with publish_directly may schedule; for
    # everyone else a dated row becomes a draft that keeps the date as its proposal.
    membership = request.workspace_membership
    perms = membership.effective_permissions if membership else {}
    may_schedule = workspace.approval_workflow_mode not in _APPROVAL_MODES_BLOCKING_DIRECT_SCHEDULE or perms.get(
        "publish_directly", False
    )
    tz = zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC")
    allowed_ids = _publishable_account_ids(request, workspace)

    rows = csv_data["rows"]
    created_count = 0
    drafted_for_approval = 0
    row_errors = []

    for row_idx, row in enumerate(rows, start=2):
        try:
            caption = _csv_cell(row, mapping, "caption")
            if not caption:
                raise ValueError("El texto está vacío")

            platforms = [p.strip().lower() for p in _csv_cell(row, mapping, "platforms").split(",") if p.strip()]
            accounts = [
                acc
                for acc in SocialAccount.objects.filter(
                    workspace=workspace,
                    platform__in=platforms,
                    connection_status=SocialAccount.ConnectionStatus.CONNECTED,
                )
                if str(acc.id) in allowed_ids
            ]
            if not accounts:
                raise ValueError("La fila no tiene cuentas conectadas a las que puedas publicar")

            scheduled_at = None
            date_str = _csv_cell(row, mapping, "date")
            if date_str:
                time_str = _csv_cell(row, mapping, "time")
                d = datetime.strptime(date_str, "%Y-%m-%d").date()
                t = datetime.strptime(time_str, "%H:%M").time() if time_str else time_cls(9, 0)
                scheduled_at = datetime.combine(d, t).replace(tzinfo=tz)

            media_url = _csv_cell(row, mapping, "media_url")
            tags_raw = _csv_cell(row, mapping, "tags")
            cat_name = _csv_cell(row, mapping, "category")

            with transaction.atomic():
                post = Post(
                    workspace=workspace,
                    author=request.user,
                    caption=caption,
                    first_comment=_csv_cell(row, mapping, "first_comment"),
                )
                if tags_raw:
                    post.tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
                if cat_name:
                    post.category, _ = ContentCategory.objects.get_or_create(
                        workspace=workspace, name=cat_name, defaults={"color": "#3B82F6"}
                    )
                initial_pp_status = "draft"
                if scheduled_at and may_schedule:
                    post.scheduled_at = scheduled_at
                    initial_pp_status = "scheduled"
                elif scheduled_at:
                    post.proposed_publish_at = scheduled_at
                post.save()

                media_types = []
                if media_url:
                    asset = _import_media_from_url(workspace, request.user, media_url)
                    PostMedia.objects.create(post=post, media_asset=asset, position=0)
                    media_types.append(asset.media_type)
                if initial_pp_status == "scheduled":
                    for acc in accounts:
                        media_err = media_error_for_platform(acc.platform, media_types)
                        if media_err:
                            raise ValueError(media_err)

                for acc in accounts:
                    PlatformPost.objects.create(
                        post=post,
                        social_account=acc,
                        status=initial_pp_status,
                        scheduled_at=post.scheduled_at,
                    )
            if scheduled_at and not may_schedule:
                drafted_for_approval += 1
            created_count += 1
        except ValidationError as exc:
            row_errors.append({"row": row_idx, "errors": [" ".join(exc.messages)]})
        except ValueError as exc:
            row_errors.append({"row": row_idx, "errors": [str(exc)]})
        except Exception:
            logger.exception("CSV import: row %s failed", row_idx)
            row_errors.append({"row": row_idx, "errors": ["Error inesperado al importar la fila"]})

    # Clean up session data
    request.session.pop(f"csv_import_{workspace.id}", None)
    request.session.pop(f"csv_mapping_{workspace.id}", None)

    return render(
        request,
        "composer/partials/csv_progress.html",
        {
            "workspace": workspace,
            "created_count": created_count,
            "error_count": len(row_errors),
            "row_errors": row_errors[:50],
            "drafted_for_approval": drafted_for_approval,
            "total_rows": len(rows),
        },
    )


# ---------------------------------------------------------------------------
# Tag CRUD (JSON API endpoints)
# ---------------------------------------------------------------------------


def _sync_tags_to_model(workspace, tag_names):
    """Ensure Tag records exist for all given tag names in a workspace."""
    if not tag_names:
        return
    existing = set(Tag.objects.for_workspace(workspace.id).filter(name__in=tag_names).values_list("name", flat=True))
    new_tags = [Tag(workspace=workspace, name=name) for name in tag_names if name not in existing]
    if new_tags:
        Tag.objects.bulk_create(new_tags, ignore_conflicts=True)


@login_required
@require_GET
def tag_list(request, workspace_id):
    """Return workspace tags as JSON, optionally filtered by search query."""
    workspace = _get_workspace(request, workspace_id)
    q = request.GET.get("q", "").strip().lower()
    tags = Tag.objects.for_workspace(workspace.id)
    if q:
        tags = tags.filter(name__icontains=q)
    tag_data = [{"id": str(t.id), "name": t.name} for t in tags[:50]]
    return JsonResponse(tag_data, safe=False)


@login_required
@require_POST
@require_permission("create_posts")
def tag_create(request, workspace_id):
    """Create a new tag and return it as JSON."""
    workspace = _get_workspace(request, workspace_id)
    name = request.POST.get("name", "").strip()
    if not name:
        return JsonResponse({"error": "El nombre de la etiqueta es obligatorio."}, status=400)
    tag, created = Tag.objects.get_or_create(workspace=workspace, name=name)
    return JsonResponse({"id": str(tag.id), "name": tag.name, "created": created})
