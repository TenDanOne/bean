"""Analytics is gated on the ``view_analytics`` permission key.

Until this existed, every analytics view checked only "are you a member of this
workspace" — so `contributor`, the one role the permission matrix explicitly
denies analytics to, read every metric anyway. The permission had been declared
for a long time; nothing enforced it.
"""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.members.models import OrgMembership, WorkspaceMembership


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(organization=organization, name="Brand A")


def _user(django_user_model, email):
    # tos_accepted_at matters: TosAcceptanceMiddleware redirects everyone who
    # hasn't accepted, which would turn every assertion here into a 302 and hide
    # whatever the permission layer actually did.
    return django_user_model.objects.create_user(email=email, password="pw12345!", tos_accepted_at=timezone.now())


def _member(django_user_model, workspace, role, email):
    user = _user(django_user_model, email)
    OrgMembership.objects.create(user=user, organization=workspace.organization, org_role=OrgMembership.OrgRole.MEMBER)
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role)
    return user


@pytest.mark.django_db
class TestAnalyticsPermission:
    @pytest.mark.parametrize("role", ["owner", "editor", "viewer"])
    def test_roles_granted_analytics_can_open_the_page(self, client, django_user_model, workspace, role):
        user = _member(django_user_model, workspace, role, f"{role}@example.com")
        client.force_login(user)
        response = client.get(reverse("analytics:index", kwargs={"workspace_id": workspace.id}))
        assert response.status_code == 200

    def test_contributor_is_refused(self, client, django_user_model, workspace):
        """The regression this file exists for."""
        user = _member(django_user_model, workspace, "contributor", "contributor@example.com")
        client.force_login(user)
        response = client.get(reverse("analytics:index", kwargs={"workspace_id": workspace.id}))
        assert response.status_code == 403

    def test_a_stranger_is_refused(self, client, django_user_model, workspace):
        user = _user(django_user_model, "nobody@example.com")
        client.force_login(user)
        response = client.get(reverse("analytics:index", kwargs={"workspace_id": workspace.id}))
        assert response.status_code == 403

    def test_every_analytics_view_is_decorated(self):
        """A new analytics view must not ship undecorated.

        Adding one is easy; remembering the decorator is not, and the failure is
        silent — the page just works for someone who should not see it.
        """
        import inspect

        from apps.analytics import views

        undecorated = []
        for name, fn in inspect.getmembers(views, inspect.isfunction):
            if name.startswith("_") or fn.__module__ != views.__name__:
                continue
            source = inspect.getsource(fn)
            # The decorator wraps, so __wrapped__ chains are the reliable signal;
            # fall back to reading the source for the unwrapped case.
            if "require_permission" not in source and not hasattr(fn, "__wrapped__"):
                undecorated.append(name)
        assert not undecorated, f"analytics views with no permission check: {undecorated}"
