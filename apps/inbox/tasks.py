"""Inbox sync engine - polls connected accounts for new messages."""

import logging
from datetime import timedelta

from background_task import background
from django.utils import timezone

from apps.members.models import WorkspaceMembership
from apps.notifications.engine import notify
from apps.notifications.models import EventType
from apps.settings_manager.defaults import APP_DEFAULTS
from apps.settings_manager.helpers import get_setting
from apps.social_accounts.models import SocialAccount
from providers import get_provider
from providers.exceptions import RateLimitError

from .grouping import extract_platform_post_id, resolve_related_post_for
from .models import InboxMessage, InboxReply, InboxSLAConfig
from .sentiment import analyze_sentiment

logger = logging.getLogger(__name__)

# On an account's first-ever sync we may pull a historical backlog; notifications
# are suppressed for it, EXCEPT messages newer than this window — so a long-quiet
# account's genuinely-new first message still alerts instead of being swallowed.
INBOX_BACKLOG_NOTIFY_WINDOW = timedelta(hours=1)


def _is_recent(ts):
    """True if a provider message timestamp falls within the backlog-notify window."""
    if ts is None:
        return False
    if timezone.is_naive(ts):
        ts = timezone.make_aware(ts, timezone.get_default_timezone())
    return ts >= timezone.now() - INBOX_BACKLOG_NOTIFY_WINDOW


def _first_sync_cutoff(account):
    """Oldest timestamp the first sync of ``account`` should reach back to.

    Passing ``since=None`` makes providers hand back everything they hold, which
    on the first poll means importing the account's whole history — and on
    YouTube that walks every comment thread the channel ever received, one
    quota-billed request per page. Bound it with the configured window instead.
    Returns ``None`` when the window is 0, restoring the unbounded behaviour.
    """
    days = get_setting(account.workspace_id, "inbox.first_sync_backfill_days")
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = APP_DEFAULTS["inbox.first_sync_backfill_days"]
    if days <= 0:
        return None
    return timezone.now() - timedelta(days=days)


