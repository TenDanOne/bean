"""YouTube Data API v3 provider."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from urllib.parse import urlencode

from .base import SocialProvider
from .exceptions import OAuthError, PublishError
from .types import (
    AccountMetrics,
    AccountProfile,
    AuthType,
    CommentResult,
    InboxMessage,
    MediaType,
    OAuthTokens,
    PostMetrics,
    PostType,
    PublishContent,
    PublishResult,
    RateLimitConfig,
    ReplyResult,
)

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API_BASE = "https://www.googleapis.com/youtube/v3"
UPLOAD_BASE = "https://www.googleapis.com/upload/youtube/v3"
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024  # resumable chunks must be multiples of 256 KiB
ANALYTICS_BASE = "https://youtubeanalytics.googleapis.com/v2"

# YouTube Analytics caps a ``filters=video==<id>,<id>,...`` list at 500 IDs
# per request. Larger inputs to :meth:`YouTubeProvider.get_post_analytics`
# are split into multiple requests transparently.
_ANALYTICS_VIDEO_FILTER_CHUNK = 500

# ``commentThreads?order=time`` returns threads newest-first, so once a page
# falls entirely before ``since`` every later page is older still and there is
# nothing left to collect. We don't stop on the *first* such page, though: a
# reply can be newer than the top-level comment it hangs off, so a thread whose
# root is old may still carry fresh replies. Paginate this many consecutive
# all-stale pages before giving up.
_SINCE_GRACE_PAGES = 2


#: Categorías de video asignables (videoCategories, región MX). Catálogo estático: no se llama a la API en cada render.
YOUTUBE_CATEGORIES = [
    ("1", "Cine y animación"),
    ("2", "Autos y vehículos"),
    ("10", "Música"),
    ("15", "Mascotas y animales"),
    ("17", "Deportes"),
    ("19", "Viajes y eventos"),
    ("20", "Videojuegos"),
    ("22", "Gente y blogs"),
    ("23", "Comedia"),
    ("24", "Entretenimiento"),
    ("25", "Noticias y política"),
    ("26", "Consejos y estilo"),
    ("27", "Educación"),
    ("28", "Ciencia y tecnología"),
    ("29", "ONG y activismo"),
]
YOUTUBE_CATEGORY_IDS = {cid for cid, _ in YOUTUBE_CATEGORIES}
_PLAYLISTS_CACHE_SECONDS = 300
PLAYLIST_ERROR_PREFIX = "No se agregó a la lista de reproducción:"


def _chapter_seconds(raw: str) -> int:
    """``mm:ss`` or ``h:mm:ss`` -> seconds (ValueError when malformed)."""
    parts = [int(p) for p in str(raw).strip().split(":")]
    if not 2 <= len(parts) <= 3 or any(p < 0 for p in parts) or any(p > 59 for p in parts[1:]):
        raise ValueError(raw)
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return secs


def chapters_error(chapters) -> str | None:
    """Spanish reason a ``[{t, title}]`` list is not valid YouTube chapters, else ``None``."""
    if not chapters:
        return None
    if len(chapters) < 3:
        return "YouTube necesita al menos 3 capítulos."
    try:
        secs = [_chapter_seconds(c.get("t", "")) for c in chapters]
    except (ValueError, AttributeError):
        return "Los tiempos de los capítulos deben ser mm:ss (por ejemplo 01:30)."
    if secs[0] != 0:
        return "El primer capítulo debe empezar en 00:00."
    if any(not str(c.get("title", "")).strip() for c in chapters):
        return "Cada capítulo necesita un título."
    if any(b - a < 10 for a, b in zip(secs, secs[1:], strict=False)):
        return "Cada capítulo debe durar al menos 10 segundos y estar en orden."
    return None


def build_chapters_block(chapters) -> str:
    """Bloque de texto «00:00 Título» por línea para la descripción; ValueError si no es válido."""
    err = chapters_error(chapters)
    if err:
        raise ValueError(err)
    return "\n".join(f"{c['t'].strip()} {c['title'].strip()}" for c in chapters or [])


def _parse_yt_timestamp(value: str) -> datetime:
    """Parse a YouTube RFC-3339 timestamp (``…Z``) into an aware datetime."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class YouTubeProvider(SocialProvider):
    """YouTube Data API v3 provider using Google OAuth 2.0."""

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "YouTube"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.OAUTH2

    @property
    def max_caption_length(self) -> int:
        return 5000

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.VIDEO, PostType.SHORT]

    @property
    def supported_media_types(self) -> list[MediaType]:
        return [MediaType.MP4, MediaType.MOV]

    @property
    def required_scopes(self) -> list[str]:
        scopes = [
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly",
            "https://www.googleapis.com/auth/youtube.force-ssl",
        ]
        if self.include_analytics_scopes:
            scopes.extend(self.analytics_only_scopes)
        return scopes

    @property
    def analytics_only_scopes(self) -> list[str]:
        # Required for the YouTube Analytics API (watch time, retention,
        # demographics). Only requested when analytics is enabled so the
        # Google OAuth consent screen doesn't list it when not needed.
        return ["https://www.googleapis.com/auth/yt-analytics.readonly"]

    @property
    def rate_limits(self) -> RateLimitConfig:
        return RateLimitConfig(
            requests_per_hour=600,
            requests_per_day=10000,
            publish_per_day=6,
            extra={"quota_units_per_day": 10000, "upload_cost_units": 1600},
        )

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        params = {
            "client_id": self.credentials["client_id"],
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": " ".join(self.required_scopes),
            "response_type": "code",
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthTokens:
        resp = self._request(
            "POST",
            TOKEN_URL,
            data={
                "code": code,
                "client_id": self.credentials["client_id"],
                "client_secret": self.credentials["client_secret"],
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        body = resp.json()
        if "access_token" not in body:
            raise OAuthError(
                f"YouTube token exchange failed: {body}",
                platform=self.platform_name,
                raw_response=body,
            )
        return OAuthTokens(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_in=body.get("expires_in"),
            scope=body.get("scope"),
            raw_response=body,
        )

    def refresh_token(self, refresh_token: str) -> OAuthTokens:
        resp = self._request(
            "POST",
            TOKEN_URL,
            data={
                "refresh_token": refresh_token,
                "client_id": self.credentials["client_id"],
                "client_secret": self.credentials["client_secret"],
                "grant_type": "refresh_token",
            },
        )
        body = resp.json()
        if "access_token" not in body:
            raise OAuthError(
                f"YouTube token refresh failed: {body}",
                platform=self.platform_name,
                raw_response=body,
            )
        return OAuthTokens(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token", refresh_token),
            expires_in=body.get("expires_in"),
            scope=body.get("scope"),
            raw_response=body,
        )

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def get_profile(self, access_token: str) -> AccountProfile:
        resp = self._request(
            "GET",
            f"{API_BASE}/channels",
            access_token=access_token,
            params={"part": "snippet,statistics", "mine": "true"},
        )
        body = resp.json()
        items = body.get("items", [])
        if not items:
            return AccountProfile(platform_id="", name="Unknown")

        channel = items[0]
        snippet = channel.get("snippet", {})
        stats = channel.get("statistics", {})
        thumbnails = snippet.get("thumbnails", {})
        avatar = thumbnails.get("default", {}).get("url") or thumbnails.get("medium", {}).get("url")
        return AccountProfile(
            platform_id=channel["id"],
            name=snippet.get("title", ""),
            handle=snippet.get("customUrl"),
            avatar_url=avatar,
            follower_count=int(stats.get("subscriberCount", 0)),
            extra={
                "view_count": int(stats.get("viewCount", 0)),
                "video_count": int(stats.get("videoCount", 0)),
            },
        )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        if content.post_type not in (PostType.VIDEO, PostType.SHORT):
            raise PublishError(
                "YouTube only supports VIDEO and SHORT post types",
                platform=self.platform_name,
            )

        title = content.title or content.text or ""
        description = content.description or content.text or ""

        # Shorts: add #Shorts tag if not already present
        if content.post_type == PostType.SHORT and "#Shorts" not in title:
            title = f"{title} #Shorts".strip()

        privacy_status = content.extra.get("privacy_status", "public")
        made_for_kids = content.extra.get("made_for_kids", content.extra.get("self_declared_made_for_kids", False))
        category_id = content.extra.get("category_id") or "22"  # 22 = People & Blogs
        tags = content.extra.get("tags", [])

        chapters = content.extra.get("chapters")
        if chapters:
            try:
                block = build_chapters_block(chapters)
            except ValueError as exc:
                raise PublishError(str(exc), platform=self.platform_name, retryable=False) from exc
            description = f"{description}\n\n{block}" if description else block

        metadata = {
            "snippet": {
                "title": title[:100],
                "description": description[: self.max_caption_length],
                "tags": tags,
                "categoryId": category_id,
            },
            "status": {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": made_for_kids,
                "containsSyntheticMedia": bool(content.extra.get("contains_synthetic_media", False)),
            },
        }

        # Step 1: Initiate resumable upload
        init_resp = self._request(
            "POST",
            f"{UPLOAD_BASE}/videos",
            access_token=access_token,
            params={"uploadType": "resumable", "part": "snippet,status"},
            json=metadata,
        )
        upload_uri = init_resp.headers.get("Location")
        if not upload_uri:
            raise PublishError(
                "YouTube did not return a resumable upload URI",
                platform=self.platform_name,
            )

        # Step 2: Upload video binary
        if content.media_files:
            upload_body = self._upload_resumable(upload_uri, content.media_files[0])
            video_id = upload_body.get("id", "")

            # Step 3 (optional): upload custom thumbnail. The video is already live, so a
            # rejection is reported in result.extra["thumbnail_error"] instead of failing the publish.
            thumbnail_path = content.extra.get("thumbnail_file")
            if video_id and thumbnail_path:
                if content.post_type == PostType.SHORT:
                    upload_body["thumbnail_error"] = "YouTube no aplica miniaturas personalizadas a los Shorts."
                else:
                    try:
                        with open(thumbnail_path, "rb") as tf:
                            thumb_data = tf.read()
                        # Real content type from magic bytes, not from the file extension.
                        if thumb_data.startswith(b"\x89PNG\r\n\x1a\n"):
                            thumb_ct = "image/png"
                        elif thumb_data.startswith(b"\xff\xd8\xff"):
                            thumb_ct = "image/jpeg"
                        else:
                            raise ValueError("el archivo no es JPG ni PNG")
                        self._request(
                            "POST",
                            f"{UPLOAD_BASE}/thumbnails/set",
                            access_token=access_token,
                            params={"videoId": video_id, "uploadType": "media"},
                            headers={
                                "Content-Type": thumb_ct,
                                "Content-Length": str(len(thumb_data)),
                            },
                            data=thumb_data,
                            timeout=60.0,
                        )
                    except Exception as exc:
                        logger.exception("Custom thumbnail upload failed for video %s", video_id)
                        upload_body["thumbnail_error"] = str(exc)[:300] or exc.__class__.__name__

            # Step 4 (optional, best-effort): add to a playlist. Same rule as the thumbnail.
            playlist_id = content.extra.get("playlist_id")
            if video_id and playlist_id:
                try:
                    self._request(
                        "POST",
                        f"{API_BASE}/playlistItems",
                        access_token=access_token,
                        params={"part": "snippet"},
                        json={
                            "snippet": {
                                "playlistId": playlist_id,
                                "resourceId": {"kind": "youtube#video", "videoId": video_id},
                            }
                        },
                    )
                except Exception as exc:
                    logger.exception("Adding video %s to playlist %s failed", video_id, playlist_id)
                    reason = f"{PLAYLIST_ERROR_PREFIX} {str(exc)[:200] or exc.__class__.__name__}"
                    prev = upload_body.get("thumbnail_error")
                    upload_body["thumbnail_error"] = f"{prev} | {reason}" if prev else reason

            return PublishResult(
                platform_post_id=video_id,
                url=f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
                extra=upload_body,
            )

        raise PublishError(
            "No video file provided (media_files required)",
            platform=self.platform_name,
        )

    def list_playlists(self, access_token: str, cache_id: str = "") -> list[dict]:
        """Playlists del canal como ``[{id, title}]``; caché corta para no gastar cuota en cada render."""
        from django.core.cache import cache

        key = (
            f"yt_playlists:{cache_id}" if cache_id else None
        )  # por cuenta; sin id no hay caché (evita mezclar canales)
        cached = cache.get(key) if key else None
        if cached is not None:
            return cached
        playlists: list[dict] = []
        page_token = None
        for _ in range(4):  # shortcut: 200 listas máx., suficiente para un canal personal
            params = {"part": "snippet", "mine": "true", "maxResults": 50}
            if page_token:
                params["pageToken"] = page_token
            data = self._request("GET", f"{API_BASE}/playlists", access_token=access_token, params=params).json()
            playlists += [{"id": i["id"], "title": i["snippet"]["title"]} for i in data.get("items", [])]
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        if key:
            cache.set(key, playlists, _PLAYLISTS_CACHE_SECONDS)
        return playlists

    def _upload_resumable(self, upload_uri: str, video_path: str) -> dict:
        """PUT the file to a resumable session in chunks (308 = keep going, 200/201 = done)."""
        total = os.path.getsize(video_path)
        if total == 0:
            raise PublishError("El archivo de video está vacío.", platform=self.platform_name, retryable=False)
        with open(video_path, "rb") as f:
            start = 0
            while start < total:
                chunk = f.read(UPLOAD_CHUNK_SIZE)
                end = start + len(chunk)
                resp = self._request(
                    "PUT",
                    upload_uri,
                    headers={
                        "Content-Type": "video/*",
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {start}-{end - 1}/{total}",
                    },
                    data=chunk,
                    timeout=300.0,
                )
                if resp.status_code != 308:
                    return resp.json()
                start = end
        raise PublishError("YouTube no confirmó la subida del video.", platform=self.platform_name)

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------

    def publish_comment(self, access_token: str, post_id: str, text: str) -> CommentResult:
        resp = self._request(
            "POST",
            f"{API_BASE}/commentThreads",
            access_token=access_token,
            params={"part": "snippet"},
            json={
                "snippet": {
                    "videoId": post_id,
                    "topLevelComment": {
                        "snippet": {
                            "textOriginal": text,
                        }
                    },
                }
            },
        )
        body = resp.json()
        comment_id = body.get("id", "")
        return CommentResult(
            platform_comment_id=comment_id,
            extra=body,
        )

    # ------------------------------------------------------------------
    # Inbox
    # ------------------------------------------------------------------

    def _channel_id(self, access_token: str) -> str | None:
        """This account's channel id, without spending a quota unit if avoidable.

        ``_resolve_publish_credentials`` injects it from
        ``SocialAccount.account_platform_id``. Looking it up instead costs a unit
        on every poll, which at a 60-second cadence doubles the cost of a cycle
        that otherwise costs one.
        """
        channel_id = self.credentials.get("channel_id")
        if channel_id:
            return channel_id
        resp = self._request(
            "GET",
            f"{API_BASE}/channels",
            access_token=access_token,
            params={"part": "id", "mine": "true"},
        )
        items = resp.json().get("items", [])
        return items[0]["id"] if items else None

    def _threads_to_messages(self, threads, since=None):
        """Flatten comment threads into messages, and say whether any were fresh.

        Shared by the every-minute catch-up and the history backfill. They ask
        for different pages and stop for different reasons, but a comment is a
        comment — two copies of this parsing would drift the moment either side
        changed.

        Returns ``(messages, saw_anything_newer_than_since)``. That second value
        is what lets the catch-up stop paging into the past; the backfill
        ignores it, because going into the past is the whole job.
        """
        messages: list[InboxMessage] = []
        page_had_fresh = False

        for thread in threads:
            top_comment = thread["snippet"]["topLevelComment"]
            top_snippet = top_comment["snippet"]
            published = _parse_yt_timestamp(top_snippet["publishedAt"])
            top_comment_id = top_comment["id"]
            video_id = top_snippet["videoId"]

            # An out-of-window root still gets its replies scanned below — a
            # fresh reply on an old thread is a real inbox message.
            if since is None or published >= since:
                page_had_fresh = True
                messages.append(
                    InboxMessage(
                        platform_message_id=top_comment_id,
                        sender_id=top_snippet.get("authorChannelId", {}).get("value", ""),
                        sender_name=top_snippet.get("authorDisplayName", ""),
                        text=top_snippet.get("textDisplay", ""),
                        timestamp=published,
                        message_type="comment",
                        extra={
                            "video_id": video_id,
                            "comment_id": top_comment_id,
                            "sender_avatar_url": top_snippet.get("authorProfileImageUrl", ""),
                            # How many replies YouTube says exist, against how
                            # many it actually handed back — it inlines at most
                            # five. Recorded so the caller can fetch the rest
                            # rather than silently showing a 30-reply argument
                            # as five remarks.
                            "reply_count": thread["snippet"].get("totalReplyCount", 0),
                        },
                    )
                )

            for reply in thread.get("replies", {}).get("comments", []):
                r_snippet = reply["snippet"]
                r_published = _parse_yt_timestamp(r_snippet["publishedAt"])
                if since and r_published < since:
                    continue
                page_had_fresh = True
                messages.append(
                    InboxMessage(
                        platform_message_id=reply["id"],
                        sender_id=r_snippet.get("authorChannelId", {}).get("value", ""),
                        sender_name=r_snippet.get("authorDisplayName", ""),
                        text=r_snippet.get("textDisplay", ""),
                        timestamp=r_published,
                        message_type="comment",
                        extra={
                            "video_id": video_id,
                            "comment_id": reply["id"],
                            "parent_id": top_comment_id,
                            "sender_avatar_url": r_snippet.get("authorProfileImageUrl", ""),
                        },
                    )
                )

        return messages, page_had_fresh

    def get_messages(self, access_token: str, since: datetime | None = None) -> list[InboxMessage]:
        """Fetch comment threads across the channel, newest first.

        When ``since`` is given, pagination stops once
        :data:`_SINCE_GRACE_PAGES` consecutive pages contain nothing newer than
        the cutoff. Without that, a bounded window (``backfill_inbox --days 30``)
        would still walk the channel's entire comment history — each page is a
        quota-billed request, and a project's default allowance is only 10,000
        units a day.
        """
        channel_id = self._channel_id(access_token)
        if not channel_id:
            return []

        messages: list[InboxMessage] = []
        page_token: str | None = None
        stale_pages = 0

        while True:
            params: dict = {
                "part": "snippet,replies",
                "allThreadsRelatedToChannelId": channel_id,
                "maxResults": 100,
                "order": "time",
            }
            if page_token:
                params["pageToken"] = page_token

            resp = self._request(
                "GET",
                f"{API_BASE}/commentThreads",
                access_token=access_token,
                params=params,
            )
            body = resp.json()
            fresh, page_had_fresh = self._threads_to_messages(body.get("items", []), since=since)
            messages.extend(fresh)

            page_token = body.get("nextPageToken")
            if not page_token:
                break

            if since is not None:
                stale_pages = 0 if page_had_fresh else stale_pages + 1
                if stale_pages >= _SINCE_GRACE_PAGES:
                    logger.debug(
                        "YouTube inbox: stopping pagination after %d pages older than %s",
                        stale_pages,
                        since.isoformat(),
                    )
                    break

        return messages

    def reply_to_message(self, access_token: str, message_id: str, text: str, extra: dict | None = None) -> ReplyResult:
        resp = self._request(
            "POST",
            f"{API_BASE}/comments",
            access_token=access_token,
            params={"part": "snippet"},
            json={
                "snippet": {
                    "parentId": message_id,
                    "textOriginal": text,
                }
            },
        )
        body = resp.json()
        return ReplyResult(platform_message_id=body.get("id", ""), extra=body)

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    def get_messages_page(self, access_token, page_token=None, since=None):
        """One page of comment threads, plus the cursor for the next.

        ``commentThreads.list`` costs **1 quota unit per 100 comments**, so
        importing three months of a busy channel is around 100 units of a daily
        10,000 — the expensive call on this API is uploading a video (1,600),
        not reading comments.

        Unlike :meth:`get_messages` this does not stop early on stale pages.
        The backfill is walking deliberately into the past; "these are old" is
        the point, not a signal to stop.
        """
        channel_id = self._channel_id(access_token)
        params: dict = {
            "part": "snippet,replies",
            "allThreadsRelatedToChannelId": channel_id,
            "maxResults": 100,
            "order": "time",
        }
        if page_token:
            params["pageToken"] = page_token

        resp = self._request("GET", f"{API_BASE}/commentThreads", access_token=access_token, params=params)
        body = resp.json()
        messages, _ = self._threads_to_messages(body.get("items", []), since=since)
        return messages, body.get("nextPageToken")

    def delete_comment(self, access_token: str, comment_id: str) -> None:
        """Delete one of our own comments. 50 quota units."""
        self._request("DELETE", f"{API_BASE}/comments", access_token=access_token, params={"id": comment_id})

    def delete_post(self, access_token: str, platform_post_id: str) -> None:
        """videos.delete. 50 quota units; covered by the youtube.force-ssl scope."""
        self._request("DELETE", f"{API_BASE}/videos", access_token=access_token, params={"id": platform_post_id})

    def set_comment_visibility(self, access_token: str, comment_id: str, visible: bool) -> None:
        """Publish or hold someone else's comment on our video. 50 quota units.

        Hiding is `heldForReview`, deliberately not `rejected`. YouTube treats a
        rejected comment as soft-deleted: the API cannot bring it back to
        ``published`` (it answers 400), and it also buries every reply on the
        thread — one click and a whole conversation is effectively gone. That
        makes "rejected" the wrong tool for a reversible hiding feature.

        `heldForReview` is the documented way to keep a comment out of public
        view *and* able to come back: it is hidden from the audience, sits in
        the owner's moderation queue, and ``published`` restores it over the
        same endpoint. Note the statuses are one-way for `heldForReview` — once
        a comment has reached ``published`` or ``rejected`` YouTube will not
        move it back to `heldForReview` — so holding is the reversible state
        and rejecting the permanent one.
        """
        self._request(
            "POST",
            f"{API_BASE}/comments/setModerationStatus",
            access_token=access_token,
            params={
                "id": comment_id,
                "moderationStatus": "published" if visible else "heldForReview",
            },
        )

    def get_post_details(self, access_token: str, post_ids: list[str]) -> dict[str, dict]:
        """Video titles for the inbox's publication folders.

        ``videos.list`` costs **1 quota unit per call** and accepts up to 50 ids,
        so a channel's whole folder list is one unit — against a daily 10,000,
        and against the 1,600 a single upload costs. That is why this batches
        rather than asking per video.

        Ids that no longer resolve (deleted, or made private) are simply absent
        from the response and therefore from the returned dict. The caller shows
        the id instead, which is worse but true.
        """
        details: dict[str, dict] = {}
        unique = [pid for pid in dict.fromkeys(post_ids) if pid]
        for start in range(0, len(unique), 50):
            batch = unique[start : start + 50]
            resp = self._request(
                "GET",
                f"{API_BASE}/videos",
                access_token=access_token,
                params={"part": "snippet", "id": ",".join(batch)},
            )
            for item in resp.json().get("items", []):
                snippet = item.get("snippet", {}) or {}
                title = (snippet.get("title") or "").strip()
                if not title:
                    continue
                # The publish date rides along free: `part=snippet` already
                # returned it, so carrying it costs no extra request and saves a
                # second call for something shown beside every folder.
                details[item["id"]] = {
                    "title": title,
                    "published_at": (snippet.get("publishedAt") or "").strip() or None,
                }
        return details

    def get_post_metrics(self, access_token: str, post_id: str) -> PostMetrics:
        """Per-video counts from the YouTube Data API ``videos.list?part=statistics``.

        Only the counts the Data API exposes: views, likes, comments. Watch
        time, average view percentage, and shares are intentionally absent
        — those live on the Analytics API and are batched per channel by
        :meth:`get_post_analytics`.
        """
        resp = self._request(
            "GET",
            f"{API_BASE}/videos",
            access_token=access_token,
            params={"part": "statistics", "id": post_id},
        )
        body = resp.json()
        items = body.get("items", [])
        if not items:
            return PostMetrics()

        stats = items[0].get("statistics", {})
        views = int(stats.get("viewCount", 0))
        likes = int(stats.get("likeCount", 0))
        comments = int(stats.get("commentCount", 0))
        return PostMetrics(
            video_views=views,
            likes=likes,
            comments=comments,
            engagements=likes + comments,
            extra={
                "favorite_count": int(stats.get("favoriteCount", 0)),
            },
        )

    def get_account_metrics(self, access_token: str, date_range: tuple[datetime, datetime]) -> AccountMetrics:
        """Channel-level metrics from the YouTube Analytics API.

        Fetches the metrics the YouTube Data API can't provide per-video:
        watch time, average view %, subscribers gained, and shares.
        Views/likes/comments come from per-post snapshots so the main page
        stays consistent with the per-post drawer; shares is the exception
        because ``videos.list?part=statistics`` exposes no shareCount field.

        Per-video equivalents of the channel-level watch-time / avg-view-% /
        shares triple come from :meth:`get_post_analytics`, which calls the
        same ``/reports`` endpoint with ``dimensions=video`` and the video
        IDs as a filter.

        Callers must pass a single-day ``date_range`` — Analytics returns one
        aggregated row across [startDate, endDate], and the sync layer writes
        it as a single day's snapshot. Larger ranges would silently collapse
        into one cell.

        Requires the ``yt-analytics.readonly`` scope. The YouTube Analytics
        API typically lags 1–2 days behind real-time.
        """
        start_date = date_range[0].date().isoformat()
        end_date = date_range[1].date().isoformat()
        resp = self._request(
            "GET",
            f"{ANALYTICS_BASE}/reports",
            access_token=access_token,
            params={
                "ids": "channel==MINE",
                "startDate": start_date,
                "endDate": end_date,
                "metrics": "estimatedMinutesWatched,averageViewPercentage,subscribersGained,shares",
            },
        )
        body = resp.json()
        headers = body.get("columnHeaders", [])
        rows = body.get("rows", [])
        if not rows or not rows[0]:
            return AccountMetrics()

        index = {col.get("name", ""): i for i, col in enumerate(headers)}
        row = rows[0]

        def _value_or_none(name: str) -> float | None:
            """Return float value if Analytics returned the column (even 0), else None.

            Distinguishes "metric not in the response" (None) from "metric
            returned 0" (0.0). Falsy-zero guards downstream incorrectly drop
            real 0-value days otherwise.
            """
            i = index.get(name)
            if i is None or i >= len(row) or row[i] is None:
                return None
            try:
                return float(row[i])
            except (TypeError, ValueError):
                return None

        extra: dict = {}
        for api_key, catalog_key in (
            ("estimatedMinutesWatched", "watch_time"),
            ("averageViewPercentage", "avg_view_pct"),
            ("subscribersGained", "subscribers"),
            ("shares", "shares"),
        ):
            v = _value_or_none(api_key)
            if v is not None:
                extra[catalog_key] = v
        return AccountMetrics(extra=extra)

    def get_post_analytics(
        self,
        access_token: str,
        post_ids: list[str],
        date_range: tuple[datetime, datetime],
    ) -> dict[str, PostMetrics]:
        """Per-video metrics from the YouTube Analytics API, batched.

        Returns ``{video_id: PostMetrics}`` populated with ``extra``:
        ``watch_time`` (estimatedMinutesWatched), ``avg_view_pct``
        (averageViewPercentage), and ``shares``. These are the per-video
        metrics the Data API v3 ``statistics`` part can't provide — the
        catalog keys match the ones :func:`apps.analytics.tasks._post_metrics_to_dict`
        reads from ``PostMetrics.extra`` via ``_GENERIC_POST_EXTRA_KEYS``.

        One ``/reports`` request covers every video in ``post_ids`` via
        ``dimensions=video`` and ``filters=video==<id>,<id>,...``. The
        endpoint caps the filter at 500 IDs per request, so longer inputs
        are split into multiple calls and merged.

        Videos that have no activity in the window are omitted from the
        Analytics response and will be absent from the returned dict —
        callers should treat that as "no data" rather than "all zeros".

        Requires the ``yt-analytics.readonly`` scope (same as
        :meth:`get_account_metrics`). The Analytics API typically lags
        1–2 days behind real-time.
        """
        if not post_ids:
            return {}

        start_date = date_range[0].date().isoformat()
        end_date = date_range[1].date().isoformat()
        result: dict[str, PostMetrics] = {}

        for offset in range(0, len(post_ids), _ANALYTICS_VIDEO_FILTER_CHUNK):
            chunk = post_ids[offset : offset + _ANALYTICS_VIDEO_FILTER_CHUNK]
            resp = self._request(
                "GET",
                f"{ANALYTICS_BASE}/reports",
                access_token=access_token,
                params={
                    "ids": "channel==MINE",
                    "startDate": start_date,
                    "endDate": end_date,
                    "metrics": "estimatedMinutesWatched,averageViewPercentage,shares",
                    "dimensions": "video",
                    "filters": f"video=={','.join(chunk)}",
                },
            )
            body = resp.json()
            headers = body.get("columnHeaders", []) or []
            rows = body.get("rows", []) or []
            index = {col.get("name", ""): i for i, col in enumerate(headers)}
            video_idx = index.get("video")
            if video_idx is None:
                continue

            # API column → indexes used below. Pre-computed once per chunk so
            # the inner row loop is a flat lookup. Same semantics as the
            # closure in ``get_account_metrics``: missing column → key
            # omitted; present column with a 0 → 0 is preserved (not dropped
            # as falsy).
            watch_idx = index.get("estimatedMinutesWatched")
            avp_idx = index.get("averageViewPercentage")
            shares_idx = index.get("shares")

            for row in rows:
                if video_idx >= len(row):
                    continue
                video_id = row[video_idx]
                if not video_id:
                    continue

                extra: dict = {}
                for catalog_key, i in (("watch_time", watch_idx), ("avg_view_pct", avp_idx)):
                    if i is None or i >= len(row) or row[i] is None:
                        continue
                    try:
                        extra[catalog_key] = float(row[i])
                    except (TypeError, ValueError):
                        continue

                # ``shares`` has a dedicated ``PostMetrics`` field; the catalog
                # mapper (``_post_metrics_to_dict``) reads it from there, not
                # from ``extra``. Cast to int to match the field type.
                shares_value: int | None = None
                if shares_idx is not None and shares_idx < len(row) and row[shares_idx] is not None:
                    try:
                        shares_value = int(float(row[shares_idx]))
                    except (TypeError, ValueError):
                        shares_value = None

                if extra or shares_value is not None:
                    result[video_id] = PostMetrics(shares=shares_value or 0, extra=extra)
        return result

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def revoke_token(self, access_token: str) -> bool:
        try:
            self._request(
                "POST",
                REVOKE_URL,
                params={"token": access_token},
            )
            return True
        except Exception:
            logger.exception("Failed to revoke YouTube token")
            return False
