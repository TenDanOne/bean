"""Five holes found by auditing what each role can actually reach.

Each of these was a mutation a role should not have been able to perform, and
each survived because the check was written somewhere *near* the write rather
than in front of it. They are grouped here rather than scattered across app test
suites because they are one finding — "the view refuses, or it was assumed to" —
and reading them together is what makes the pattern visible.
"""

import uuid

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.composer.models import Post
from apps.media_library.models import MediaAsset
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.workspaces.models import Workspace


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(organization=organization, name="Brand A")


def member(django_user_model, workspace, role, email):
    user = django_user_model.objects.create_user(email=email, password="pw12345!", tos_accepted_at=timezone.now())
    OrgMembership.objects.filter(user=user).exclude(organization=workspace.organization).delete()
    OrgMembership.objects.update_or_create(
        user=user,
        organization=workspace.organization,
        defaults={"org_role": OrgMembership.OrgRole.MEMBER},
    )
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role)
    user.last_workspace_id = workspace.id
    user.save(update_fields=["last_workspace_id"])
    return user


@pytest.mark.django_db
class TestMediaCannotBeDestroyedByAnyone:
    """`remove_pending_media` deleted the asset *outside* the "is it pending?"
    check, with no permission decorator and the error swallowed. Posting any
    asset id in the workspace destroyed it and its files."""

    def _asset(self, workspace, owner):
        return MediaAsset.objects.create(
            workspace=workspace,
            uploaded_by=owner,
            filename="keep.png",
            media_type=MediaAsset.MediaType.IMAGE,
        )

    def test_an_asset_that_was_never_staged_survives(self, client, django_user_model, workspace):
        manager = member(django_user_model, workspace, "manager", "m@example.com")
        asset = self._asset(workspace, manager)
        client.force_login(manager)

        client.post(
            reverse(
                "composer:remove_pending_media",
                kwargs={"workspace_id": workspace.id, "asset_id": asset.id},
            )
        )

        assert MediaAsset.objects.filter(id=asset.id).exists()

    def test_a_viewer_cannot_reach_it_at_all(self, client, django_user_model, workspace):
        owner = member(django_user_model, workspace, "owner", "o@example.com")
        viewer = member(django_user_model, workspace, "viewer", "v@example.com")
        asset = self._asset(workspace, owner)
        client.force_login(viewer)

        response = client.post(
            reverse(
                "composer:remove_pending_media",
                kwargs={"workspace_id": workspace.id, "asset_id": asset.id},
            )
        )

        assert response.status_code == 403
        assert MediaAsset.objects.filter(id=asset.id).exists()


@pytest.mark.django_db
class TestBrandSettingsAreNotOpen:
    """The view checked a role for archive/delete and then *fell through* to
    renaming and the logo with nothing guarding them."""

    def test_a_viewer_cannot_rename_the_brand(self, client, django_user_model, workspace):
        viewer = member(django_user_model, workspace, "viewer", "v@example.com")
        client.force_login(viewer)

        response = client.post(
            reverse("workspaces:settings", kwargs={"workspace_id": workspace.id}),
            {"name": "Renamed By A Viewer"},
        )

        assert response.status_code == 403
        workspace.refresh_from_db()
        assert workspace.name == "Brand A"

    def test_an_editor_cannot_rename_the_brand(self, client, django_user_model, workspace):
        editor = member(django_user_model, workspace, "editor", "e@example.com")
        client.force_login(editor)

        client.post(
            reverse("workspaces:settings", kwargs={"workspace_id": workspace.id}),
            {"name": "Renamed By An Editor"},
        )

        workspace.refresh_from_db()
        assert workspace.name == "Brand A"

    def test_a_manager_can(self, client, django_user_model, workspace):
        """Your call: the manager runs the brand, so its name and logo are
        theirs to change."""
        manager = member(django_user_model, workspace, "manager", "m@example.com")
        client.force_login(manager)

        client.post(
            reverse("workspaces:settings", kwargs={"workspace_id": workspace.id}),
            {"name": "Renamed By A Manager"},
        )

        workspace.refresh_from_db()
        assert workspace.name == "Renamed By A Manager"


