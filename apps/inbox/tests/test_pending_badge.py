# ruff: noqa: F401, F811
"""Insignia de pendientes por contestar (misma cuenta que «No contestados») y avisos agrupados por cuenta."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.inbox.panel import pending_count
from apps.inbox.tasks import InboxSyncEngine
from apps.notifications.models import EventType, Notification
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

from .test_inbox_ui import counts, fb, feed, ig, make_account, msg, operator, workspace


def badge_url(workspace):
    return reverse("inbox:pending_badge", kwargs={"workspace_id": workspace.id})


@pytest.mark.django_db
class TestPendingCount:
    def test_matches_no_contestados(self, client, operator, workspace, fb, ig):
        msg(workspace, fb, "c1", post="p1")
        msg(workspace, ig, "c2", post="p2")
        msg(workspace, fb, "c3", post="p1", awaiting=False)
        msg(workspace, fb, "d1", kind="dm", convo="t1", minutes_ago=30)
        msg(workspace, fb, "d2", kind="dm", convo="t1", minutes_ago=20)
        client.force_login(operator)
        tabs = counts(client.get(feed(workspace)))
        assert pending_count(workspace.id, SocialAccount.objects.filter(workspace=workspace)) == tabs["todo"] == 3

    def test_respects_tenancy(self, workspace, fb, organization):
        other = Workspace.objects.create(organization=organization, name="Otro")
        msg(other, make_account(other, "facebook", "ajeno"), "x1")
        msg(workspace, fb, "c1")
        assert pending_count(workspace.id, SocialAccount.objects.all()) == 1

    def test_endpoint_returns_badge_and_count(self, client, operator, workspace, fb):
        client.force_login(operator)
        assert 'data-count="0"' in client.get(badge_url(workspace)).content.decode()
        msg(workspace, fb, "c1")
        html = client.get(badge_url(workspace)).content.decode()
        assert 'data-count="1"' in html and "sidebar-badge" in html

    def test_endpoint_needs_login(self, client, workspace):
        assert client.get(badge_url(workspace)).status_code in (302, 403)


def _incoming(mid, sender=None, minutes_ago=1):
    return SimpleNamespace(
        platform_message_id=mid,
        sender_id=sender or "u-" + mid,
        sender_name="Ana",
        text="hola",
        message_type="comment",
        timestamp=timezone.now() - timedelta(minutes=minutes_ago),
        extra={},
    )


@pytest.mark.django_db
class TestGroupedNotifications:
    def avisos(self, user):
        return Notification.objects.filter(user=user, event_type=EventType.NEW_INBOX_MESSAGE)

    def test_one_per_account_and_updates(self, operator, workspace, fb, ig):
        engine = InboxSyncEngine()
        for mid in ("a1", "a2", "a3"):
            engine._upsert_message(fb, _incoming(mid))
        engine._upsert_message(ig, _incoming("b1"))
        assert self.avisos(operator).count() == 2
        grouped = self.avisos(operator).get(data__group_account=str(fb.id))
        assert grouped.title.startswith("3 mensajes nuevos en ")
        assert f"account={fb.id}" in grouped.data["action_url"]
        assert not grouped.is_read

    def test_read_one_starts_a_new_group(self, operator, workspace, fb):
        engine = InboxSyncEngine()
        engine._upsert_message(fb, _incoming("a1"))
        self.avisos(operator).update(is_read=True)
        engine._upsert_message(fb, _incoming("a2"))
        assert self.avisos(operator).count() == 2

    def test_own_and_answered_messages_do_not_notify(self, operator, workspace, fb):
        engine = InboxSyncEngine()
        engine._upsert_message(fb, _incoming("o1", sender=fb.account_platform_id))
        assert not self.avisos(operator).exists()
        # Un mensaje directo que llega después de que ya contestamos esa conversación tampoco avisa.
        reply = _incoming("o2", sender=fb.account_platform_id, minutes_ago=0)
        reply.message_type, reply.extra = "dm", {"conversation_id": "t1"}
        engine._upsert_message(fb, reply)
        old = _incoming("old", minutes_ago=30)
        old.message_type, old.extra = "dm", {"conversation_id": "t1"}
        engine._upsert_message(fb, old)
        assert not self.avisos(operator).exists()


@pytest.mark.django_db
def test_inbox_live_outside_sidebar_link(client, operator, workspace):
    client.force_login(operator)
    html = client.get(reverse("calendar:calendar", kwargs={"workspace_id": workspace.id})).content.decode()
    live = html.index('id="inbox-live"')
    link_start = html.rindex("<a ", 0, live)
    assert "</a>" in html[link_start:live]  # el enlace de la Bandeja ya cerró antes del aviso de voz
