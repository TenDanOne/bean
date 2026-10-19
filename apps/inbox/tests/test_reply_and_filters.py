"""Regressions for the inbox telling the operator things that weren't true.

Two independent failures produced the same class of lie:

* a reply that the provider rejected was still stored and rendered as sent;
* touching any filter emptied the bandeja, because the "All …" sentinel was
  submitted as an empty string and filtered on verbatim.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.inbox.models import InboxMessage, InboxReply
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(name="Inbox WS", organization=organization)


@pytest.fixture
def operator(db, user, organization, workspace):
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="owner")
    return user


@pytest.fixture
def account(db, workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="youtube",
        account_platform_id="UC-test",
        account_name="Test channel",
        oauth_access_token="token",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


@pytest.fixture
def message(db, workspace, account):
    return InboxMessage.objects.create(
        workspace=workspace,
        social_account=account,
        platform_message_id="comment-1",
        sender_name="Viewer",
        body="Great video",
        message_type="comment",
        received_at=timezone.now(),
    )


@pytest.mark.django_db
class TestSendReplyFailures:
    def _url(self, workspace, message):
        return reverse("inbox:send_reply", kwargs={"workspace_id": workspace.id, "message_id": message.id})

    def test_provider_error_does_not_record_a_reply(self, client, operator, workspace, message):
        """The whole bug: a rejected reply used to be stored with an empty
        platform_reply_id and rendered in the thread as if it had been sent."""
        client.force_login(operator)
        with patch("apps.inbox.views.get_provider") as get_provider:
            get_provider.return_value.reply_to_message.side_effect = RuntimeError("401 Invalid Credentials")
            response = client.post(self._url(workspace, message), {"body": "saludos!"})

        assert response.status_code == 422
        assert not InboxReply.objects.filter(inbox_message=message).exists()

    def test_unsupported_platform_does_not_record_a_reply(self, client, operator, workspace, message):
        client.force_login(operator)
        with patch("apps.inbox.views.get_provider") as get_provider:
            get_provider.return_value.reply_to_message.side_effect = NotImplementedError
            response = client.post(self._url(workspace, message), {"body": "saludos!"})

        assert response.status_code == 422
        assert not InboxReply.objects.filter(inbox_message=message).exists()

    def test_network_error_does_not_record_a_reply(self, client, operator, workspace, message):
        client.force_login(operator)
        with patch("apps.inbox.views.get_provider") as get_provider:
            get_provider.return_value.reply_to_message.side_effect = TimeoutError
            response = client.post(self._url(workspace, message), {"body": "saludos!"})

        assert response.status_code == 422
        assert not InboxReply.objects.filter(inbox_message=message).exists()

    def test_successful_reply_is_recorded_with_the_platform_id(self, client, operator, workspace, message):
        client.force_login(operator)
        with patch("apps.inbox.views.get_provider") as get_provider:
            get_provider.return_value.reply_to_message.return_value.platform_message_id = "yt-reply-1"
            response = client.post(self._url(workspace, message), {"body": "saludos!"})

        assert response.status_code == 200
        reply = InboxReply.objects.get(inbox_message=message)
        assert reply.platform_reply_id == "yt-reply-1"
        assert reply.author == operator

    def test_reply_uses_a_freshly_refreshed_token(self, client, operator, workspace, message, account):
        """Replies used to read oauth_access_token directly and 401 on a stale one."""
        client.force_login(operator)
        with patch("apps.inbox.views.get_provider") as get_provider:
            get_provider.return_value.reply_to_message.return_value.platform_message_id = "yt-reply-1"
            with patch.object(SocialAccount, "get_fresh_access_token", return_value="fresh") as fresh:
                client.post(self._url(workspace, message), {"body": "hola"})

        fresh.assert_called_once()
        assert get_provider.return_value.reply_to_message.call_args.kwargs["access_token"] == "fresh"


@pytest.mark.django_db
def _listed(response):
    """Messages on the page, whichever depth it is drawing.

    A plain browse and most filters show publication folders; search and the
    queue tabs show a flat list. Tests about *filtering* should not care which,
    so they ask this instead of reaching for one context key.
    """
    if response.context.get("showing_folders"):
        return [m for folder in response.context["folders"] for m in folder["messages"]]
    return list(response.context["inbox_messages"])


@pytest.mark.django_db
class TestHistoryFeedRestoreRendersTheFullPage:
    """Pressing back/forward over htmx-swapped history restores a *whole page*,
    and the feed only offered a fragment — leaving the page torn apart."""

    def _url(self, workspace):
        return reverse("inbox:feed", kwargs={"workspace_id": workspace.id})

    def test_history_restore_returns_the_page_not_the_swap_fragment(self, client, operator, workspace, message):
        client.force_login(operator)

        response = client.get(
            self._url(workspace),
            HTTP_HX_REQUEST="true",
            HTTP_HX_HISTORY_RESTORE_REQUEST="true",
        )

        assert response.status_code == 200
        assert any(template.name == "inbox/feed.html" for template in response.templates)

    def test_an_ordinary_htmx_request_still_returns_the_fragment(self, client, operator, workspace, message):
        """The swap path is unchanged — only the history restore differs."""
        client.force_login(operator)

        response = client.get(self._url(workspace), HTTP_HX_REQUEST="true")

        assert response.status_code == 200
        names = {template.name for template in response.templates}
        assert "inbox/partials/_main.html" in names
        assert "inbox/feed.html" not in names


@pytest.mark.django_db
class TestFreshTokenHelper:
    def _account(self, workspace, *, expires_in_minutes):
        return SocialAccount.objects.create(
            workspace=workspace,
            platform="youtube",
            account_platform_id="UC-token",
            account_name="Token test",
            oauth_access_token="stale",
            oauth_refresh_token="refresh",
            token_expires_at=timezone.now() + timedelta(minutes=expires_in_minutes),
        )

    def test_refreshes_a_nearly_expired_token(self, workspace):
        account = self._account(workspace, expires_in_minutes=1)
        provider = object()

        with patch.object(SocialAccount, "refresh_oauth_token", return_value="fresh") as refresh:
            assert account.get_fresh_access_token(provider) == "fresh"
        refresh.assert_called_once_with(provider)

    def test_refreshes_an_already_expired_token(self, workspace):
        account = self._account(workspace, expires_in_minutes=-30)

        with patch.object(SocialAccount, "refresh_oauth_token", return_value="fresh"):
            assert account.get_fresh_access_token(object()) == "fresh"

    def test_leaves_a_healthy_token_alone(self, workspace):
        account = self._account(workspace, expires_in_minutes=55)

        with patch.object(SocialAccount, "refresh_oauth_token") as refresh:
            assert account.get_fresh_access_token(object()) == "stale"
        refresh.assert_not_called()

    def test_refresh_failure_degrades_to_the_existing_token(self, workspace):
        """The real API error should surface at the call site, not a refresh traceback."""
        account = self._account(workspace, expires_in_minutes=1)

        with patch.object(SocialAccount, "refresh_oauth_token", side_effect=RuntimeError("boom")):
            assert account.get_fresh_access_token(object()) == "stale"

    def test_no_refresh_token_means_no_attempt(self, workspace):
        account = self._account(workspace, expires_in_minutes=1)
        account.oauth_refresh_token = ""

        with patch.object(SocialAccount, "refresh_oauth_token") as refresh:
            assert account.get_fresh_access_token(object()) == "stale"
        refresh.assert_not_called()
