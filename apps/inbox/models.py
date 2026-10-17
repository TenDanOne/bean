"""Models for the Unified Social Inbox (F-3.1)."""

import uuid

from django.conf import settings
from django.db import models

from apps.common.managers import WorkspaceScopedManager


class InboxMessage(models.Model):
    class MessageType(models.TextChoices):
        COMMENT = "comment", "Comment"
        MENTION = "mention", "Mention"
        DM = "dm", "Direct Message"
        REVIEW = "review", "Review"

    class Status(models.TextChoices):
        UNREAD = "unread", "Unread"
        OPEN = "open", "Open"
        RESOLVED = "resolved", "Resolved"
        ARCHIVED = "archived", "Archived"

    class Sentiment(models.TextChoices):
        POSITIVE = "positive", "Positive"
        NEUTRAL = "neutral", "Neutral"
        NEGATIVE = "negative", "Negative"

    class SentimentSource(models.TextChoices):
        AUTO = "auto", "Auto"
        MANUAL = "manual", "Manual"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="inbox_messages",
    )
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount",
        on_delete=models.CASCADE,
        related_name="inbox_messages",
    )
    platform_message_id = models.CharField(max_length=255, db_index=True)
    message_type = models.CharField(
        max_length=20,
        choices=MessageType.choices,
        default=MessageType.COMMENT,
        db_index=True,
    )
    sender_name = models.CharField(max_length=255)
    sender_handle = models.CharField(max_length=255, blank=True, default="")
    sender_avatar_url = models.URLField(max_length=500, blank=True, default="")
    body = models.TextField(blank=True, default="")
    sentiment = models.CharField(
        max_length=10,
        choices=Sentiment.choices,
        default=Sentiment.NEUTRAL,
        db_index=True,
    )
    sentiment_source = models.CharField(
        max_length=10,
        choices=SentimentSource.choices,
        default=SentimentSource.AUTO,
    )
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.UNREAD,
        db_index=True,
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_inbox_messages",
    )
    parent_message = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="thread_replies",
    )
    is_outbound = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Written by the connected account itself, so it needs no reply.",
    )
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_inbox_messages",
        help_text="Who sent it from Brightbean. Empty on an outbound message means it was written directly on the platform.",
    )
    related_post = models.ForeignKey(
        "composer.PlatformPost",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inbox_messages",
    )
    # Moderation state, mirrored from the platform rather than replacing it.
    #
    # The row is kept in both cases. A hidden comment can be brought back, and
    # keeping a deleted one means the thread still shows what we answered and
    # who did what — deleting our own record as well would erase the evidence
    # along with the comment.
    is_hidden = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Hidden on the platform so the public cannot see it. Reversible.",
    )
    deleted_on_platform = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When it was deleted for good. The row stays so the thread and the audit trail survive.",
    )
    moderated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="moderated_inbox_messages",
        help_text="Who last hid, restored or deleted it.",
    )

    extra = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "inbox_message"
        ordering = ["-received_at"]
        unique_together = [("social_account", "platform_message_id")]
        indexes = [
            models.Index(
                fields=["workspace", "status", "-received_at"],
                name="inbox_msg_ws_status_recv",
            ),
            models.Index(
                fields=["workspace", "assigned_to", "status"],
                name="inbox_msg_ws_assign_status",
            ),
            models.Index(
                fields=["workspace", "social_account", "-received_at"],
                name="inbox_msg_ws_account_recv",
            ),
        ]

    def __str__(self):
        return f"{self.get_message_type_display()} from {self.sender_name}"

    @property
    def platform(self):
        return self.social_account.platform

    # Ids that are not comment ids, per platform. YouTube's PubSubHubbub feed
    # announces a new video as an Atom entry (`yt:video:<id>`), and the webhook
    # files those under message_type=COMMENT — so without this they offer a Hide
    # button that would post a *video* id to setModerationStatus.
    NON_COMMENT_ID_PREFIXES = ("yt:video:", "yt:channel:")

    @property
    def is_moderatable(self):
        """Whether hiding or deleting this on the platform means anything.

        Read by the template and by the two moderation views, so a row that
        cannot be moderated never shows the buttons *and* is refused if someone
        posts to the endpoint anyway.
        """
        if self.message_type != self.MessageType.COMMENT:
            return False
        return not self.platform_message_id.startswith(self.NON_COMMENT_ID_PREFIXES)


