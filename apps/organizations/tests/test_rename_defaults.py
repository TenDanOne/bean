from importlib import import_module

import pytest
from django.apps import apps
from django.template import Context, Template

from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


@pytest.mark.django_db
def test_rename_solo_nombres_exactos():
    org_mig = import_module("apps.organizations.migrations.0004_rename_default_org_name")
    ws_mig = import_module("apps.workspaces.migrations.0004_rename_default_workspace_name")
    a = Organization.objects.create(name="My Organization")
    b = Organization.objects.create(name="My Organization 2")
    wa = Workspace.objects.create(organization=a, name="My Workspace")
    wb = Workspace.objects.create(organization=a, name="my workspace")

    org_mig.rename_default_org(apps, None)
    ws_mig.rename_default_workspace(apps, None)

    for obj in (a, b, wa, wb):
        obj.refresh_from_db()
    assert a.name == "Mi organización"
    assert b.name == "My Organization 2"
    assert wa.name == "Mi espacio de trabajo"
    assert wb.name == "my workspace"


@pytest.mark.parametrize(("n", "esperado"), [(1, "1 notificación"), (2, "2 notificaciones")])
def test_plural_notificacion(n, esperado):
    t = Template('{{ n }} notificaci{{ n|pluralize:"ón,ones" }}')
    assert t.render(Context({"n": n})) == esperado
