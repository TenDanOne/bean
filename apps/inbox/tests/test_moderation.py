"""Hiding and deleting comments on the platform.

Both were checkboxes for months with nothing behind them: the provider adapters
carried working implementations and no view ever called them, so ticking
"Delete comments" granted a permission over an action that did not exist.

Two actions, not one with a flag. Hiding is reversible and the platform keeps
the comment; deleting is permanent. The tests keep them apart for the same
reason the interface does.
"""

from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.inbox.models import InboxMessage
from apps.members.models import ChannelAssignment, OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from providers.exceptions import APIError, RateLimitError


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(organization=organization, name="Brand A")


@pytest.fixture
def channel(workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="youtube",
        account_handle="chan",
        account_name="Chan",
        account_platform_id="chan",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


def member(django_user_model, workspace, role, email):
    user = django_user_model.objects.create_user(email=email, password="pw12345!", tos_accepted_at=timezone.now())
    OrgMembership.objects.filter(user=user).exclude(organization=workspace.organization).delete()
    OrgMembership.objects.update_or_create(
        user=user,
        organization=workspace.organization,
        defaults={"org_role": OrgMembership.OrgRole.MEMBER},
    )
    membership = WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role)
    return user, membership


def comment(workspace, channel, *, outbound=False, platform_id="c1"):
    return InboxMessage.objects.create(
        workspace=workspace,
        social_account=channel,
        platform_message_id=platform_id,
        message_type="comment",
        sender_name="Someone",
        body="hello",
        is_outbound=outbound,
        received_at=timezone.now(),
    )


def hide_url(workspace, message):
    return reverse("inbox:toggle_hidden", kwargs={"workspace_id": workspace.id, "message_id": message.id})


def delete_url(workspace, message):
    return reverse("inbox:delete_comment", kwargs={"workspace_id": workspace.id, "message_id": message.id})


class FakeProvider:
    """Records what would have gone to the platform."""

    def __init__(self, *, fail=None):
        self.fail = fail
        self.visibility_calls = []
        self.delete_calls = []

    def set_comment_visibility(self, access_token, comment_id, visible):
        if self.fail:
            raise self.fail
        self.visibility_calls.append((comment_id, visible))

    def delete_comment(self, access_token, comment_id):
        if self.fail:
            raise self.fail
        self.delete_calls.append(comment_id)


def patched(provider):
    """Patch out everything between the view and the network."""
    return (
        patch("apps.inbox.views.get_provider", return_value=provider),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value={}),
        patch.object(SocialAccount, "get_fresh_access_token", return_value="tok"),
    )


def run_with(provider, fn):
    a, b, c = patched(provider)
    with a, b, c:
        return fn()


