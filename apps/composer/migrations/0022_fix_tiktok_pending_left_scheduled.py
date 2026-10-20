"""Rows hit by the stale-save bug: TikTok already accepted them (publish_id stored) but they were left
SCHEDULED, so the publisher would send them again. Move them to PUBLISHING so the status check resolves them."""

from django.db import migrations


def forwards(apps, schema_editor):
    PlatformPost = apps.get_model("composer", "PlatformPost")
    PlatformPost.objects.filter(
        status="scheduled",
        social_account__platform="tiktok",
    ).exclude(platform_post_id="").update(status="publishing")


class Migration(migrations.Migration):
    dependencies = [
        ("composer", "0021_alter_csvimportjob_status_alter_idea_status_and_more"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
