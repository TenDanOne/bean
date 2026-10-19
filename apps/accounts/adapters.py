import logging

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib import messages
from django.shortcuts import redirect

from apps.accounts.models import OAuthConnection

logger = logging.getLogger(__name__)


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    """Custom adapter that syncs Google social logins to OAuthConnection."""

    def on_authentication_error(self, request, provider, error=None, exception=None, extra_context=None):
        """Log why a social login failed (allauth's 401 page says nothing) and send the user back to login."""
        logger.warning(
            "Social login failed (provider=%s, error=%s): %r", getattr(provider, "id", provider), error, exception
        )
        messages.error(request, "No se pudo entrar con Google. Intenta de nuevo o usa tu correo y contraseña.")
        raise ImmediateHttpResponse(redirect("account_login"))

    def populate_user(self, request, sociallogin, data):
        """Set user.name from Google profile (custom User model has 'name', not first/last)."""
        user = super().populate_user(request, sociallogin, data)
        first_name = data.get("first_name", "")
        last_name = data.get("last_name", "")
        full_name = f"{first_name} {last_name}".strip()
        if full_name and not user.name:
            user.name = full_name
        return user

    def save_user(self, request, sociallogin, form=None):
        """Create OAuthConnection after saving a new social signup."""
        user = super().save_user(request, sociallogin, form)
        self._sync_oauth_connection(user, sociallogin)
        return user

    def pre_social_login(self, request, sociallogin):
        """Sync OAuthConnection for returning users and auto-connected accounts."""
        # Unverified local accounts matched by email are protected by allauth itself:
        # SOCIALACCOUNT_EMAIL_AUTHENTICATION wipes their password on the Google login (wipe_password).
        super().pre_social_login(request, sociallogin)
        if sociallogin.is_existing:
            self._sync_oauth_connection(sociallogin.user, sociallogin)

    def _sync_oauth_connection(self, user, sociallogin):
        account = sociallogin.account
        if account.provider != "google":
            return
        provider_email = ""
        for ea in sociallogin.email_addresses:
            provider_email = ea.email
            break
        OAuthConnection.objects.update_or_create(
            provider=OAuthConnection.Provider.GOOGLE,
            provider_user_id=account.uid,
            defaults={"user": user, "provider_email": provider_email},
        )
