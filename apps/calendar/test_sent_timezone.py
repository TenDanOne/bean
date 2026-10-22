"""Publicar → Enviadas: la fecha y la hora salen en la zona del espacio (no en UTC) y la insignia no repite el texto."""

from datetime import UTC, datetime

import pytest
from django.urls import reverse

from apps.composer.models import PlatformPost, Post
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@pytest.fixture
def ws(db, organization, org_owner):
    w = Workspace.objects.create(name="TZ WS", organization=organization, timezone="America/Mexico_City")
    WorkspaceMembership.objects.create(user=org_owner, workspace=w, workspace_role="owner")
    return w


@pytest.mark.django_db
def test_sent_tab_shows_workspace_timezone(client, ws, org_owner):
    account = SocialAccount.objects.create(
        workspace=ws,
        platform="youtube",
        account_platform_id="tz",
        account_name="tz",
        oauth_access_token="t",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )
    # 03:34 UTC del 22 de octubre = 21:34 del 21 de octubre en Ciudad de México (UTC-6).
    post = Post.objects.create(
        workspace=ws, author=org_owner, title="T", caption="c", scheduled_at=datetime(2026, 10, 22, 3, 34, tzinfo=UTC)
    )
    PlatformPost.objects.create(post=post, social_account=account, status="publishing")
    client.force_login(org_owner)

    html = client.get(reverse("calendar:publish_tab_sent", kwargs={"workspace_id": ws.id})).content.decode()

    assert "21 oct 2026" in html and "21:34" in html
    assert "22 oct" not in html and "03:34" not in html
    assert "post-chip" not in html  # su ::after repetía «Publicando»


@pytest.mark.django_db
def test_queue_groups_by_workspace_day(client, ws, org_owner):
    account = SocialAccount.objects.create(
        workspace=ws,
        platform="youtube",
        account_platform_id="tzq",
        account_name="tzq",
        oauth_access_token="t",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )
    # 23:30 en Ciudad de México del 21 de octubre = 05:30 UTC del 22.
    post = Post.objects.create(
        workspace=ws, author=org_owner, title="Q", caption="c", scheduled_at=datetime(2026, 10, 22, 5, 30, tzinfo=UTC)
    )
    PlatformPost.objects.create(post=post, social_account=account, status="scheduled")
    client.force_login(org_owner)

    html = client.get(reverse("calendar:publish_tab_queue", kwargs={"workspace_id": ws.id})).content.decode()

    assert "21 de octubre" in html and "23:30" in html
    assert "22 de octubre" not in html
