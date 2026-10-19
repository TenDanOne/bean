"""Inbound webhook receivers for Facebook/Instagram and YouTube."""

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime

from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django_ratelimit.decorators import ratelimit

from apps.common.validators import safe_xml_fromstring
from apps.credentials.models import resolve_app_secret, resolve_app_secrets
from apps.social_accounts.models import SocialAccount

from .models import InboxMessage

logger = logging.getLogger(__name__)


# --- Meta (Facebook + Instagram) shared helpers ---


def _meta_verify(request, configured_token: str):
    """Handle Meta webhook verification handshake.

    Meta hits this endpoint with hub.mode=subscribe + hub.verify_token + hub.challenge.
    We compare the supplied token to the one configured for this endpoint and echo
    the challenge on match.
    """
    mode = request.GET.get("hub.mode")
    token = request.GET.get("hub.verify_token")
    challenge = request.GET.get("hub.challenge", "")

    if not configured_token:
        logger.error("Webhook verify token not configured. Rejecting verification.")
        return HttpResponseForbidden("Webhook verify token not configured.")

    if mode == "subscribe" and token == configured_token:
        return HttpResponse(challenge, content_type="text/plain")
    return HttpResponseForbidden("Verification failed.")


def _verify_meta_signature(body: bytes, signature_header: str, app_secret: str) -> bool:
    """Verify HMAC-SHA256 signature (X-Hub-Signature-256) from Meta."""
    if not app_secret:
        logger.error("Meta app_secret not configured. Cannot verify webhook.")
        return False

    expected = (
        "sha256="
        + hmac.new(
            app_secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
    )
    return hmac.compare_digest(expected, signature_header)


def _meta_receive(request, platforms: list[str]):
    """Validate the HMAC signature and dispatch incoming Meta webhook events.

    A delivery is signed by exactly one Meta app. We accept the request only if
    the signature matches a configured app secret, then process events *only* for
    accounts whose owning org's secret actually signed the body (see
    ``_process_meta_events``) — so one org's secret can never authorize writes
    into another org's accounts.
    """
    signature = request.headers.get("X-Hub-Signature-256", "")
    candidate_secrets = resolve_app_secrets(*platforms)
    valid_secrets = {secret for secret in candidate_secrets if _verify_meta_signature(request.body, signature, secret)}
    if not valid_secrets:
        logger.warning("Meta webhook: signature did not match any configured app secret.")
        return HttpResponseForbidden("Invalid signature.")

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in Meta webhook payload.")
        return HttpResponse("Bad request", status=400)

    _process_meta_events(payload, platforms, valid_secrets)
    return HttpResponse("OK", status=200)


def _process_meta_events(payload: dict, platforms: list[str], valid_secrets: set[str]):
    """Process Meta (Facebook/Instagram) webhook events into InboxMessages.

    An event is processed only for SocialAccounts whose platform is in
    `platforms` AND whose owning org's app secret signed this delivery (i.e. is in
    `valid_secrets`). This binds each event to the org that owns the signing app,
    preventing one org's secret from forging events into another org's accounts.
    """
    for entry in payload.get("entry", []):
        page_id = entry.get("id")
        if not page_id:
            continue

        accounts = SocialAccount.objects.filter(
            account_platform_id=page_id,
            platform__in=platforms,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        ).select_related("workspace__organization")

        for account in accounts:
            account_secret = resolve_app_secret(account.platform, account.workspace.organization_id)
            if not account_secret or account_secret not in valid_secrets:
                logger.warning(
                    "Meta webhook: signature not valid for account %s's org app; skipping event.",
                    account.id,
                )
                continue

            for change in entry.get("changes", []):
                _handle_facebook_change(account, change, entry.get("time"))

            for messaging in entry.get("messaging", []):
                _handle_facebook_messaging(account, messaging)


# --- Webhook entry points ---


@csrf_exempt
@ratelimit(key="ip", rate="60/m", block=True)
@require_http_methods(["GET", "POST"])
def facebook_webhook(request):
    """Facebook & Instagram (Facebook Login) webhook endpoint.

    GET:  Verification handshake (hub.mode, hub.verify_token, hub.challenge).
    POST: Incoming events with HMAC-SHA256 signature in X-Hub-Signature-256.
    """
    if request.method == "GET":
        return _meta_verify(request, settings.FACEBOOK_WEBHOOK_VERIFY_TOKEN)
    # Facebook + Instagram (Facebook Login) share one Meta app.
    return _meta_receive(request, platforms=["facebook", "instagram"])


@csrf_exempt
@ratelimit(key="ip", rate="60/m", block=True)
@require_http_methods(["GET", "POST"])
def instagram_login_webhook(request):
    """Instagram (Direct, via Instagram Login) webhook endpoint.

    Separate from `facebook_webhook` because it uses its own verify token and
    its own app secret (the Instagram App is distinct from the parent Meta App
    when authenticating via Instagram Login).
    """
    if request.method == "GET":
        return _meta_verify(request, settings.INSTAGRAM_LOGIN_WEBHOOK_VERIFY_TOKEN)
    return _meta_receive(request, platforms=["instagram_login"])


# --- Event handlers (shared between facebook + instagram_login) ---


def _unix(value, millis=False):
    """A webhook's unix time as an aware datetime, or None when absent or garbled."""
    try:
        return datetime.fromtimestamp(float(value) / (1000 if millis else 1), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _handle_facebook_change(account, change: dict, entry_time=None):
    """Handle a single change event (Facebook feed comments, Instagram comments and mentions)."""
    field = change.get("field", "")
    value = change.get("value", {})

    if field == "feed":
        # The feed also announces reactions, shares, edits and removals; only a
        # newly added comment is something to answer.
        if value.get("item") == "comment" and value.get("verb") == "add":
            _upsert_facebook_comment(account, value)
    elif field == "comments":
        _upsert_instagram_comment(account, value, entry_time)
    elif field == "mentions":
        _upsert_instagram_mention(account, value, entry_time)
    elif field == "mention":
        _upsert_facebook_mention(account, value)


def _upsert_facebook_comment(account, value: dict):
    """Upsert a Facebook comment from a webhook event."""
    comment_id = value.get("comment_id") or value.get("id")
    if not comment_id:
        return

    from_data = value.get("from", {})
    graph_post_id = str(value.get("post_id", ""))
    extra = {**value, "graph_post_id": graph_post_id, "sender_handle": str(from_data.get("id", ""))}
    if graph_post_id:
        # Stored the way the publisher keeps it, so the comment finds its PlatformPost.
        extra["post_id"] = graph_post_id.rsplit("_", 1)[-1]
    # On a top-level comment Meta reports the post itself as the parent.
    parent_id = str(value.get("parent_id", ""))
    if parent_id and parent_id != graph_post_id:
        extra["parent_id"] = parent_id
    else:
        extra.pop("parent_id", None)

    _create_if_new(
        account=account,
        platform_message_id=str(comment_id),
        message_type=InboxMessage.MessageType.COMMENT,
        sender_name=from_data.get("name", "Unknown"),
        sender_id=from_data.get("id", ""),
        body=value.get("message", ""),
        extra=extra,
        timestamp=_unix(value.get("created_time")),
    )


def _upsert_instagram_comment(account, value: dict, entry_time=None):
    """An Instagram comment (``comments`` field): id, text, from{id,username}, media{id}, parent_id."""
    comment_id = value.get("id")
    if not comment_id:
        return
    from_data = value.get("from") or {}
    handle = str(from_data.get("id") or from_data.get("username", ""))
    extra = {**value, "sender_handle": handle}
    if (value.get("media") or {}).get("id"):
        extra["media_id"] = value["media"]["id"]
    if value.get("parent_id"):
        extra["parent_id"] = value["parent_id"]
    _create_if_new(
        account=account,
        platform_message_id=str(comment_id),
        message_type=InboxMessage.MessageType.COMMENT,
        sender_name=from_data.get("username") or "Usuario de Instagram",
        sender_id=handle,
        body=value.get("text", ""),
        extra=extra,
        timestamp=_unix(entry_time),
    )


def _upsert_instagram_mention(account, value: dict, entry_time=None):
    """An Instagram mention (``mentions`` field). Meta sends only ids, so the body stays empty."""
    mention_id = value.get("comment_id") or value.get("media_id")
    if not mention_id:
        return
    _create_if_new(
        account=account,
        platform_message_id=str(mention_id),
        message_type=InboxMessage.MessageType.MENTION,
        sender_name="Instagram",
        sender_id="",
        body="",
        extra={**value, "media_id": value.get("media_id", "")},
        timestamp=_unix(entry_time),
    )


def _upsert_facebook_mention(account, value: dict):
    """Upsert a Facebook mention from a webhook event."""
    mention_id = value.get("post_id") or value.get("id")
    if not mention_id:
        return

    from_data = value.get("from", {})
    text = value.get("message", "")

    _create_if_new(
        account=account,
        platform_message_id=str(mention_id),
        message_type=InboxMessage.MessageType.MENTION,
        sender_name=from_data.get("name", "Unknown"),
        sender_id=from_data.get("id", ""),
        body=text,
        extra=value,
        timestamp=_unix(value.get("created_time")),
    )


def _handle_facebook_messaging(account, messaging: dict):
    """Handle a Facebook/Instagram messaging event (DM, including echoes of what the Page sent)."""
    message_data = messaging.get("message", {})
    mid = message_data.get("mid")
    if not mid:
        return

    sender = messaging.get("sender", {})
    sender_id = str(sender.get("id", ""))
    # The conversation partner: the sender, unless this is the Page's own message.
    if sender_id == account.account_platform_id:
        partner_id = str((messaging.get("recipient") or {}).get("id", ""))
    else:
        partner_id = sender_id
    conversation_id = ""
    if partner_id:  # without a partner there is nobody to group under
        conversation_id = (
            InboxMessage.objects.filter(social_account=account, extra__recipient_id=partner_id)
            .exclude(conversation_id="")
            .values_list("conversation_id", flat=True)
            .first()
            or f"user:{partner_id}"
        )

    _create_if_new(
        account=account,
        platform_message_id=str(mid),
        message_type=InboxMessage.MessageType.DM,
        sender_name=sender.get("name", sender.get("id", "Unknown")),
        sender_id=sender_id,
        body=message_data.get("text", ""),
        extra={**messaging, "conversation_id": conversation_id, "recipient_id": partner_id},
        timestamp=_unix(messaging.get("timestamp"), millis=True),
    )


def _create_if_new(
    account,
    platform_message_id: str,
    message_type: str,
    sender_name: str,
    sender_id: str,
    body: str,
    extra: dict,
    timestamp=None,
):
    """Create the InboxMessage unless it already exists (deduplication).

    Goes through the same path as the poller (``_upsert_message``), so a webhook
    row gets the same outbound detection, threading, related post and
    ``awaiting_reply`` as a polled one. ``timestamp`` is the platform's own time.
    """
    from django.utils import timezone

    from providers.types import InboxMessage as ProviderMessage

    from .tasks import InboxSyncEngine

    if InboxMessage.objects.filter(social_account=account, platform_message_id=platform_message_id).exists():
        return
    InboxSyncEngine()._upsert_message(
        account,
        ProviderMessage(
            platform_message_id=platform_message_id,
            sender_id=sender_id,
            sender_name=sender_name,
            text=body,
            timestamp=timestamp or timezone.now(),
            message_type=message_type,
            extra=extra,
        ),
    )


# --- YouTube PubSubHubbub ---


def _pubsub_verify(request, configured_token: str):
    """Handle a PubSubHubbub subscribe/unsubscribe verification handshake.

    The hub hits this endpoint with hub.mode + hub.challenge, and replays the
    hub.verify_token supplied when the subscription was created. Echo the
    challenge only when that token matches ours, so a third party can't point
    our endpoint at a topic we never asked for.
    """
    mode = request.GET.get("hub.mode")
    token = request.GET.get("hub.verify_token", "")
    challenge = request.GET.get("hub.challenge", "")

    if not configured_token:
        logger.error("YOUTUBE_WEBHOOK_VERIFY_TOKEN is not configured. Rejecting verification.")
        return HttpResponseForbidden("Webhook verify token not configured.")

    if mode in ("subscribe", "unsubscribe") and hmac.compare_digest(token, configured_token):
        return HttpResponse(challenge, content_type="text/plain")

    logger.warning("YouTube webhook verification failed (mode=%s).", mode)
    return HttpResponseForbidden("Verification failed.")


@csrf_exempt
@ratelimit(key="ip", rate="60/m", block=True)
@require_http_methods(["GET", "POST"])
def youtube_webhook(request):
    """YouTube PubSubHubbub webhook endpoint.

    GET:  Subscription verification (verify token check, then echo hub.challenge).
    POST: Atom XML notification for new/updated videos on the channel.

    Note this feed carries video activity, not comments — comment sync runs
    through the polling path in ``apps.inbox.tasks``.
    """
    if request.method == "GET":
        # Echoing hub.challenge unconditionally would let anyone confirm a
        # subscription against this endpoint for a topic of their choosing.
        # Same handshake contract as the Meta receivers above.
        return _pubsub_verify(request, settings.YOUTUBE_WEBHOOK_VERIFY_TOKEN)

    # Verify HMAC - reject if secret is not configured
    webhook_secret = settings.YOUTUBE_WEBHOOK_SECRET
    if not webhook_secret:
        logger.error("YOUTUBE_WEBHOOK_SECRET is not configured. Rejecting webhook POST.")
        return HttpResponseForbidden("Webhook secret not configured.")

    signature = request.headers.get("X-Hub-Signature", "")
    expected = (
        "sha1="
        + hmac.new(
            webhook_secret.encode(),
            request.body,
            hashlib.sha1,
        ).hexdigest()
    )
    if not hmac.compare_digest(expected, signature):
        logger.warning("Invalid YouTube webhook signature.")
        return HttpResponseForbidden("Invalid signature.")

    try:
        _process_youtube_notification(request.body)
    except Exception:
        logger.exception("Error processing YouTube webhook.")

    return HttpResponse("OK", status=200)


def _process_youtube_notification(body: bytes):
    """Parse Atom XML notification from YouTube and upsert messages."""
    root = safe_xml_fromstring(body)
    if root is None:
        logger.warning("Invalid or unsafe XML in YouTube webhook payload.")
        return

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
    }

    for entry in root.findall("atom:entry", ns):
        video_id = entry.findtext("yt:videoId", default="", namespaces=ns)
        channel_id = entry.findtext("yt:channelId", default="", namespaces=ns)
        title = entry.findtext("atom:title", default="", namespaces=ns)
        entry_id = entry.findtext("atom:id", default="", namespaces=ns)

        if not channel_id or not entry_id:
            continue

        accounts = SocialAccount.objects.filter(
            account_platform_id=channel_id,
            platform="youtube",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        ).select_related("workspace__organization")

        for account in accounts:
            _create_if_new(
                account=account,
                platform_message_id=entry_id,
                # A published video is not a comment. Filing it as one meant the
                # inbox offered Hide and Delete on it, and hiding would have
                # posted an Atom entry id (`yt:video:…`) to setModerationStatus.
                # MENTION is the closest existing type: something the channel is
                # named in, that nobody wrote to us.
                message_type=InboxMessage.MessageType.MENTION,
                sender_name="YouTube",
                sender_id=channel_id,
                body=title,
                extra={"video_id": video_id, "entry_id": entry_id},
            )
