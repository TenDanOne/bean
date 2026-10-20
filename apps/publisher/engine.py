"""Publishing Engine - background worker logic (F-2.4).

This module implements the core publish loop:
1. Poll for PlatformPosts where scheduled_at <= now() and status = 'scheduled'.
2. Transition each due PlatformPost to 'publishing'.
3. Dispatch platform posts in parallel.
4. Handle retries with exponential backoff.
5. Post first comment after 2-minute delay.
6. Update per-platform status and log results.

Status is owned entirely by ``PlatformPost`` — the parent ``Post`` exposes an
aggregate ``status`` property derived from its children (see
``apps.composer.status``).
"""

import contextlib
import logging
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta

from background_task import background
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.composer.models import PlatformPost
from apps.composer.services import media_error_for_platform
from apps.credentials.models import resolve_platform_credentials
from apps.social_accounts.models import SocialAccount
from providers import get_provider
from providers.exceptions import PublishError
from providers.types import PostType, PublishContent

from .models import PublishLog, RateLimitState

logger = logging.getLogger(__name__)

# Retry backoff schedule (in seconds)
RETRY_BACKOFF = [60, 300, 1800]  # 1min, 5min, 30min

# A PUBLISHING row with no platform id this long is a crashed/timed-out attempt, not a live one.
STUCK_PUBLISHING_TIMEOUT = timedelta(minutes=15)

# A pending publish (TikTok processing) that the platform never confirms fails after this long.
PENDING_PUBLISH_TIMEOUT = timedelta(hours=24)


def _resolve_publish_credentials(account):
    """Resolve the credentials dict for publishing on behalf of `account`.

    Combines org-level `PlatformCredential` (with `.env` dominant) with
    per-account federation metadata (Mastodon `instance_url` +
    `MastodonAppRegistration`, Bluesky `pds_url`). Returns a plain dict
    suitable for `get_provider(platform, credentials)`.
    """
    platform = account.platform

    # .env is dominant; admin-entered org credentials are the fallback.
    credentials = resolve_platform_credentials(platform, account.workspace.organization_id)

    if platform == "mastodon" and account.instance_url:
        from apps.common.validators import is_safe_url

        if is_safe_url(account.instance_url):
            credentials["instance_url"] = account.instance_url
            if not credentials.get("client_id"):
                from apps.social_accounts.models import MastodonAppRegistration

                try:
                    reg = MastodonAppRegistration.objects.get(instance_url=account.instance_url)
                    credentials["client_id"] = reg.client_id
                    credentials["client_secret"] = reg.client_secret
                except MastodonAppRegistration.DoesNotExist:
                    pass
        else:
            logger.warning(
                "Mastodon instance URL failed SSRF check for account %s",
                account.id,
            )
    elif platform == "bluesky" and account.instance_url:
        from apps.common.validators import is_safe_url

        if is_safe_url(account.instance_url):
            credentials["pds_url"] = account.instance_url
        else:
            logger.warning(
                "Bluesky PDS URL failed SSRF check for account %s",
                account.id,
            )
    elif platform == "facebook":
        credentials["page_id"] = account.account_platform_id
    elif platform == "instagram":
        credentials["ig_user_id"] = account.account_platform_id
    elif platform == "youtube":
        # Saves the provider a `channels.list` round-trip per inbox poll — a
        # quota unit spent re-deriving an ID we already stored at connect time.
        credentials["channel_id"] = account.account_platform_id

    return credentials


MAX_RETRIES = 3
FIRST_COMMENT_DELAY = getattr(settings, "PUBLISHER_FIRST_COMMENT_DELAY", 120)
MAX_CONCURRENT_PUBLISHES = getattr(settings, "PUBLISHER_MAX_CONCURRENT_PUBLISHES", 10)
MAX_CONCURRENT_POSTS = getattr(settings, "PUBLISHER_MAX_CONCURRENT_POSTS", 4)


