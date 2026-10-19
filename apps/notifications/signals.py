"""Emite POST_PUBLISHED / POST_FAILED cuando una PlatformPost llega a ese estado.

Va por señal para no tocar el motor de publicación: ``_mark_published`` y
``_fail_permanently`` guardan con ``save()``, lo que dispara ``post_save``.
"""

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.composer.models import PlatformPost

from .engine import notify
from .models import EventType, Notification

_EVENTS: dict[str, tuple] = {
    PlatformPost.Status.PUBLISHED: (EventType.POST_PUBLISHED, "Publicación publicada", "se publicó en"),
    PlatformPost.Status.FAILED: (EventType.POST_FAILED, "Falló la publicación", "no se pudo publicar en"),
}


@receiver(post_save, sender=PlatformPost)
def notify_publish_outcome(sender, instance: PlatformPost, created: bool, **kwargs):
    event = _EVENTS.get(str(instance.status))
    if created or event is None or instance.post.author is None:
        return
    author = instance.post.author
    event_type, title, verb = event
    # shortcut: una sola notificación por publicación y evento; si falla de nuevo tras un reintento manual no se repite.
    if Notification.objects.filter(
        user=author, event_type=event_type, data__platform_post_id=str(instance.id)
    ).exists():
        return
    account = instance.social_account
    body = f"«{instance.post.title or 'Sin título'}» {verb} {account.account_name or account.account_handle}."
    if instance.status == PlatformPost.Status.FAILED and instance.publish_error:
        body += f" {instance.publish_error}"
    notify(
        user=author,
        event_type=event_type,
        title=title,
        body=body,
        data={
            "platform_post_id": str(instance.id),
            "post_id": str(instance.post_id),
            "workspace_id": str(instance.post.workspace_id),
        },
    )
