from django.db import migrations


def rename_default_org(apps, schema_editor):
    Organization = apps.get_model("organizations", "Organization")
    Organization.objects.filter(name="My Organization").update(name="Mi organización")


class Migration(migrations.Migration):
    dependencies = [
        ("organizations", "0003_remove_organization_billing_email"),
    ]

    operations = [
        migrations.RunPython(rename_default_org, migrations.RunPython.noop),
    ]