class PublishEngine:
    """Orchestrates the publishing of scheduled posts."""

    def poll_and_publish(self):
        """Main poll loop - find and publish due platform posts.

        Called every ~15 seconds by the background worker. Groups due
        PlatformPosts by parent Post and publishes each group.
        """
        due_pps = self._get_due_platform_posts()

        # Group by parent post_id
        groups: dict = {}
        for pp in due_pps:
            groups.setdefault(pp.post_id, []).append(pp)

        published_count = 0
        with ThreadPoolExecutor(max_workers=min(len(groups), MAX_CONCURRENT_POSTS) or 1) as executor:
            futures = {
                executor.submit(self._publish_post_group, pps[0].post, pps): post_id for post_id, pps in groups.items()
            }
            for future in as_completed(futures):
                post_id = futures[future]
                try:
                    future.result()
                    published_count += 1
                except Exception:
                    logger.exception("Unexpected error publishing post group %s", post_id)

        # Always process retries, even when no new posts are due
        self._recover_stuck_publishing()
        self._process_retries()
        self._check_pending_publishes()

        return published_count

    def _get_due_platform_posts(self):
        """Find PlatformPosts due for publishing, using Coalesce fallback."""
        now = timezone.now()
        return list(
            PlatformPost.objects.filter(
                status=PlatformPost.Status.SCHEDULED,
            )
            .annotate(effective_at=Coalesce("scheduled_at", "post__scheduled_at"))
            .filter(effective_at__lte=now)
            # A failed attempt waits out its backoff before the next one.
            .filter(Q(next_retry_at__isnull=True) | Q(next_retry_at__lte=now))
            # Never publish a post that has any platform on hold — a client hold
            # parks the whole post out of the publish path even if a sibling
            # platform is already scheduled.
            .exclude(post__platform_posts__status=PlatformPost.Status.ON_HOLD)
            # An archived channel has been retired on purpose and its credentials
            # cleared. Every other surface filters on connection_status, but this
            # one selects by post status, so without this an archived channel
            # would still fire its already-scheduled posts.
            .exclude(social_account__connection_status=SocialAccount.ConnectionStatus.ARCHIVED)
            .select_related("post__workspace", "social_account")
            .order_by("effective_at")[:MAX_CONCURRENT_PUBLISHES]
        )

    def _publish_post_group(self, post, due_pps):
        """Publish a group of due PlatformPosts belonging to the same Post.

        Grouping is purely an operational optimization (shared media download,
        shared credential resolution). Status lives on the children — the
        parent Post is not touched.
        """
        # Lock and transition each due child from SCHEDULED → PUBLISHING.
        with transaction.atomic():
            # Lock ALL of this post's platform rows (not just the due scheduled
            # ones) so a concurrent client hold serializes against publishing:
            # request_hold()'s UPDATE on an approved sibling blocks on the locked
            # row until we commit, and we re-check on_hold here under the lock.
            locked = list(
                PlatformPost.objects.select_for_update()
                .filter(post_id=post.id)
                .select_related("social_account", "post__workspace")
            )

            if any(pp.status == PlatformPost.Status.ON_HOLD for pp in locked):
                return

            due_ids = {pp.id for pp in due_pps}
            platform_posts = [pp for pp in locked if pp.id in due_ids and pp.status == PlatformPost.Status.SCHEDULED]

            if not platform_posts:
                return

            # updated_at marks when the attempt began (stuck-publishing recovery relies on it).
            PlatformPost.objects.filter(id__in=[pp.id for pp in platform_posts]).update(
                status=PlatformPost.Status.PUBLISHING, updated_at=timezone.now()
            )

        # Publish in parallel
        results = {}
        with ThreadPoolExecutor(max_workers=min(len(platform_posts), 5)) as executor:
            futures = {executor.submit(self._publish_platform_post, pp): pp for pp in platform_posts}
            for future in as_completed(futures):
                pp = futures[future]
                try:
                    results[pp.id] = future.result()
                except Exception as e:
                    results[pp.id] = {"success": False, "error": str(e)}

        # Reflect the aggregate onto Post.published_at so dashboards that
        # display "last published" don't need to query every child.
        self._sync_parent_published_at(post)

        # Schedule first comments for successful publishes (non-blocking)
        for pp in platform_posts:
            pp.refresh_from_db()
            if pp.status != PlatformPost.Status.PUBLISHED:
                continue
            if not pp.social_account.supports_first_comment():
                continue
            comment_text = pp.effective_first_comment
            if comment_text:
                _post_first_comment_task(str(pp.id), schedule=FIRST_COMMENT_DELAY)

    def _publish_platform_post(self, platform_post):
        """Publish a single PlatformPost to its target platform.

        Returns dict: {"success": bool, "platform_post_id": str, "error": str}
        """
        start_time = time.monotonic()
        account = platform_post.social_account

        # Check rate limits
        rate_state = RateLimitState.objects.filter(
            social_account=account,
            platform=account.platform,
        ).first()

        if rate_state and rate_state.is_rate_limited:
            error_msg = f"Rate limited until {rate_state.window_resets_at}"
            self._schedule_retry(platform_post, error_msg)
            return {"success": False, "error": error_msg}

        try:
            # Get the provider for this platform
            result = self._dispatch_to_provider(platform_post)

            duration_ms = int((time.monotonic() - start_time) * 1000)

            if result["success"]:
                platform_post.platform_post_id = result.get("platform_post_id", "")
                response_extra = result.get("response")
                if isinstance(response_extra, dict) and response_extra:
                    platform_post.platform_extra = {
                        **(platform_post.platform_extra or {}),
                        **response_extra,
                    }
                if result.get("pending"):
                    # Platform still processing: stay PUBLISHING; _check_pending_publishes resolves it.
                    platform_post.save()
                else:
                    self._mark_published(platform_post)
                    self._notify_thumbnail_error(platform_post)

                # Log success
                PublishLog.objects.create(
                    platform_post=platform_post,
                    attempt_number=platform_post.retry_count + 1,
                    status_code=result.get("status_code", 200),
                    response_body=str(result.get("response", ""))[:1000],
                    duration_ms=duration_ms,
                )

                # Update rate limit state
                self._update_rate_limit(account, result)

                return result
            else:
                error_msg = result.get("error", "Unknown publish error")
                duration_ms = int((time.monotonic() - start_time) * 1000)

                PublishLog.objects.create(
                    platform_post=platform_post,
                    attempt_number=platform_post.retry_count + 1,
                    status_code=result.get("status_code"),
                    response_body=str(result.get("response", ""))[:1000],
                    error_message=error_msg,
                    duration_ms=duration_ms,
                )

                self._schedule_retry(platform_post, error_msg)
                return result

        except Exception as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            error_msg = str(e)

            PublishLog.objects.create(
                platform_post=platform_post,
                attempt_number=platform_post.retry_count + 1,
                error_message=error_msg,
                duration_ms=duration_ms,
            )

            if getattr(e, "retryable", True):
                self._schedule_retry(platform_post, error_msg)
            else:
                self._fail_permanently(platform_post, error_msg)
            return {"success": False, "error": error_msg}

    def _notify_thumbnail_error(self, platform_post):
        """The video is PUBLISHED but the platform rejected the custom thumbnail: tell the author."""
        reason = (platform_post.platform_extra or {}).get("thumbnail_error")
        author = platform_post.post.author
        if not reason or author is None:
            return
        try:
            from apps.notifications.engine import notify
            from apps.notifications.models import EventType

            notify(
                user=author,
                event_type=EventType.POST_PUBLISHED,
                title="Video publicado sin miniatura",
                body=f"El video se publicó pero YouTube rechazó la miniatura: {reason}",
                data={
                    "platform_post_id": str(platform_post.id),
                    "post_id": str(platform_post.post_id),
                    "workspace_id": str(platform_post.post.workspace_id),
                },
            )
        except Exception:
            logger.warning("Could not notify thumbnail error for %s", platform_post.id, exc_info=True)

    def _mark_published(self, platform_post):
        platform_post.status = PlatformPost.Status.PUBLISHED
        platform_post.published_at = timezone.now()
        platform_post.save()

        # A published post leaves the queue: drop the QueueEntry that
        # held this channel's slot so the queue shows only upcoming posts
        # and the slot frees up as a gap. Best-effort and isolated: the
        # publish has already succeeded, so a cleanup failure here must
        # NOT fall through to the `except` in the caller (which would schedule a
        # retry and double-post). The PlatformPost + published_at are kept.
        try:
            from apps.calendar.models import QueueEntry

            QueueEntry.objects.filter(
                post_id=platform_post.post_id,
                queue__social_account_id=platform_post.social_account_id,
            ).delete()
        except Exception:
            logger.warning(
                "Failed to drop QueueEntry for published PlatformPost %s",
                platform_post.id,
                exc_info=True,
            )

    def _recover_stuck_publishing(self):
        """Turn attempts that died mid-publish (no platform id, >15 min) into a retryable FAILED."""
        return PlatformPost.objects.filter(
            status=PlatformPost.Status.PUBLISHING,
            platform_post_id="",
            updated_at__lt=timezone.now() - STUCK_PUBLISHING_TIMEOUT,
        ).update(
            status=PlatformPost.Status.FAILED,
            publish_error="La publicación se interrumpió; puedes reintentarla.",
            updated_at=timezone.now(),
        )

    def _check_pending_publishes(self):
        """Resolve PUBLISHING posts whose platform is still processing (e.g. TikTok Direct Post)."""
        pending = PlatformPost.objects.filter(
            status=PlatformPost.Status.PUBLISHING, platform_post_id__gt=""
        ).select_related("social_account__workspace", "post")
        for pp in pending:
            try:
                account = pp.social_account
                provider = get_provider(account.platform, _resolve_publish_credentials(account))
                st = provider.check_publish_status(account.get_fresh_access_token(provider), pp.platform_post_id)
                if st.state == "published":
                    if st.platform_post_id:
                        pp.platform_post_id = st.platform_post_id
                    self._mark_published(pp)
                    self._sync_parent_published_at(pp.post)
                elif st.state == "failed":
                    self._fail_permanently(pp, st.error, reason="platform reported failure")
                # Timeout only after asking: with no 24/7 worker a drain may come days later.
                elif timezone.now() - pp.updated_at > PENDING_PUBLISH_TIMEOUT:
                    self._fail_permanently(pp, "La plataforma no confirmó la publicación", reason="pending timeout")
            except NotImplementedError:
                continue  # provider has no async publish; nothing to poll
            except Exception:
                logger.exception("Error checking publish status of PlatformPost %s", pp.id)

    def _dispatch_to_provider(self, platform_post):
        """Dispatch to the appropriate platform provider.

        Resolves credentials, refreshes tokens if needed, builds a
        PublishContent payload, and calls provider.publish_post().
        Returns: {"success": bool, "platform_post_id": str, ...}
        """
        account = platform_post.social_account
        platform = account.platform

        credentials = _resolve_publish_credentials(account)
        provider = get_provider(platform, credentials)

        # Refresh a nearly-spent token before publishing, for any provider that
        # has a refresh token stored. This covers OAuth2 providers *and* session
        # providers like Bluesky, whose accessJwt expires after only a few
        # hours and must be renewed via refreshSession before each publish.
        # Shared with the inbox paths so there is one refresh policy, not three.
        access_token = account.get_fresh_access_token(provider)

        # Download media from storage (S3/cloud) to temp files for upload
        # and collect public URLs (presigned R2 / absolute) for providers
        # that require fetchable URLs (Instagram, Threads, Google Business, etc.)
        media_files = []
        media_urls = []
        temp_files = []
        attachments = list(platform_post.post.media_attachments.select_related("media_asset").order_by("position"))

        # Fail clearly (no retries) when the media can't work on this platform.
        media_error = media_error_for_platform(
            platform,
            [pm.media_asset.media_type for pm in attachments if pm.media_asset.file],
            (platform_post.platform_extra or {}).get("post_type"),
            [pm.media_asset.duration for pm in attachments if pm.media_asset.file and pm.media_asset.is_video],
        )
        if media_error:
            raise PublishError(media_error, platform=platform, retryable=False)

        # For video-only platforms (YouTube, TikTok), skip non-video attachments
        video_only = set(provider.supported_post_types) <= {PostType.VIDEO, PostType.SHORT}
        if video_only:
            attachments = [pm for pm in attachments if pm.media_asset.media_type == "video"]

        first_media_type = None
        primary_video_duration = None
        app_url = getattr(settings, "APP_URL", "").rstrip("/")
        try:
            for pm in attachments:
                asset = pm.media_asset
                if not asset.file:
                    continue
                # Track the first media type for post type detection
                if first_media_type is None:
                    first_media_type = asset.media_type

                # Capture the first video's duration so providers can enforce
                # platform max-duration limits (e.g. TikTok max_video_post_duration_sec).
                if primary_video_duration is None and asset.media_type == "video":
                    primary_video_duration = asset.duration or None

                # Collect the public/presigned URL for this asset
                url = asset.file.url
                if url.startswith("/"):
                    # Local storage: make absolute using APP_URL
                    url = f"{app_url}{url}"
                media_urls.append(url)

                # Download to a temp file (works with any storage backend)
                suffix = os.path.splitext(asset.filename)[1] or ".tmp"
                tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
                    suffix=suffix, delete=False
                )
                temp_files.append(tmp.name)
                with asset.file.open("rb") as src:
                    for chunk in iter(lambda: src.read(8192), b""):
                        tmp.write(chunk)
                tmp.close()
                media_files.append(tmp.name)

            # Merge per-platform extras (e.g. YouTube privacy_status, custom
            # tags, thumbnail) on top of the base extra dict.
            extra: dict = {}  # internal post tags are not public YouTube tags; only platform_extra["tags"]
            platform_extra = platform_post.platform_extra or {}
            extra.update(platform_extra)

            # Inject page_id for Facebook from the connected account.
            if platform == "facebook" and "page_id" not in extra:
                extra["page_id"] = account.account_platform_id

            # Inject Instagram user ID for Facebook-login Instagram accounts.
            if platform == "instagram" and "ig_user_id" not in extra:
                extra["ig_user_id"] = account.account_platform_id

            # Inject org author URN for LinkedIn Company Page.
            if platform == "linkedin_company" and "author" not in extra:
                extra["author"] = f"urn:li:organization:{account.account_platform_id}"

            # Pop link_url from extra and set on PublishContent directly
            link_url = extra.pop("link_url", None)

            # Resolve thumbnail_asset_id → temp file path for providers that
            # need to upload a custom thumbnail (YouTube).
            thumb_asset_id = extra.pop("thumbnail_asset_id", None)
            if thumb_asset_id:
                from apps.media_library.models import MediaAsset

                try:
                    thumb_asset = MediaAsset.objects.for_workspace_with_shared(
                        platform_post.post.workspace_id, platform_post.post.workspace.organization_id
                    ).get(id=thumb_asset_id)
                    if thumb_asset.file:
                        suffix = os.path.splitext(thumb_asset.filename)[1] or ".jpg"
                        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)  # noqa: SIM115
                        temp_files.append(tmp.name)
                        with thumb_asset.file.open("rb") as src:
                            for chunk in iter(lambda: src.read(8192), b""):
                                tmp.write(chunk)
                        tmp.close()
                        extra["thumbnail_file"] = tmp.name
                except MediaAsset.DoesNotExist:
                    logger.warning("Thumbnail asset %s not found", thumb_asset_id)

            # Resolve cover_image_asset_id → temp file (Pinterest video pins)
            cover_asset_id = extra.pop("cover_image_asset_id", None)
            if cover_asset_id:
                try:
                    cover_asset = MediaAsset.objects.for_workspace_with_shared(
                        platform_post.post.workspace_id, platform_post.post.workspace.organization_id
                    ).get(id=cover_asset_id)
                    if cover_asset.file:
                        suffix = os.path.splitext(cover_asset.filename)[1] or ".jpg"
                        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)  # noqa: SIM115
                        temp_files.append(tmp.name)
                        with cover_asset.file.open("rb") as src:
                            for chunk in iter(lambda: src.read(8192), b""):
                                tmp.write(chunk)
                        tmp.close()
                        extra["cover_image_file"] = tmp.name
                except MediaAsset.DoesNotExist:
                    logger.warning("Cover image asset %s not found", cover_asset_id)

            post_type = self._resolve_post_type(
                platform=platform,
                platform_extra=platform_extra,
                media_count=len(media_files),
                first_media_type=first_media_type,
            )

            content = PublishContent(
                text=platform_post.effective_caption or "",
                title=platform_post.effective_title,
                description=platform_post.effective_caption,
                first_comment=platform_post.effective_first_comment,
                media_files=media_files,
                media_urls=media_urls,
                post_type=post_type,
                extra=extra,
                link_url=link_url,
                video_duration_sec=primary_video_duration,
            )

            logger.info(
                "Publishing to %s (account: %s, type: %s, media: %d)",
                platform,
                account.account_name,
                post_type.value,
                len(media_files),
            )
            result = provider.publish_post(access_token, content)
            return {
                "success": True,
                "platform_post_id": result.platform_post_id,
                "url": result.url,
                "response": result.extra,
                "pending": result.pending,
            }
        finally:
            # Clean up temp files regardless of success/failure
            for path in temp_files:
                with contextlib.suppress(OSError):
                    os.unlink(path)

    @staticmethod
    def _resolve_post_type(
        platform: str,
        platform_extra: dict,
        media_count: int,
        first_media_type: str | None,
    ) -> PostType:
        """Derive the correct PostType from context.

        Priority:
        1. Explicit hint in platform_extra (validated against PostType enum)
        2. Platform defaults (Pinterest → PIN)
        3. Multi-media on carousel-capable platforms → CAROUSEL
        4. Fallback: video → VIDEO, image → IMAGE, else → TEXT
        """
        # 1. Explicit post_type hint from platform_extra
        hint = platform_extra.get("post_type")
        if hint:
            valid_values = {pt.value for pt in PostType}
            if hint in valid_values:
                return PostType(hint)
            logger.warning("Invalid post_type hint %r, ignoring", hint)

        # 2. Platform defaults
        if platform == "pinterest":
            return PostType.PIN

        # 3. Multi-media → CAROUSEL for Instagram/Threads
        if media_count > 1 and platform in (
            "instagram",
            "instagram_login",
            "threads",
        ):
            return PostType.CAROUSEL

        # 4. Fallback based on first media type
        if first_media_type == "video":
            return PostType.VIDEO
        if first_media_type == "image":
            return PostType.IMAGE
        return PostType.TEXT

    def _fail_permanently(self, platform_post, error_msg, *, reason="non-retryable"):
        """Mark a post FAILED with no further retries."""
        platform_post.status = PlatformPost.Status.FAILED
        platform_post.publish_error = error_msg
        platform_post.save()
        logger.warning(
            "PlatformPost %s failed (%s): %s",
            platform_post.id,
            reason,
            error_msg,
        )

    def _schedule_retry(self, platform_post, error_msg):
        """Schedule a retry with exponential backoff."""
        if platform_post.retry_count >= MAX_RETRIES:
            self._fail_permanently(platform_post, error_msg, reason=f"after {MAX_RETRIES} retries")
            return

        backoff_seconds = RETRY_BACKOFF[min(platform_post.retry_count, len(RETRY_BACKOFF) - 1)]
        platform_post.retry_count += 1
        platform_post.next_retry_at = timezone.now() + timedelta(seconds=backoff_seconds)
        # Drop back to SCHEDULED so the next _process_retries tick picks it up
        # once next_retry_at passes.
        platform_post.status = PlatformPost.Status.SCHEDULED
        platform_post.publish_error = error_msg
        platform_post.save()

        logger.info(
            "Scheduled retry %d for PlatformPost %s in %d seconds",
            platform_post.retry_count,
            platform_post.id,
            backoff_seconds,
        )

    def _process_retries(self):
        """Process platform posts that are due for retry."""
        now = timezone.now()
        # Mirror the primary due-query's hold guard: a retrying child must not
        # publish while any sibling is on_hold (the exclude covers the query
        # window; the per-row re-check below closes a hold placed after it).
        retry_posts = (
            PlatformPost.objects.filter(
                status=PlatformPost.Status.SCHEDULED,
                retry_count__gt=0,
                retry_count__lte=MAX_RETRIES,
                next_retry_at__lte=now,
            )
            .exclude(post__platform_posts__status=PlatformPost.Status.ON_HOLD)
            .select_related("social_account", "post")
        )

        for pp in retry_posts:
            if pp.post.platform_posts.filter(status=PlatformPost.Status.ON_HOLD).exists():
                continue
            try:
                # Atomic claim: publish-now calls this outside the task lock, so a concurrent drain could race.
                claimed = PlatformPost.objects.filter(id=pp.id, status=PlatformPost.Status.SCHEDULED).update(
                    status=PlatformPost.Status.PUBLISHING, updated_at=timezone.now()
                )
                if not claimed:
                    continue
                pp.status = PlatformPost.Status.PUBLISHING
                result = self._publish_platform_post(pp)
                if result.get("success"):
                    self._sync_parent_published_at(pp.post)
            except Exception:
                logger.exception("Error retrying PlatformPost %s", pp.id)

    def _update_rate_limit(self, account, result):
        """Update rate limit state from API response headers."""
        remaining = result.get("rate_limit_remaining")
        resets_at = result.get("rate_limit_resets_at")

        if remaining is not None:
            RateLimitState.objects.update_or_create(
                social_account=account,
                platform=account.platform,
                defaults={
                    "requests_remaining": remaining,
                    "window_resets_at": resets_at,
                },
            )

    def _sync_parent_published_at(self, post):
        """Reflect the latest child published_at onto the parent Post.

        Status itself lives entirely on PlatformPost now (Post.status is a
        derived property), but we still maintain ``Post.published_at`` so
        dashboards/lists that show "last published" don't have to aggregate
        children at read time.
        """
        latest = max(
            (pp.published_at for pp in post.platform_posts.all() if pp.published_at),
            default=None,
        )
        if latest and post.published_at != latest:
            post.published_at = latest
            post.save(update_fields=["published_at", "updated_at"])


@background(schedule=0)
def _post_first_comment_task(platform_post_id):
    """Post the first comment as a background task (avoids blocking the publisher thread)."""
    try:
        platform_post = PlatformPost.objects.select_related("social_account__workspace__organization").get(
            pk=platform_post_id
        )
    except PlatformPost.DoesNotExist:
        logger.warning("PlatformPost %s not found for first comment.", platform_post_id)
        return

    comment_text = platform_post.effective_first_comment
    if not comment_text:
        return

    account = platform_post.social_account
    try:
        credentials = _resolve_publish_credentials(account)
        provider = get_provider(account.platform, credentials)
        provider.publish_comment(
            access_token=account.get_fresh_access_token(provider),
            post_id=platform_post.platform_post_id,
            text=comment_text,
        )
        logger.info("Posted first comment for PlatformPost %s", platform_post.id)
    except NotImplementedError:
        logger.info("First comment not supported for %s", account.platform)
    except Exception:
        logger.exception("Failed to post first comment for PlatformPost %s", platform_post.id)
