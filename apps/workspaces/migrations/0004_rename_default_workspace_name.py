from django.db import migrations


def rename_default_workspace(apps, schema_editor):
    Workspace = apps.get_model("workspaces", "Workspace")
    Workspace.objects.filter(name="My Workspace").update(name="Mi espacio de trabajo")


class Migration(migrations.Migration):
    dependencies = [
        ("workspaces", "0003_alter_workspace_primary_color_and_more"),
    ]

    operations = [
        migrations.RunPython(rename_default_workspace, migrations.RunPython.noop),
    ]
