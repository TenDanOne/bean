"""Seguridad del login: Google no hereda una cuenta local sin verificar; contraseña y borrado."""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import OrgMembership


@pytest.mark.django_db
def test_social_user_sets_password_without_current(client):
    u = User.objects.create_user(email="g@example.com", password="x-tmp-12345", tos_accepted_at=timezone.now())
    u.set_unusable_password()
    u.save()
    assert not u.has_usable_password()
    client.force_login(u)

    client.post(
        reverse("accounts:settings"),
        {"action": "update_password", "password": "nueva-clave-1", "password_confirm": "nueva-clave-1"},
    )

    u.refresh_from_db()
    assert u.check_password("nueva-clave-1")


@pytest.mark.django_db
def test_password_user_still_needs_current(client):
    u = User.objects.create_user(email="p@example.com", password="vieja-clave-1", tos_accepted_at=timezone.now())
    client.force_login(u)

    client.post(
        reverse("accounts:settings"),
        {"action": "update_password", "password": "nueva-clave-1", "password_confirm": "nueva-clave-1"},
    )

    u.refresh_from_db()
    assert u.check_password("vieja-clave-1")


@pytest.mark.django_db
def test_delete_account_requires_typed_email(client):
    u = User.objects.create_user(email="borrar@example.com", password="clave-12345", tos_accepted_at=timezone.now())
    OrgMembership.objects.filter(user=u).delete()  # el alta crea una org de la que sería único dueño
    client.force_login(u)

    client.post(reverse("accounts:settings"), {"action": "delete_account"})
    assert User.objects.filter(id=u.id).exists()

    client.post(reverse("accounts:settings"), {"action": "delete_account", "confirm_email": "Borrar@example.com"})
    assert not User.objects.filter(id=u.id).exists()
