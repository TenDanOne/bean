"""Apply the new threading/outbound semantics to messages already synced.

Without this the existing inbox keeps showing every reply the connected account
ever made as an unanswered inbound message, and every reply detached from the
comment it answers — the schema would be right and the data still wrong.

Also drops replies that were never delivered: rows with an empty
``platform_reply_id`` are the artefact of the old ``send_reply``, which stored a
reply even when the provider rejected it, so the thread claimed a message had
been sent when the platform never received it.
"""

from django.db import migrations


def apply_semantics(apps, schema_editor):
    InboxMessage = apps.get_model("inbox", "InboxMessage")
    InboxReply = apps.get_model("inbox", "InboxReply")

    # Never-delivered replies: the old failure path persisted these anyway.
    InboxReply.objects.filter(platform_reply_id="").delete()

    for message in InboxMessage.objects.select_related("social_account").iterator():
        account = message.social_account
        updates = []

        handle = message.sender_handle or message.extra.get("sender_handle", "")
        is_outbound = bool(account.account_platform_id) and handle == account.account_platform_id
        if is_outbound != message.is_outbound:
            message.is_outbound = is_outbound
            updates.append("is_outbound")

        parent_id = (message.extra or {}).get("parent_id")
        if parent_id and message.parent_message_id is None:
            parent = InboxMessage.objects.filter(
                social_account=account,
                platform_message_id=parent_id,
            ).first()
            if parent is not None:
                message.parent_message = parent
                updates.append("parent_message")

        if is_outbound and message.sent_by_id is None:
            pending = InboxReply.objects.filter(platform_reply_id=message.platform_message_id).first()
            if pending is not None:
                message.sent_by_id = pending.author_id
                updates.append("sent_by")
                pending.delete()

        if updates:
            message.save(update_fields=updates)


def noop(apps, schema_editor):
    """Irreversible: the deleted rows can't be reconstructed, and the flags are
    derived data that the next sync recomputes anyway."""


class Migration(migrations.Migration):
    dependencies = [
        ("inbox", "0002_inboxmessage_is_outbound_inboxmessage_sent_by"),
    ]

    operations = [
        migrations.RunPython(apply_semantics, noop),
    ]