class InboxReply(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inbox_message = models.ForeignKey(
        InboxMessage,
        on_delete=models.CASCADE,
        related_name="replies",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="inbox_replies",
    )
    body = models.TextField()
    platform_reply_id = models.CharField(max_length=255, blank=True, default="")
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inbox_reply"
        ordering = ["sent_at"]

    def __str__(self):
        return f"Reply by {self.author} on {self.sent_at:%Y-%m-%d %H:%M}"


class InternalNote(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inbox_message = models.ForeignKey(
        InboxMessage,
        on_delete=models.CASCADE,
        related_name="internal_notes",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="inbox_notes",
    )
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inbox_internal_note"
        ordering = ["created_at"]

    def __str__(self):
        return f"Note by {self.author} on {self.created_at:%Y-%m-%d %H:%M}"


class SavedReply(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="saved_replies",
    )
    title = models.CharField(max_length=255)
    body = models.TextField(
        help_text="Supports variables: {sender_name}, {account_name}, {post_url}",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_saved_replies",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "inbox_saved_reply"
        ordering = ["title"]

    def __str__(self):
        return self.title

    def render(self, context: dict) -> str:
        """Substitute variables in body with context values."""
        text = self.body
        for key, value in context.items():
            text = text.replace(f"{{{key}}}", str(value))
        return text


class InboxSLAConfig(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.OneToOneField(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="inbox_sla_config",
    )
    target_response_minutes = models.PositiveIntegerField(default=120)
    is_active = models.BooleanField(default=False)
    auto_resolve_on_reply = models.BooleanField(
        default=True,
        help_text="Automatically mark messages as resolved when a reply is sent.",
    )

    class Meta:
        db_table = "inbox_sla_config"

    def __str__(self):
        return f"SLA Config for {self.workspace} ({self.target_response_minutes}min)"


class InboxBackfillState(models.Model):
    """How far back this channel's history has actually been fetched.

    Exists because the sync had no memory of it. ``since`` was derived from the
    *newest* message already stored, so a first load that broke halfway —
    network, timeout, exhausted quota — left the older comments it never reached
    permanently unreachable: the next run only asked for things newer than the
    newest it had. YouTube pages newest-first, so the hole was always at the old
    end, where nobody looks.

    Separating "catch up on new" from "reach back through history" is the fix.
    They are different questions with different cadences, and only the second
    one leaves damage when it fails.

    A OneToOne rather than columns on SocialAccount: six fields that matter only
    during the initial load and sit inert forever after, and deleting the row is
    a clean way to force a re-import without touching the channel.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETE = "complete", "Complete"
        # Quota spent, or too many failures. Distinct from an error: stalled is
        # expected and resolves itself, an error needs someone to look.
        STALLED = "stalled", "Stalled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    social_account = models.OneToOneField(
        "social_accounts.SocialAccount",
        on_delete=models.CASCADE,
        related_name="backfill_state",
    )
    target_at = models.DateTimeField(
        help_text="Oldest point we intend to reach. Connection time minus the configured window."
    )
    reached_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Oldest message actually fetched. Walks backwards until it passes target_at.",
    )
    page_token = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text="The provider's cursor, so an interrupted run resumes mid-pagination.",
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    attempts = models.IntegerField(
        default=0,
        help_text="Consecutive real failures. Exhausted quota does not count — it is not a failure.",
    )
    last_error = models.TextField(blank=True, default="")
    last_run_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "inbox_backfill_state"

    def __str__(self):
        return f"Backfill {self.social_account} ({self.status})"

    @property
    def is_done(self):
        return self.status == self.Status.COMPLETE

    def mark_stalled(self, reason: str):
        """Pause without counting it against the retry budget.

        Used when quota runs out. The next cycle picks it up again from the same
        cursor, which is what "try again tomorrow" actually means here — nothing
        already fetched is re-fetched.
        """
        self.status = self.Status.STALLED
        self.last_error = reason
        self.save(update_fields=["status", "last_error", "updated_at"])