@pytest.mark.django_db
class TestHiding:
    def test_an_editor_with_the_grant_can_hide(self, client, django_user_model, workspace, channel):
        """Hiding is reversible, so it belongs to whoever works the inbox."""
        user, membership = member(django_user_model, workspace, "editor", "e@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel, can_moderate=True)
        message = comment(workspace, channel)
        client.force_login(user)
        provider = FakeProvider()

        response = run_with(provider, lambda: client.post(hide_url(workspace, message)))

        assert response.status_code == 200
        assert provider.visibility_calls == [("c1", False)]
        message.refresh_from_db()
        assert message.is_hidden is True
        assert message.moderated_by == user

    def test_hiding_again_unhides(self, client, django_user_model, workspace, channel):
        user, membership = member(django_user_model, workspace, "editor", "e@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel, can_moderate=True)
        message = comment(workspace, channel)
        message.is_hidden = True
        message.save(update_fields=["is_hidden"])
        client.force_login(user)
        provider = FakeProvider()

        run_with(provider, lambda: client.post(hide_url(workspace, message)))

        assert provider.visibility_calls == [("c1", True)]
        message.refresh_from_db()
        assert message.is_hidden is False

    def test_without_the_channel_grant_it_is_refused(self, client, django_user_model, workspace, channel):
        """The role allows moderating; this channel was never handed over."""
        user, membership = member(django_user_model, workspace, "editor", "e@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel, can_reply=True)
        message = comment(workspace, channel)
        client.force_login(user)
        provider = FakeProvider()

        response = run_with(provider, lambda: client.post(hide_url(workspace, message)))

        assert response.status_code == 403
        assert provider.visibility_calls == []
        message.refresh_from_db()
        assert message.is_hidden is False

    def test_a_viewer_is_refused_by_their_role(self, client, django_user_model, workspace, channel):
        """Even holding the flag: both layers have to pass, and a viewer changes
        nothing."""
        user, membership = member(django_user_model, workspace, "viewer", "v@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel, can_moderate=True)
        message = comment(workspace, channel)
        client.force_login(user)

        response = run_with(FakeProvider(), lambda: client.post(hide_url(workspace, message)))

        assert response.status_code == 403

    def test_nothing_is_stored_when_the_platform_refuses(self, client, django_user_model, workspace, channel):
        """Recording it first would show the comment as hidden here while it
        stayed visible to everyone else — the one thing this must never do."""
        user, membership = member(django_user_model, workspace, "editor", "e@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel, can_moderate=True)
        message = comment(workspace, channel)
        client.force_login(user)

        response = run_with(
            FakeProvider(fail=APIError("nope")),
            lambda: client.post(hide_url(workspace, message)),
        )

        assert response.status_code == 422
        message.refresh_from_db()
        assert message.is_hidden is False

    def test_exhausted_quota_says_so(self, client, django_user_model, workspace, channel):
        user, membership = member(django_user_model, workspace, "editor", "e@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel, can_moderate=True)
        message = comment(workspace, channel)
        client.force_login(user)

        response = run_with(
            FakeProvider(fail=RateLimitError("slow down")),
            lambda: client.post(hide_url(workspace, message)),
        )

        assert response.status_code == 422
        assert b"cuota" in response.content.lower()


@pytest.mark.django_db
class TestDeleting:
    def test_a_manager_can_delete_our_own_comment(self, client, django_user_model, workspace, channel):
        user, membership = member(django_user_model, workspace, "manager", "m@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel, can_delete_comments=True)
        message = comment(workspace, channel, outbound=True)
        client.force_login(user)
        provider = FakeProvider()

        response = run_with(provider, lambda: client.post(delete_url(workspace, message)))

        assert response.status_code == 200
        assert provider.delete_calls == ["c1"]
        message.refresh_from_db()
        assert message.deleted_on_platform is not None

    def test_the_row_survives_the_deletion(self, client, django_user_model, workspace, channel):
        """Dropping our record too would erase the thread we answered and who
        did what, along with the comment."""
        user, membership = member(django_user_model, workspace, "manager", "m@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel, can_delete_comments=True)
        message = comment(workspace, channel, outbound=True)
        client.force_login(user)

        run_with(FakeProvider(), lambda: client.post(delete_url(workspace, message)))

        assert InboxMessage.objects.filter(id=message.id).exists()

    def test_someone_elses_comment_can_be_deleted(self, client, django_user_model, workspace, channel):
        """A channel owner may delete comments on their own videos.

        This used to be refused up front on `is_outbound`, which was both wrong
        about the platform and unreachable: the feed excludes outbound messages,
        so the panel could only ever be opened on someone else's comment — the
        exact case the check rejected. The button existed and could not appear.
        """
        user, membership = member(django_user_model, workspace, "manager", "m@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel, can_delete_comments=True)
        message = comment(workspace, channel, outbound=False)
        client.force_login(user)
        provider = FakeProvider()

        response = run_with(provider, lambda: client.post(delete_url(workspace, message)))

        assert response.status_code == 200
        assert provider.delete_calls == ["c1"]

    def test_a_platform_refusal_names_the_reversible_alternative(self, client, django_user_model, workspace, channel):
        """If a platform really will not delete someone else's comment, the
        answer is "hide it instead", not a raw API error."""
        user, membership = member(django_user_model, workspace, "manager", "m@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel, can_delete_comments=True)
        message = comment(workspace, channel)
        client.force_login(user)

        response = run_with(
            FakeProvider(fail=APIError("forbidden")),
            lambda: client.post(delete_url(workspace, message)),
        )

        assert response.status_code == 422
        assert b"Intenta ocultarlo" in response.content
        message.refresh_from_db()
        assert message.deleted_on_platform is None

    def test_a_video_notification_is_not_moderatable(self, client, django_user_model, workspace, channel):
        """An older webhook filed published-video announcements as comments.
        Hiding one would post an Atom entry id to setModerationStatus."""
        user, membership = member(django_user_model, workspace, "manager", "m@example.com")
        ChannelAssignment.objects.create(
            membership=membership, social_account=channel, can_delete_comments=True, can_moderate=True
        )
        notification = comment(workspace, channel, platform_id="yt:video:abc123")
        assert notification.is_moderatable is False
        client.force_login(user)
        provider = FakeProvider()

        response = run_with(provider, lambda: client.post(hide_url(workspace, notification)))

        assert response.status_code == 422
        assert provider.visibility_calls == []

    def test_an_editor_cannot_delete_even_with_the_flag(self, client, django_user_model, workspace, channel):
        """Deleting is permanent, so it stays at manager and above however the
        channel boxes are set."""
        user, membership = member(django_user_model, workspace, "editor", "e@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel, can_delete_comments=True)
        message = comment(workspace, channel, outbound=True)
        client.force_login(user)

        response = run_with(FakeProvider(), lambda: client.post(delete_url(workspace, message)))

        assert response.status_code == 403

    def test_deleting_twice_is_refused(self, client, django_user_model, workspace, channel):
        user, membership = member(django_user_model, workspace, "manager", "m@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel, can_delete_comments=True)
        message = comment(workspace, channel, outbound=True)
        message.deleted_on_platform = timezone.now()
        message.save(update_fields=["deleted_on_platform"])
        client.force_login(user)
        provider = FakeProvider()

        response = run_with(provider, lambda: client.post(delete_url(workspace, message)))

        assert response.status_code == 422
        assert provider.delete_calls == []


@pytest.mark.django_db
class TestTheButtonsAreActuallyOnScreen:
    """The whole point of the report that started this: the control existed in
    the codebase and could not be reached in the product."""

    def _panel(self, client, workspace, message):
        return client.get(
            reverse("inbox:message_detail", kwargs={"workspace_id": workspace.id, "message_id": message.id})
        ).content.decode()

    def test_both_buttons_render_on_an_ordinary_comment(self, client, django_user_model, workspace, channel):
        user, membership = member(django_user_model, workspace, "manager", "m@example.com")
        ChannelAssignment.objects.create(
            membership=membership, social_account=channel, can_moderate=True, can_delete_comments=True
        )
        message = comment(workspace, channel)
        client.force_login(user)

        body = self._panel(client, workspace, message)

        assert ">Ocultar" in body or "Ocultar\n" in body
        assert "Eliminar" in body
        assert reverse("inbox:delete_comment", kwargs={"workspace_id": workspace.id, "message_id": message.id}) in body

    def test_the_delete_button_says_delete_not_just_an_icon(self, client, django_user_model, workspace, channel):
        user, membership = member(django_user_model, workspace, "manager", "m@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel, can_delete_comments=True)
        message = comment(workspace, channel)
        client.force_login(user)

        body = self._panel(client, workspace, message)

        assert "Eliminar" in body

    def test_without_the_grant_the_buttons_stay_and_explain(self, client, django_user_model, workspace, channel):
        """Hiding the control teaches people the product cannot do it."""
        user, membership = member(django_user_model, workspace, "editor", "e@example.com")
        ChannelAssignment.objects.create(membership=membership, social_account=channel, can_reply=True)
        message = comment(workspace, channel)
        client.force_login(user)

        body = self._panel(client, workspace, message)

        assert "Eliminar" in body
        assert "disabled" in body

    def test_a_video_notification_shows_no_moderation_at_all(self, client, django_user_model, workspace, channel):
        user, membership = member(django_user_model, workspace, "manager", "m@example.com")
        ChannelAssignment.objects.create(
            membership=membership, social_account=channel, can_moderate=True, can_delete_comments=True
        )
        notification = comment(workspace, channel, platform_id="yt:video:abc123")
        client.force_login(user)

        body = self._panel(client, workspace, notification)

        assert (
            reverse("inbox:delete_comment", kwargs={"workspace_id": workspace.id, "message_id": notification.id})
            not in body
        )


@pytest.mark.django_db
class TestTheCapabilityIsOfferedToTheRightRoles:
    """The dialog must not offer a box the server will refuse anyway."""

    def test_an_editor_is_offered_moderate_but_not_delete(self, django_user_model, workspace):
        from apps.members.models import capabilities_available_to

        _, membership = member(django_user_model, workspace, "editor", "e@example.com")
        offered = {key for key, _label, _help in capabilities_available_to(membership)}
        assert "can_moderate" in offered
        assert "can_delete_comments" not in offered

    def test_a_manager_is_offered_both(self, django_user_model, workspace):
        from apps.members.models import capabilities_available_to

        _, membership = member(django_user_model, workspace, "manager", "m@example.com")
        offered = {key for key, _label, _help in capabilities_available_to(membership)}
        assert {"can_moderate", "can_delete_comments"} <= offered

    def test_a_viewer_is_offered_metrics_only(self, django_user_model, workspace):
        from apps.members.models import capabilities_available_to

        _, membership = member(django_user_model, workspace, "viewer", "v@example.com")
        offered = {key for key, _label, _help in capabilities_available_to(membership)}
        assert offered == {"can_view_metrics"}
