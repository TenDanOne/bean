"""drain_pending_tasks, the 'process pending' view and POST /cron/run (no 24/7 worker)."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from background_task import background
from background_task.models import Task
from django.urls import reverse

from apps.common.background import drain_pending_tasks
from apps.members.models import OrgMembership

RAN: list[str] = []


@background(schedule=0)
def drain_probe(label):
    RAN.append(label)


@pytest.fixture(autouse=True)
def _clear(db):
    RAN.clear()
    Task.objects.all().delete()  # migrate registers the recurring jobs; isolate from them


@pytest.mark.django_db
class TestDrain:
    def test_runs_due_task_not_future_one(self):
        drain_probe("due")
        drain_probe("later", schedule=timedelta(hours=1))

        assert drain_pending_tasks() == 1
        assert RAN == ["due"]
        assert Task.objects.filter(task_params__contains="later").exists()

    def test_respects_max_seconds(self):
        drain_probe("a")
        drain_probe("b")
        # monotonic: deadline calc, first check (ok), second check (past deadline)
        with patch("apps.common.background.time.monotonic", side_effect=[0, 1, 100]):
            assert drain_pending_tasks(max_seconds=10) == 1
        assert len(RAN) == 1


@pytest.fixture
def org_member(client, user):
    # User creation already attaches an org membership (owner); demote it.
    OrgMembership.objects.filter(user=user).update(org_role="member")
    client.force_login(user)
    return client


@pytest.mark.django_db
class TestProcessPendingView:
    def test_member_forbidden(self, org_member):
        assert org_member.post(reverse("process_pending")).status_code == 403

    def test_owner_gets_toast(self, client, org_owner):
        drain_probe("x")
        client.force_login(org_owner)
        response = client.post(reverse("process_pending"))
        assert response.status_code == 204
        assert "Se procesaron 1 tareas" in response["HX-Trigger"]
        assert RAN == ["x"]

    def test_get_not_allowed(self, client, org_owner):
        client.force_login(org_owner)
        assert client.get(reverse("process_pending")).status_code == 405


@pytest.mark.django_db
class TestCronRun:
    def test_503_without_secret(self, client, settings):
        settings.CRON_SECRET = ""
        assert client.post(reverse("cron_run")).status_code == 503

    def test_401_bad_bearer(self, client, settings):
        settings.CRON_SECRET = "s3cret"
        response = client.post(reverse("cron_run"), headers={"Authorization": "Bearer nope"})
        assert response.status_code == 401
        assert client.post(reverse("cron_run")).status_code == 401

    def test_200_good_bearer(self, client, settings):
        settings.CRON_SECRET = "s3cret"
        drain_probe("cron")
        response = client.post(reverse("cron_run"), headers={"Authorization": "Bearer s3cret"})
        assert response.status_code == 200
        assert response.json() == {"ran": 1, "pending": 0}
        assert RAN == ["cron"]

    def test_csrf_not_required(self, settings):
        from django.test import Client

        settings.CRON_SECRET = "s3cret"
        c = Client(enforce_csrf_checks=True)
        assert c.post(reverse("cron_run"), headers={"Authorization": "Bearer s3cret"}).status_code == 200
