"""Publicar → Enviadas → «Eliminar fallidos»: solo borra posts cuyas redes fallaron todas y nunca publicaron."""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.composer.models import PlatformPost, Post
from apps.members.models import ChannelAssignment, OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@pytest.fixture
def ws(db, organization, org_owner):
    w = Workspace.objects.create(name="Fail WS", organization=organization, timezone="UTC")
    WorkspaceMembership.objects.create(user=org_owner, workspace=w, workspace_role="owner")
    return w


def _account(ws, uid):
    return SocialAccount.objects.create(
        workspace=ws,
        platform="youtube",
        account_platform_id=uid,
        account_name=uid,
        oauth_access_token="t",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


def _post(ws, user, *statuses_accounts):
    post = Post.objects.create(workspace=ws, author=user, title="P", caption="c")
    for status, account in statuses_accounts:
        PlatformPost.objects.create(post=post, social_account=account, status=status)
    return post


def _delete(client, ws):
    return client.post(reverse("calendar:publish_delete_failed", kwargs={"workspace_id": ws.id}))


@pytest.mark.django_db
def test_deletes_only_pure_failed_and_keeps_published(client, ws, org_owner):
    a, b = _account(ws, "a"), _account(ws, "b")
    pure = _post(ws, org_owner, ("failed", a))
    pure_multi = _post(ws, org_owner, ("failed", a), ("failed", b))
    mixed = _post(ws, org_owner, ("failed", a), ("published", b))
    ok = _post(ws, org_owner, ("published", a))
    sched = _post(ws, org_owner, ("scheduled", a))
    client.force_login(org_owner)

    assert _delete(client, ws).status_code == 204

    assert not Post.objects.filter(id__in=[pure.id, pure_multi.id]).exists()
    assert Post.objects.filter(id__in=[mixed.id, ok.id, sched.id]).count() == 3
    assert PlatformPost.objects.filter(post=mixed).count() == 2


@pytest.mark.django_db
def test_failed_with_published_at_is_kept(client, ws, org_owner):
    post = _post(ws, org_owner, ("failed", _account(ws, "a")))
    PlatformPost.objects.filter(post=post).update(published_at=timezone.now())
    client.force_login(org_owner)

    _delete(client, ws)

    assert Post.objects.filter(id=post.id).exists()


@pytest.mark.django_db
def test_other_workspace_untouched(client, organization, ws, org_owner):
    other = Workspace.objects.create(name="Otro", organization=organization, timezone="UTC")
    foreign = _post(other, org_owner, ("failed", _account(other, "z")))
    client.force_login(org_owner)

    _delete(client, ws)

    assert Post.objects.filter(id=foreign.id).exists()


@pytest.mark.django_db
def test_requires_permission(client, organization, ws, org_owner):
    from apps.accounts.models import User

    viewer = User.objects.create_user(email="v@example.com", password="x-12345678", tos_accepted_at=timezone.now())
    OrgMembership.objects.filter(user=viewer).delete()
    OrgMembership.objects.create(user=viewer, organization=organization, org_role=OrgMembership.OrgRole.MEMBER)
    WorkspaceMembership.objects.create(user=viewer, workspace=ws, workspace_role="viewer")
    post = _post(ws, org_owner, ("failed", _account(ws, "a")))
    client.force_login(viewer)

    assert _delete(client, ws).status_code == 403
    assert Post.objects.filter(id=post.id).exists()


@pytest.mark.django_db
def test_scoped_to_visible_accounts(client, organization, ws, org_owner):
    from apps.accounts.models import User

    editor = User.objects.create_user(email="e@example.com", password="x-12345678", tos_accepted_at=timezone.now())
    OrgMembership.objects.filter(user=editor).delete()
    OrgMembership.objects.create(user=editor, organization=organization, org_role=OrgMembership.OrgRole.MEMBER)
    m = WorkspaceMembership.objects.create(user=editor, workspace=ws, workspace_role="editor")
    seen, hidden = _account(ws, "seen"), _account(ws, "hidden")
    ChannelAssignment.objects.create(membership=m, social_account=seen)
    mine = _post(ws, editor, ("failed", seen))
    not_visible = _post(ws, editor, ("failed", hidden))
    client.force_login(editor)

    assert _delete(client, ws).status_code == 204

    assert not Post.objects.filter(id=mine.id).exists()
    assert Post.objects.filter(id=not_visible.id).exists()


@pytest.mark.django_db
def test_sent_tab_offers_button_with_count(client, ws, org_owner):
    _post(ws, org_owner, ("failed", _account(ws, "a")))
    client.force_login(org_owner)

    html = client.get(reverse("calendar:publish_tab_sent", kwargs={"workspace_id": ws.id})).content.decode()

    assert "Eliminar fallidos (1)" in html
    assert "Se eliminarán 1 publicaciones fallidas. No se puede deshacer." in html


@pytest.mark.django_db
def test_post_that_stopped_failing_before_delete_survives(client, ws, org_owner, monkeypatch):
    from apps.calendar import views

    post = _post(ws, org_owner, ("failed", _account(ws, "a")))
    real = views._pure_failed_posts
    calls = []

    def fake(request, workspace):
        calls.append(1)
        if len(calls) == 1:  # el primer listado lo ve fallido; luego pasa a scheduled
            found = real(request, workspace)
            PlatformPost.objects.filter(post=post).update(status="scheduled")
            return found
        return real(request, workspace)

    monkeypatch.setattr(views, "_pure_failed_posts", fake)
    client.force_login(org_owner)

    _delete(client, ws)

    assert Post.objects.filter(id=post.id).exists()


@pytest.mark.django_db
def test_post_delete_refuses_protected_platform_post(client, ws, org_owner):
    a = _account(ws, "a")
    post = _post(ws, org_owner, ("published", a))
    client.force_login(org_owner)

    r = client.post(
        reverse("composer:post_delete", kwargs={"workspace_id": ws.id, "post_id": post.id}) + f"?account={a.id}"
    )

    assert r.status_code == 409
    assert PlatformPost.objects.filter(post=post).exists()


@pytest.mark.django_db
def test_month_thumbnails_load_in_one_query(ws, org_owner, organization, django_assert_num_queries):
    from apps.calendar.views import _preload_thumbnails
    from apps.media_library.models import MediaAsset

    asset = MediaAsset.objects.create(
        workspace=ws, organization=organization, filename="t.jpg", file="media_library/t.jpg", media_type="image"
    )
    a = _account(ws, "a")
    pps = []
    for _ in range(3):
        post = _post(ws, org_owner, ("failed", a))
        pp = post.platform_posts.get()
        pp.platform_extra = {"thumbnail_asset_id": str(asset.id)}
        pp.save()
        pps.append(PlatformPost.objects.select_related("post").get(id=pp.id))

    with django_assert_num_queries(1):
        _preload_thumbnails(pps)
        assert all(pp.display_thumbnail_url.endswith("t.jpg") for pp in pps)
