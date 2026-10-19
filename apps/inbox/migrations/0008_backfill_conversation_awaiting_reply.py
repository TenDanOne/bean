from django.db import migrations
from django.db.models import Exists, OuterRef, Q, Value
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Coalesce


def backfill(apps, schema_editor):
    """Fill conversation_id from extra, and awaiting_reply for what is plausibly still unanswered.

    Unanswered = inbound, not deleted, not resolved/archived, a real comment or
    DM (not YouTube's video announcements), with no outbound reply later in its
    thread and no reply sent from the app.
    """
    InboxMessage = apps.get_model("inbox", "InboxMessage")
    InboxReply = apps.get_model("inbox", "InboxReply")

    InboxMessage.objects.filter(extra__has_key="conversation_id", conversation_id="").update(
        conversation_id=Coalesce(KeyTextTransform("conversation_id", "extra"), Value(""))
    )

    answered_in_thread = InboxMessage.objects.filter(
        is_outbound=True, social_account_id=OuterRef("social_account_id")
    ).filter(
        Q(parent_message_id=OuterRef("id"))
        | (~Q(conversation_id="") & Q(conversation_id=OuterRef("conversation_id"), received_at__gte=OuterRef("received_at")))
    )
    (
        InboxMessage.objects.filter(is_outbound=False, deleted_on_platform__isnull=True, status__in=["unread", "open"])
        .exclude(platform_message_id__startswith="yt:")
        .exclude(Exists(answered_in_thread))
        .exclude(Exists(InboxReply.objects.filter(inbox_message_id=OuterRef("id"))))
        .update(awaiting_reply=True)
    )


class Migration(migrations.Migration):
    dependencies = [
        ("inbox", "0007_inboxmessage_conversation_awaiting_reply"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
