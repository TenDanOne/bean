"""Modo inicial del paso 3 del composer."""

from datetime import timedelta
from types import SimpleNamespace

from django.test import RequestFactory
from django.utils import timezone

from apps.composer.views import _default_mode

rf = RequestFactory()


def _post(delta):
    return SimpleNamespace(scheduled_at=timezone.now() + delta)


def test_future_schedule_opens_in_schedule():
    assert _default_mode(rf.get("/"), _post(timedelta(hours=2)), False, True, True) == "schedule"


def test_past_schedule_from_a_clone_opens_in_publish_now():
    assert _default_mode(rf.get("/"), _post(-timedelta(hours=2)), False, True, True) == "now"


def test_past_schedule_without_publish_permission_opens_as_draft():
    assert _default_mode(rf.get("/"), _post(-timedelta(hours=2)), False, True, False) == "draft"