@pytest.mark.django_db
class TestDeletingIsNotWeakerThanEditing:
    """`post_delete` required only `create_posts`, so an editor could not open a
    colleague's post but could destroy it from the same menu."""

    def test_an_editor_cannot_delete_someone_elses_post(self, client, django_user_model, workspace):
        author = member(django_user_model, workspace, "editor", "a@example.com")
        other = member(django_user_model, workspace, "editor", "b@example.com")
        post = Post.objects.create(workspace=workspace, author=author, caption="theirs")
        client.force_login(other)

        response = client.post(
            reverse("composer:post_delete", kwargs={"workspace_id": workspace.id, "post_id": post.id})
        )

        assert response.status_code == 403
        assert Post.objects.filter(id=post.id).exists()

    def test_an_editor_can_delete_their_own(self, client, django_user_model, workspace):
        author = member(django_user_model, workspace, "editor", "a@example.com")
        post = Post.objects.create(workspace=workspace, author=author, caption="mine")
        client.force_login(author)

        client.post(reverse("composer:post_delete", kwargs={"workspace_id": workspace.id, "post_id": post.id}))

        assert not Post.objects.filter(id=post.id).exists()

    def test_a_manager_can_delete_anyones(self, client, django_user_model, workspace):
        author = member(django_user_model, workspace, "editor", "a@example.com")
        manager = member(django_user_model, workspace, "manager", "m@example.com")
        post = Post.objects.create(workspace=workspace, author=author, caption="theirs")
        client.force_login(manager)

        client.post(reverse("composer:post_delete", kwargs={"workspace_id": workspace.id, "post_id": post.id}))

        assert not Post.objects.filter(id=post.id).exists()


@pytest.mark.django_db
class TestCommentVisibilityIsNotTakenOnTrust:
    def test_an_unknown_visibility_is_refused(self, client, django_user_model, workspace):
        manager = member(django_user_model, workspace, "manager", "m@example.com")
        post = Post.objects.create(workspace=workspace, author=manager, caption="x")
        client.force_login(manager)

        response = client.post(
            reverse("approvals:add_comment", kwargs={"workspace_id": workspace.id, "post_id": post.id}),
            {"body": "hi", "visibility": "secret"},
        )

        assert response.status_code == 400

    def test_a_client_cannot_write_into_the_internal_channel(self, client, django_user_model, workspace):
        """They would be writing somewhere they cannot read: the internal
        channel is hidden from clients, so the team would take it for a private
        note."""
        from apps.approvals.models import PostComment

        manager = member(django_user_model, workspace, "manager", "m@example.com")
        outsider = member(django_user_model, workspace, "client", "c@example.com")
        post = Post.objects.create(workspace=workspace, author=manager, caption="x")
        client.force_login(outsider)

        client.post(
            reverse("approvals:add_comment", kwargs={"workspace_id": workspace.id, "post_id": post.id}),
            {"body": "hi", "visibility": "internal"},
        )

        comment = PostComment.objects.get(post=post)
        assert comment.visibility == PostComment.Visibility.EXTERNAL


@pytest.mark.django_db
class TestBulkActionsRespectChannelAccess:
    """Reading was scoped by `visible_accounts` and writing was not, so a
    crafted set of ids could archive messages on channels never granted."""

    def test_a_message_on_an_unassigned_channel_is_untouched(self, client, django_user_model, workspace):
        from apps.inbox.models import InboxMessage
        from apps.social_accounts.models import SocialAccount

        editor = member(django_user_model, workspace, "editor", "e@example.com")
        channel = SocialAccount.objects.create(
            workspace=workspace,
            platform="youtube",
            account_handle="not-theirs",
            account_name="Not Theirs",
            account_platform_id="not-theirs",
        )
        message = InboxMessage.objects.create(
            workspace=workspace,
            social_account=channel,
            platform_message_id=str(uuid.uuid4()),
            message_type="comment",
            sender_name="Someone",
            body="hello",
            received_at=timezone.now(),
        )
        client.force_login(editor)

        client.post(
            reverse("inbox:bulk_action", kwargs={"workspace_id": workspace.id}),
            {"message_ids": str(message.id), "action": "archive"},
        )

        message.refresh_from_db()
        assert message.status != InboxMessage.Status.ARCHIVED
