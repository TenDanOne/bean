"""Filtro `ago`: tiempo relativo corto con espacios («hace 3 h, 24 min»)."""

from datetime import timedelta

from django.utils import timezone

from apps.inbox.templatetags.inbox_extras import ago


def test_ago_formats():
    now = timezone.now()
    assert ago(now - timedelta(hours=3, minutes=24, seconds=5)) == "hace 3 h, 24 min"
    assert ago(now - timedelta(hours=3, seconds=5)) == "hace 3 h"
    assert ago(now - timedelta(minutes=12, seconds=5)) == "hace 12 min"
    assert ago(now - timedelta(days=2, hours=5)) == "hace 2 d"
    assert ago(None) == ""
