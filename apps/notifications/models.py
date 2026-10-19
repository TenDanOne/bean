import uuid

from django.conf import settings
from django.db import models


class EventType(models.TextChoices):
    POST_SUBMITTED = "post_submitted", "Publicación enviada a aprobación"
    POST_APPROVED = "post_approved", "Publicación aprobada"
    POST_CHANGES_REQUESTED = "post_changes_requested", "Cambios solicitados en la publicación"
    POST_REJECTED = "post_rejected", "Publicación rechazada"
    POST_PUBLISHED = "post_published", "Publicación publicada"
    POST_FAILED = "post_failed", "Falló la publicación"
    NEW_INBOX_MESSAGE = "new_inbox_message", "Nuevo mensaje en la bandeja"
    INBOX_SLA_OVERDUE = "inbox_sla_overdue", "SLA de la bandeja vencido"
    CLIENT_APPROVAL_REQUESTED = "client_approval_requested", "Aprobación del cliente solicitada"
    TEAM_MEMBER_INVITED = "team_member_invited", "Miembro del equipo invitado"
    SOCIAL_ACCOUNT_DISCONNECTED = "social_account_disconnected", "Cuenta social desconectada"
    REPORT_GENERATED = "report_generated", "Reporte generado"
    ENGAGEMENT_ALERT = "engagement_alert", "Alerta de interacción"
    COMMENT_MENTION = "comment_mention", "Mencionado en un comentario"
    APPROVAL_REMINDER = "approval_reminder", "Recordatorio de aprobación"
    APPROVAL_STALLED = "approval_stalled", "Escalamiento de aprobación estancada"
    APPROVAL_HOLD_REQUESTED = "approval_hold_requested", "El cliente pidió una pausa"
    CLIENT_CONNECTED_ACCOUNTS = "client_connected_accounts", "El cliente conectó cuentas"


class Channel(models.TextChoices):
    IN_APP = "in_app", "En la app"
    EMAIL = "email", "Correo"
    WEBHOOK = "webhook", "Webhook"


class DeliveryStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    DELIVERED = "delivered", "Delivered"
    FAILED = "failed", "Failed"


class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    event_type = models.CharField(max_length=50, choices=EventType.choices, db_index=True)
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True, default="")
    data = models.JSONField(default=dict, blank=True)
    is_read = models.BooleanField(default=False, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "notifications_notification"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["user", "is_read", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.title} → {self.user}"


class NotificationPreference(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notification_preferences",
    )
    event_type = models.CharField(max_length=50, choices=EventType.choices)
    channel = models.CharField(max_length=20, choices=Channel.choices)
    is_enabled = models.BooleanField(default=True)

    class Meta:
        db_table = "notifications_preference"
        unique_together = [("user", "event_type", "channel")]

    def __str__(self):
        status = "on" if self.is_enabled else "off"
        return f"{self.user} | {self.event_type} | {self.channel} = {status}"


class NotificationDelivery(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    notification = models.ForeignKey(
        Notification,
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    channel = models.CharField(max_length=20, choices=Channel.choices)
    status = models.CharField(
        max_length=20,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.PENDING,
        db_index=True,
    )
    error_message = models.TextField(blank=True, default="")
    delivered_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "notifications_delivery"
        indexes = [
            models.Index(fields=["status", "next_retry_at"]),
        ]

    def __str__(self):
        return f"{self.notification.title} via {self.channel} ({self.status})"


class QuietHours(models.Model):
    """Per-user quiet hours configuration for notification suppression."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="quiet_hours",
    )
    is_enabled = models.BooleanField(default=False)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    timezone = models.CharField(max_length=50, default="UTC")
    digest_mode = models.BooleanField(
        default=False,
        help_text="Batch email notifications into a daily digest.",
    )

    class Meta:
        db_table = "notifications_quiet_hours"
        verbose_name_plural = "Quiet hours"

    def __str__(self):
        if self.is_enabled:
            return f"{self.user} quiet {self.start_time}–{self.end_time} ({self.timezone})"
        return f"{self.user} quiet hours disabled"
