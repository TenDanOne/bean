from django.db import migrations

NOMBRES = {"Unassigned": "Sin asignar", "To Do": "Por hacer", "In Progress": "En proceso", "Done": "Listo"}


def traducir(apps, schema_editor):
    IdeaGroup = apps.get_model("composer", "IdeaGroup")
    for viejo, nuevo in NOMBRES.items():
        IdeaGroup.objects.filter(name=viejo).update(name=nuevo)


def revertir(apps, schema_editor):
    IdeaGroup = apps.get_model("composer", "IdeaGroup")
    for viejo, nuevo in NOMBRES.items():
        IdeaGroup.objects.filter(name=nuevo).update(name=viejo)


class Migration(migrations.Migration):
    dependencies = [("composer", "0022_fix_tiktok_pending_left_scheduled")]

    operations = [migrations.RunPython(traducir, revertir)]
