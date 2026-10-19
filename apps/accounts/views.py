import base64
import hashlib
import hmac
import json
import uuid

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from PIL import Image

from apps.social_accounts.models import SocialAccount


def health_check(request):
    """Health check endpoint at /health/."""
    return JsonResponse({"status": "ok"})


TIKTOK_VERIFICATION_CONTENT = "tiktok-developers-site-verification=bTJ2ph9pXHrSfpXBB779Umzy76k70vQY\n"
DELETION_SALT = "bean.data-deletion"


def _public(request, template, **extra):
    return render(request, template, {"contact_email": settings.PUBLIC_CONTACT_EMAIL, **extra})


def public_review(request):
    """Public overview for platform reviewers and authorized company users."""
    return _public(request, "public/review.html")


# Responsable y domicilio de la página legal: una sola fuente para las 4+ menciones del texto.
LEGAL_RESPONSABLE = {
    "es": "Darío Abraham Curiel Niño, persona física con actividad y responsable único de ITHOS "
    "(ITHOS y BEAN by ITHOS son nombres comerciales), RFC CUND000714T89",
    "en": "Darío Abraham Curiel Niño, an individual with business activity and sole owner of ITHOS "
    "(ITHOS and BEAN by ITHOS are trade names), RFC CUND000714T89",
}
LEGAL_DOMICILIO = (
    "Calle Diego Rivera 100, Colonia Lomas de Tlaquepaque, C.P. 45559, San Pedro Tlaquepaque, Jalisco, México"
)


def public_legal(request):
    """Página legal única (ES/EN) con anclas #privacidad #terminos #borrado-de-datos #contacto."""
    return _public(request, "public/legal.html", responsable=LEGAL_RESPONSABLE, domicilio=LEGAL_DOMICILIO)


def _legal_redirect(anchor):
    def view(request):
        return redirect(f"{reverse('public_legal')}#{anchor}", permanent=True)

    return view


public_privacy = _legal_redirect("privacidad")
public_terms = _legal_redirect("terminos")
public_support = _legal_redirect("contacto")
public_data_deletion = _legal_redirect("borrado-de-datos")


def _parse_signed_request(signed_request):
    """Return the payload of a Meta signed_request, or None if malformed or badly signed."""
    secret = (settings.PLATFORM_CREDENTIALS_FROM_ENV.get("facebook") or {}).get("app_secret", "")
    if not secret:
        raise ImproperlyConfigured("Meta app secret is not configured")
    try:
        encoded_sig, payload = signed_request.split(".", 1)
        pad = lambda v: v + "=" * (-len(v) % 4)  # noqa: E731
        sig = base64.urlsafe_b64decode(pad(encoded_sig))
        data = json.loads(base64.urlsafe_b64decode(pad(payload)))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or str(data.get("algorithm", "")).upper() != "HMAC-SHA256":
        return None
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return data if hmac.compare_digest(sig, expected) else None


def _meta_callback(request):
    """Shared by both Meta callbacks: returns (user_id, None) or (None, error response)."""
    try:
        data = _parse_signed_request(request.POST.get("signed_request", ""))
    except ImproperlyConfigured:
        return None, HttpResponse(status=503)
    if not data or not data.get("user_id"):
        return None, HttpResponse(status=400)
    user_id = str(data["user_id"])
    # Match the app-scoped user id saved at connect time; account_platform_id covers accounts
    # connected before that field existed (and the odd case where the ids coincide).
    for account in SocialAccount.objects.filter(
        Q(platform_user_id=user_id) | Q(account_platform_id=user_id), platform__in=("facebook", "instagram")
    ).exclude(connection_status=SocialAccount.ConnectionStatus.ARCHIVED):
        account.archive()
    return user_id, None


@csrf_exempt
@require_POST
def meta_data_deletion_callback(request):
    """Meta data-deletion callback: archive matching accounts (clears tokens) and return a status URL."""
    _, error = _meta_callback(request)
    if error:
        return error
    code = signing.dumps({"id": uuid.uuid4().hex}, salt=DELETION_SALT)  # opaque: no Meta user id
    url = request.build_absolute_uri(reverse("public_data_deletion_status", args=[code]))
    return JsonResponse({"url": url, "confirmation_code": code})


@csrf_exempt
@require_POST
def meta_deauthorize_callback(request):
    """Meta deauthorize callback: same effect as deletion, no status page."""
    _, error = _meta_callback(request)
    return error or HttpResponse(status=200)


def public_data_deletion_status(request, code):
    """Validate a confirmation code issued by the deletion callback."""
    try:
        signing.loads(code, salt=DELETION_SALT)
    except signing.BadSignature:
        raise Http404 from None
    return _public(request, "public/data_deletion_status.html")


@require_GET
def tiktok_site_verification(request):
    """Serve TikTok's verification token at the domain root."""
    return HttpResponse(TIKTOK_VERIFICATION_CONTENT, content_type="text/plain; charset=utf-8")


def home(request):
    """Public landing for anonymous visitors; the dashboard for signed-in users."""
    if request.user.is_authenticated:
        return dashboard(request)
    return _public(request, "public/landing.html")


@login_required
def dashboard(request):
    """Main dashboard - redirects to last used workspace or shows org overview."""
    from apps.members.models import WorkspaceMembership

    user = request.user

    # Only trust last_workspace_id if the user still has an active membership
    # in that workspace. Otherwise the org may have been deleted out from under
    # them and we'd redirect into a 403.
    if user.last_workspace_id:
        if WorkspaceMembership.objects.filter(
            user=user,
            workspace_id=user.last_workspace_id,
            workspace__is_archived=False,
        ).exists():
            return redirect("calendar:calendar", workspace_id=user.last_workspace_id)
        user.last_workspace_id = None
        user.save(update_fields=["last_workspace_id"])

    # Fallback: try to find any workspace the user belongs to
    membership = (
        WorkspaceMembership.objects.filter(user=user, workspace__is_archived=False).select_related("workspace").first()
    )
    if membership:
        user.last_workspace_id = membership.workspace.id
        user.save(update_fields=["last_workspace_id"])
        return redirect("calendar:calendar", workspace_id=membership.workspace.id)

    return render(request, "accounts/dashboard.html")


