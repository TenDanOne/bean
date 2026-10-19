"""Arrastrar en el calendario: conserva la hora, rechaza el pasado y respeta la aprobación; multi-espacio respeta canales."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.composer.models import PlatformPost, Post
from apps.members.models import ChannelAssignment, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@pytest.fixture
def ws(db, organization, org_owner):
    w = Workspace.objects.create(name="Cal WS", organization=organization, timezone="UTC")
    WorkspaceMembership.objects.create(user=org_owner, workspace=w, workspace_role="owner")
    return w


def _account(ws, uid, name):
    return SocialAccount.objects.create(
        workspace=ws,
        platform="youtube",
        account_platform_id=uid,
        account_name=name,
        oauth_access_token="t",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


def _pp(ws, user, account, status, at):
    post = Post.objects.create(workspace=ws, author=user, title="P", caption="c")
    return PlatformPost.objects.create(post=post, social_account=account, status=status, scheduled_at=at)


def _drag(client, ws, pp, when, **extra):
    return client.post(
        reverse("calendar:reschedule", kwargs={"workspace_id": ws.id}),
        {"platform_post_id": str(pp.id), "new_datetime": when, **extra},
    )


@pytest.mark.django_db
def test_month_drag_keeps_original_time(client, ws, org_owner):
    orig = (timezone.now() + timedelta(days=3)).replace(hour=17, minute=30, second=0, microsecond=0)
    pp = _pp(ws, org_owner, _account(ws, "u1", "A"), "scheduled", orig)
    client.force_login(org_owner)
    target = (orig + timedelta(days=2)).date().isoformat()

    r = _drag(client, ws, pp, f"{target}T09:00:00", keep_time="1")

    assert r.status_code == 204
    pp.refresh_from_db()
    assert (pp.scheduled_at.date(), pp.scheduled_at.hour, pp.scheduled_at.minute) == (
        orig.date() + timedelta(days=2),
        17,
        30,
    )


@pytest.mark.django_db
def test_drag_to_past_is_rejected(client, ws, org_owner):
    orig = timezone.now() + timedelta(days=3)
    pp = _pp(ws, org_owner, _account(ws, "u2", "B"), "scheduled", orig)
    client.force_login(org_owner)
    past = (timezone.now() - timedelta(days=2)).date().isoformat()

    r = _drag(client, ws, pp, f"{past}T09:00:00")

    assert r.status_code == 400
    pp.refresh_from_db()
    assert pp.scheduled_at == orig


@pytest.mark.django_db
def test_drag_of_draft_respects_required_approval(client, ws, org_owner):
    ws.approval_workflow_mode = "required_internal"
    ws.save()
    pp = _pp(ws, org_owner, _account(ws, "u3", "C"), "draft", None)
    client.force_login(org_owner)
    future = (timezone.now() + timedelta(days=2)).date().isoformat()

    r = _drag(client, ws, pp, f"{future}T09:00:00")

    assert r.status_code == 400
    pp.refresh_from_db()
    assert pp.status == "draft"


@pytest.mark.django_db
def test_cross_calendar_respects_visible_accounts(client, organization, ws, org_owner):
    from apps.accounts.models import User

    other = User.objects.create_user(email="editor@example.com", password="x-12345678", tos_accepted_at=timezone.now())
    from apps.members.models import OrgMembership

    OrgMembership.objects.filter(user=other).delete()
    OrgMembership.objects.create(user=other, organization=organization, org_role=OrgMembership.OrgRole.MEMBER)
    m = WorkspaceMembership.objects.create(user=other, workspace=ws, workspace_role="editor")
    seen = _account(ws, "u4", "CanalVisible")
    hidden = _account(ws, "u5", "CanalOculto")
    ChannelAssignment.objects.create(membership=m, social_account=seen)
    when = timezone.now()
    _pp(ws, org_owner, seen, "scheduled", when).post.title = "x"
    hidden_pp = _pp(ws, org_owner, hidden, "scheduled", when)
    hidden_pp.post.title = "TituloOculto"
    hidden_pp.post.save()
    client.force_login(other)

    html = client.get(reverse("organizations:cross_workspace_calendar")).content.decode()

    assert "CanalVisible" in html
    assert "CanalOculto" not in html
    assert "TituloOculto" not in html
