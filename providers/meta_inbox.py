"""Pieces of the inbox shared by the Facebook and Instagram providers (same Graph API)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from .exceptions import APIError
from .types import InboxMessage

logger = logging.getLogger(__name__)

# Messaging window: free reply for 24 h after the user's last message, then the
# HUMAN_AGENT tag extends it to 7 days. Past that Meta refuses the send.
RESPONSE_WINDOW = timedelta(hours=24)
HUMAN_AGENT_WINDOW = timedelta(days=7)


def parse_ts(value: str) -> datetime:
    """Graph timestamps look like ``2026-10-19T12:00:00+0000``."""
    return datetime.fromisoformat(value.replace("+0000", "+00:00"))


def fetch_sender_profile(provider, base_url: str, access_token: str, user_id: str, fields: str, cache: dict) -> dict:
    """Name and photo of a PSID/IGSID. Decoration: a failure costs the photo, never the message."""
    if not user_id:
        return {}
    if user_id not in cache:
        try:
            cache[user_id] = provider._request(
                "GET", f"{base_url}/{user_id}", access_token=access_token, params={"fields": fields}
            ).json()
        except Exception:
            logger.debug("Could not fetch profile for %s", user_id, exc_info=True)
            cache[user_id] = {}
    return cache[user_id]


def send_dm(provider, base_url: str, access_token: str, recipient_id: str, text: str, last_inbound_at: str | None):
    """POST /me/messages (``me`` is the Page the token belongs to, for FB and IG alike)."""
    if not recipient_id:
        raise APIError("Falta el id del participante para responder el mensaje.", platform=provider.platform_name)
    payload: dict = {"recipient": {"id": recipient_id}, "message": {"text": text}, "messaging_type": "RESPONSE"}
    if last_inbound_at:
        last = datetime.fromisoformat(last_inbound_at)
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        age = datetime.now(UTC) - last
        if age >= HUMAN_AGENT_WINDOW:
            raise APIError(
                "Pasaron más de 7 días desde el último mensaje del usuario; Meta ya no permite responder.",
                platform=provider.platform_name,
                retryable=False,
            )
        if age >= RESPONSE_WINDOW:
            payload.update(messaging_type="MESSAGE_TAG", tag="HUMAN_AGENT")
    return provider._request("POST", f"{base_url}/me/messages", access_token=access_token, json=payload)


def parse_dm(
    provider, base_url: str, access_token: str, msg: dict, convo_id: str, own_id: str, fields: str, cache: dict
) -> InboxMessage:
    """One message of a conversation. ``recipient_id`` is always the *other* party, so a reply goes to them."""
    sender = msg.get("from") or {}
    sender_id = str(sender.get("id", ""))
    to_list = (msg.get("to") or {}).get("data") or [{}]
    other_id = str(to_list[0].get("id", "")) if own_id and sender_id == own_id else sender_id
    profile = fetch_sender_profile(provider, base_url, access_token, other_id, fields, cache)
    return InboxMessage(
        platform_message_id=msg["id"],
        sender_id=sender_id,
        sender_name=sender.get("name") or sender.get("username") or profile.get("name") or sender_id,
        text=msg.get("message", ""),
        timestamp=parse_ts(msg["created_time"]),
        message_type="dm",
        extra={
            "conversation_id": convo_id,
            "recipient_id": other_id,
            "sender_handle": sender_id,
            "sender_avatar_url": profile.get("profile_pic", "") if sender_id == other_id else "",
        },
    )
