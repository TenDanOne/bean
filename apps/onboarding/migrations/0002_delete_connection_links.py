"""Drop the connection-link tables.

The connection link was a magic URL that let someone outside BBS connect their
own social accounts without having an account here. In BBS the owner connects
every channel from inside, so the flow is gone — and with it a second, parallel
OAuth implementation that ran alongside the normal connect path.

The get-started checklist stays: it lives in the same app but is a different
feature, and removing it by association would have cost something useful.

Written by hand because ``makemigrations`` emitted the operations in an order
that fails: it dropped ``connection_link`` before clearing the unique_together
that referenced it. Deleting a model drops its columns anyway, so the field
removals aren't needed — only the constraint has to come down first.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("onboarding", "0001_initial"),
    ]

    operations = [
        migrations.AlterUniqueTogether(name="connectionlinkusage", unique_together=set()),
        migrations.DeleteModel(name="ConnectionLinkUsage"),
        migrations.DeleteModel(name="ConnectionLink"),
    ]