@login_required
@require_http_methods(["GET", "POST"])
def account_settings(request):
    user = request.user
    tab = request.GET.get("tab", "profile")
    settings_active = "preferences" if tab == "preferences" else "profile"

    if request.method == "POST":
        action = request.POST.get("action", "")

        if action == "update_photo":
            _handle_photo_update(request, user)
        elif action == "update_name":
            _handle_name_update(request, user)
        elif action == "update_password":
            _handle_password_update(request, user)
        elif action == "delete_account":
            return _handle_account_deletion(request, user)

        return redirect("accounts:settings")

    # Fetch the user's organization membership for role display
    from apps.members.models import OrgMembership

    org_membership = OrgMembership.objects.filter(user=user).select_related("organization").first()

    return render(
        request,
        "accounts/settings.html",
        {
            "settings_active": settings_active,
            "org_membership": org_membership,
        },
    )


def _handle_photo_update(request, user):
    """Handle avatar upload or deletion."""
    # Handle deletion
    if request.POST.get("delete_photo") == "1":
        if user.avatar:
            user.avatar.delete(save=False)
        user.save()
        messages.success(request, "Foto eliminada.")
        return

    # Handle upload
    if "avatar" not in request.FILES:
        return

    avatar = request.FILES["avatar"]

    # Validate file type
    allowed_types = ("image/jpeg", "image/png", "image/webp", "image/gif")
    if avatar.content_type not in allowed_types:
        messages.error(request, "La foto debe ser una imagen JPEG, PNG, WebP o GIF.")
        return

    # Validate file size (2 MB max)
    max_size = 2 * 1024 * 1024
    if avatar.size > max_size:
        messages.error(request, "La foto debe pesar menos de 2 MB.")
        return

    # Validate minimum dimensions (180x180)
    try:
        img = Image.open(avatar)
        width, height = img.size
        if width < 180 or height < 180:
            messages.error(request, "La foto debe ser de al menos 180×180 píxeles.")
            return
    except Exception:
        messages.error(request, "No se pudo leer el archivo de imagen.")
        return
    finally:
        avatar.seek(0)  # Reset file pointer after reading

    # Delete old avatar before saving new one
    if user.avatar:
        user.avatar.delete(save=False)

    user.avatar = avatar
    user.save()
    messages.success(request, "Foto actualizada.")


def _handle_name_update(request, user):
    """Handle name change."""
    name = request.POST.get("name", "").strip()
    if not name:
        messages.error(request, "El nombre no puede estar vacío.")
        return

    user.name = name
    user.save(update_fields=["name"])
    messages.success(request, "Nombre actualizado.")


def _handle_password_update(request, user):
    """Handle password change."""
    current_password = request.POST.get("current_password", "")
    password = request.POST.get("password", "")
    password_confirm = request.POST.get("password_confirm", "")

    if not current_password:
        messages.error(request, "La contraseña actual es obligatoria.")
        return

    if not user.check_password(current_password):
        messages.error(request, "La contraseña actual es incorrecta.")
        return

    if not password:
        messages.error(request, "La nueva contraseña no puede estar vacía.")
        return

    if len(password) < 8:
        messages.error(request, "La nueva contraseña debe tener al menos 8 caracteres.")
        return

    if password != password_confirm:
        messages.error(request, "Las contraseñas nuevas no coinciden.")
        return

    user.set_password(password)
    user.save()
    update_session_auth_hash(request, user)
    messages.success(request, "Contraseña actualizada.")


def _handle_account_deletion(request, user):
    """Handle account deletion with sole-owner safety check."""
    from apps.members.models import OrgMembership

    # Check if user is the sole owner of any organization
    owned_memberships = OrgMembership.objects.filter(user=user, org_role=OrgMembership.OrgRole.OWNER).select_related(
        "organization"
    )

    sole_owner_orgs = []
    for membership in owned_memberships:
        other_owners = (
            OrgMembership.objects.filter(
                organization=membership.organization,
                org_role=OrgMembership.OrgRole.OWNER,
            )
            .exclude(user=user)
            .exists()
        )
        if not other_owners:
            sole_owner_orgs.append(membership.organization.name)

    if sole_owner_orgs:
        org_names = ", ".join(sole_owner_orgs)
        messages.error(
            request,
            f"Eres el único propietario de: {org_names}. "
            "Transfiere la propiedad o elimina la organización antes de eliminar tu cuenta.",
        )
        return redirect("accounts:settings")

    # Safe to delete
    user.delete()
    logout(request)
    messages.success(request, "Tu cuenta fue eliminada.")
    return redirect("account_login")


@login_required
@require_http_methods(["GET", "POST"])
def accept_terms(request):
    """Terms of Service acceptance page for social signup users."""
    if request.user.tos_accepted_at is not None:
        return redirect("/")

    if request.method == "POST":
        if request.POST.get("agree"):
            request.user.tos_accepted_at = timezone.now()
            request.user.save(update_fields=["tos_accepted_at"])
            return redirect("/")
        messages.error(request, "Debes aceptar los Términos del Servicio y la Política de Privacidad para continuar.")

    return render(request, "account/accept_terms.html")


def logout_view(request):
    logout(request)
    return redirect("account_login")