class InboxSyncEngine:
    """Syncs inbox messages from all connected social accounts."""

    def run_cycle(self):
        """Run one full inbox cycle: poll every connected account, then check SLAs.

        Single shared entry point for the ``run_inbox_sync`` management command
        and the recurring ``run_inbox_sync_cycle`` background task, so the two
        never diverge.
        """
        self.sync_all()
        self.check_sla()

    def sync_all(self):
        """Poll each connected account for new messages."""
        accounts = SocialAccount.objects.filter(
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        ).select_related("workspace")

        for account in accounts:
            try:
                self._sync_account(account)
            except Exception:
                logger.exception("Inbox sync failed for account %s", account.id)

    def _sync_account(self, account):
        """Sync messages for a single social account."""
        from apps.publisher.engine import _resolve_publish_credentials

        try:
            provider = get_provider(account.platform, _resolve_publish_credentials(account))
        except ValueError:
            logger.warning("No provider for platform %s", account.platform)
            return

        last_msg = (
            InboxMessage.objects.filter(social_account=account)
            .order_by("-received_at")
            .values_list("received_at", flat=True)
            .first()
        )
        is_first_sync = last_msg is None
        since = last_msg if last_msg is not None else _first_sync_cutoff(account)

        try:
            messages = provider.get_messages(
                access_token=account.get_fresh_access_token(provider),
                since=since,
            )
        except NotImplementedError:
            return
        except RateLimitError:
            # Not a failure — the quota for the day is spent, and the next cycle
            # picks up where this one stopped. Logged at warning rather than
            # exception so a normal end-of-quota does not sit in the error feed
            # looking like something broke.
            logger.warning(
                "Inbox sync for %s (%s) hit the platform rate limit; resuming next cycle.",
                account.id,
                account.platform,
            )
            return
        except Exception:
            # Anything else is a real fault — a revoked token, a schema change,
            # a bug — and wants a traceback and someone's attention.
            logger.exception(
                "get_messages() failed for account %s (%s)",
                account.id,
                account.platform,
            )
            return

        # Folder labels for the inbox, fetched once per sync for the whole batch.
        # A failure here costs titles and nothing else — the comments are already
        # in hand, and a folder falls back to showing the post id.
        details = self._fetch_post_details(provider, account, messages)
        for msg in messages:
            if details:
                post_id = extract_platform_post_id(msg)
                found = details.get(post_id) if post_id else None
                if found:
                    msg.extra["post_title"] = found.get("title", "")
                    if found.get("published_at"):
                        msg.extra["post_published_at"] = found["published_at"]
            # Suppress notifications for the historical backlog pulled on an
            # account's first sync, but still alert for genuinely recent messages:
            # a long-quiet account's first real message also has last_msg=None, so
            # a blanket first-sync mute would silently swallow it. backfill_inbox
            # seeds explicit history silently (notify=False).
            notify_new = not is_first_sync or _is_recent(msg.timestamp)
            self._upsert_message(account, msg, notify=notify_new)

    @staticmethod
    def _fetch_post_details(provider, account, messages):
        """Titles for every publication in this batch, or {} if unavailable.

        Wrapped in a bare except on purpose: this is decoration. A platform that
        does not implement it, a network blip or a revoked scope must not stop
        comments from syncing — the whole point of the inbox is the comments.
        """
        post_ids = [pid for pid in (extract_platform_post_id(m) for m in messages) if pid]
        if not post_ids:
            return {}
        try:
            return provider.get_post_details(
                access_token=account.get_fresh_access_token(provider),
                post_ids=post_ids,
            )
        except NotImplementedError:
            return {}
        except Exception:
            logger.warning(
                "get_post_details() failed for account %s (%s); folders will show ids",
                account.id,
                account.platform,
                exc_info=True,
            )
            return {}

    def _upsert_message(self, account, msg, notify=True):
        """Create or update an inbox message, deduplicating by platform_message_id."""
        sender_handle = msg.extra.get("sender_handle", msg.sender_id)
        # A comment authored by the connected account is something we said, not
        # something to answer. Without this every reply came back through sync as
        # a fresh inbound message and half the inbox was our own voice. Instagram
        # comments only carry the username, so the handle counts too.
        own = {h.lower() for h in (account.account_platform_id, (account.account_handle or "").lstrip("@")) if h}
        is_outbound = str(sender_handle).lower() in own
        parent = self._resolve_parent(account, msg)

        defaults = {
            "workspace": account.workspace,
            "sender_name": msg.sender_name,
            "sender_handle": sender_handle,
            "sender_avatar_url": msg.extra.get("sender_avatar_url", ""),
            "body": msg.text,
            "message_type": msg.message_type,
            "received_at": msg.timestamp,
            "extra": msg.extra,
            "is_outbound": is_outbound,
            "parent_message": parent,
            "conversation_id": msg.extra.get("conversation_id", ""),
            # The publication this comment sits under. Filled at sync time
            # so the inbox can group by post without a join through JSON on
            # every page load.
            "related_post": resolve_related_post_for(account, msg),
        }
        # Our own words and thread replies never start out UNREAD.
        create_defaults = {**defaults, **({"status": InboxMessage.Status.OPEN} if is_outbound or parent else {})}
        obj, created = InboxMessage.objects.update_or_create(
            social_account=account,
            platform_message_id=msg.platform_message_id,
            defaults=defaults,
            create_defaults=create_defaults,
        )

        # A webhook DM stored under "user:<psid>" joins the real thread once polling reports its id.
        recipient = msg.extra.get("recipient_id")
        if recipient and obj.conversation_id and obj.conversation_id != f"user:{recipient}":
            InboxMessage.objects.filter(social_account=account, conversation_id=f"user:{recipient}").update(
                conversation_id=obj.conversation_id
            )

        if is_outbound:
            self._reconcile_with_pending_reply(obj)
            obj.mark_thread_answered(before=obj.received_at)

        if created:
            obj.sentiment = analyze_sentiment(obj.body)
            update_fields = ["sentiment"]
            # YouTube's video announcements are not something a person wrote to us.
            if not is_outbound and not obj.platform_message_id.startswith(InboxMessage.NON_COMMENT_ID_PREFIXES):
                obj.awaiting_reply = not self._already_answered(obj)
                update_fields.append("awaiting_reply")
            obj.save(update_fields=update_fields)
            # Never alert on our own words.
            if notify and not is_outbound:
                self._notify_new_message(obj)

    @staticmethod
    def _already_answered(message):
        """Whether something of ours later in the thread already answers this (history imported out of order)."""
        later = InboxMessage.objects.filter(
            social_account_id=message.social_account_id, is_outbound=True, received_at__gte=message.received_at
        )
        if message.conversation_id:
            later = later.filter(conversation_id=message.conversation_id)
        else:
            later = later.filter(parent_message_id=message.parent_message_id or message.id)
        return later.exists() or message.replies.exists()

    @staticmethod
    def _resolve_parent(account, msg):
        """Link a reply to the comment it answers, when the provider reported one.

        Providers put the parent's platform id in ``extra["parent_id"]``. The
        parent may legitimately be missing — a `since` window can start midway
        through a thread — in which case the reply stays top-level rather than
        being dropped.
        """
        parent_id = msg.extra.get("parent_id")
        if not parent_id:
            return None
        return InboxMessage.objects.filter(
            social_account=account,
            platform_message_id=parent_id,
        ).first()

    @staticmethod
    def _reconcile_with_pending_reply(message):
        """Fold the local ``InboxReply`` record into the message the API returned.

        A reply sent from Brightbean is written to ``InboxReply`` immediately and
        then comes back through sync as a comment by our own channel — the same
        text stored twice, in two models, with nothing linking them. The API copy
        is the better record (real platform id, platform timestamp, thread
        position), so it absorbs the author and the local row goes away.

        An outbound message with no matching row was written straight on the
        platform; it keeps ``sent_by`` empty, which is how the thread tells the
        two apart.
        """
        pending = InboxReply.objects.filter(platform_reply_id=message.platform_message_id).first()
        if pending is None:
            return
        message.sent_by = pending.author
        message.save(update_fields=["sent_by"])
        pending.delete()

    def _notify_new_message(self, message):
        """Send notification for a new inbox message."""
        if message.assigned_to:
            users = [message.assigned_to]
        else:
            memberships = WorkspaceMembership.objects.filter(
                workspace=message.workspace,
                workspace_role__in=["owner", "manager"],
            ).select_related("user")
            users = [m.user for m in memberships]

        for user in users:
            notify(
                user=user,
                event_type=EventType.NEW_INBOX_MESSAGE,
                title=f"Nuevo {message.get_message_type_display()} de {message.sender_name}",
                body=message.body[:200],
                data={
                    "message_id": str(message.id),
                    "workspace_id": str(message.workspace_id),
                },
            )

    def check_sla(self):
        """Check for SLA-overdue messages and send notifications."""
        from datetime import timedelta

        configs = InboxSLAConfig.objects.filter(is_active=True).select_related("workspace")

        for config in configs:
            threshold = timezone.now() - timedelta(minutes=config.target_response_minutes)
            overdue_messages = InboxMessage.objects.filter(
                workspace=config.workspace,
                status__in=[InboxMessage.Status.UNREAD, InboxMessage.Status.OPEN],
                received_at__lte=threshold,
            ).exclude(extra__has_key="sla_notified")

            for message in overdue_messages:
                self._notify_sla_overdue(message, config)
                message.extra["sla_notified"] = True
                message.save(update_fields=["extra"])

    def _notify_sla_overdue(self, message, config):
        """Notify about an SLA-overdue message."""
        if message.assigned_to:
            users = [message.assigned_to]
        else:
            memberships = WorkspaceMembership.objects.filter(
                workspace=message.workspace,
                workspace_role__in=["owner", "manager"],
            ).select_related("user")
            users = [m.user for m in memberships]

        for user in users:
            notify(
                user=user,
                event_type=EventType.INBOX_SLA_OVERDUE,
                title=f"SLA vencido: {message.get_message_type_display()} de {message.sender_name}",
                body=f"Se superó el tiempo objetivo de respuesta de {config.target_response_minutes} minutos.",
                data={
                    "message_id": str(message.id),
                    "workspace_id": str(message.workspace_id),
                },
            )


# How often the recurring inbox-sync cycle runs; registered on a repeating
# schedule by apps.inbox.apps.InboxConfig. Polling is the always-on baseline
# documented in architecture.md (webhooks, where configured, supplement it).
# Every minute. Steady-state cost on YouTube is one `commentThreads.list` unit
# per cycle (~1,440/day against a 10,000-unit daily quota) now that the channel
# ID comes from the stored account instead of a `channels.list` round-trip.
# Note this cadence is global: every connected account is polled each cycle, so
# revisit it if platforms with tighter rate limits get connected.
INBOX_SYNC_INTERVAL_SECONDS = 60


@background(schedule=0)
def run_inbox_sync_cycle():
    """Run one inbox cycle on the shared ``process_tasks`` worker (every deploy target).

    Delegates to ``InboxSyncEngine.run_cycle`` — the same entry point the
    ``run_inbox_sync`` management command uses — so the two never diverge.
    """
    InboxSyncEngine().run_cycle()
