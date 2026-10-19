"""Tipos de publicación (YouTube Short, IG Reel/Story/Carrusel) y borrar publicación publicada."""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.urls import reverse
from django.utils import timezone

from apps.composer.models import PlatformPost, Post
from apps.composer.services import media_error_for_platform
from apps.composer.tests.test_lote_a1 import Base
from apps.members.models import WorkspaceMembership
from apps.publisher.engine import PublishEngine
from apps.social_accounts.models import SocialAccount
from providers.types import PostType


class MediaRulesTests(SimpleTestCase):
    def test_reel_needs_video_story_accepts_image_carousel_needs_two(self):
        self.assertIn("Reel", media_error_for_platform("instagram", ["image"], "reel"))
        self.assertIsNone(media_error_for_platform("instagram", ["video"], "reel"))
        self.assertIsNone(media_error_for_platform("instagram", ["image"], "story"))
        self.assertIn("carrusel", media_error_for_platform("instagram", ["image"], "carousel"))
        self.assertIsNone(media_error_for_platform("instagram", ["image", "video"], "carousel"))
        self.assertIn("video", media_error_for_platform("youtube", ["image"], "short"))

    def test_engine_respects_hint(self):
        r = PublishEngine._resolve_post_type
        self.assertEqual(r("youtube", {"post_type": "short"}, 1, "video"), PostType.SHORT)
        self.assertEqual(r("instagram", {"post_type": "story"}, 1, "image"), PostType.STORY)
        self.assertEqual(r("instagram", {"post_type": "reel"}, 1, "video"), PostType.REEL)
        self.assertEqual(r("instagram", {"post_type": "carousel"}, 2, "image"), PostType.CAROUSEL)


class SavePostTypeTests(Base):
    def setUp(self):
        super().setUp()
        self.ig = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="instagram",
            account_platform_id="ig1",
            account_name="IG",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )

    def test_ig_type_saved_and_label_shown(self):
        self.client.post(
            self.save_url,
            {"caption": "c", "selected_accounts": str(self.ig.id), f"ig_post_type_{self.ig.id}": "reel"},
        )
        pp = PlatformPost.objects.get()
        self.assertEqual(pp.platform_extra["post_type"], "reel")
        self.assertEqual(pp.post_type_label, "Reel")

    def test_feed_clears_type_and_invalid_ignored(self):
        data = {"caption": "c", "selected_accounts": str(self.ig.id), f"ig_post_type_{self.ig.id}": "story"}
        self.client.post(self.save_url, data)
        pp = PlatformPost.objects.get()
        edit = reverse("composer:save_post_edit", kwargs={"workspace_id": self.workspace.id, "post_id": pp.post_id})
        self.client.post(edit, {**data, f"ig_post_type_{self.ig.id}": "feed"})
        pp.refresh_from_db()
        self.assertNotIn("post_type", pp.platform_extra)

    def test_reel_without_video_blocked_on_schedule(self):
        resp = self.client.post(
            self.save_url,
            {
                "caption": "c",
                "action": "schedule",
                "selected_accounts": str(self.ig.id),
                f"ig_post_type_{self.ig.id}": "reel",
                "scheduled_date": (timezone.now() + timezone.timedelta(days=2)).strftime("%Y-%m-%d"),
                "scheduled_time": "10:00",
            },
        )
        self.assertEqual(resp.status_code, 400)


class DeletePublishedTests(Base):
    def make_pp(self, platform, platform_post_id="x1"):
        acc = SocialAccount.objects.create(
            workspace=self.workspace,
            platform=platform,
            account_platform_id=f"{platform}-1",
            account_name=platform,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        post = Post.objects.create(workspace=self.workspace, author=self.user, caption="c")
        pp = PlatformPost.objects.create(
            post=post, social_account=acc, status=PlatformPost.Status.PUBLISHED, platform_post_id=platform_post_id
        )
        url = reverse(
            "composer:delete_published_post",
            kwargs={"workspace_id": self.workspace.id, "post_id": post.id, "platform_post_id": pp.id},
        )
        return pp, url

    @patch("providers.get_provider")
    def test_delete_calls_provider_and_marks(self, get_provider):
        provider = MagicMock()
        get_provider.return_value = provider
        pp, url = self.make_pp("youtube")
        resp = self.client.post(url, {"confirm": "BORRAR"})
        self.assertEqual(resp.status_code, 200)
        provider.delete_post.assert_called_once()
        self.assertEqual(provider.delete_post.call_args.args[1], "x1")
        pp.refresh_from_db()
        self.assertIn("deleted_on_platform_at", pp.platform_extra)
        self.assertContains(resp, "Borrada en la plataforma")

    @patch("providers.get_provider")
    def test_requires_typed_confirmation(self, get_provider):
        pp, url = self.make_pp("facebook")
        self.assertEqual(self.client.post(url, {"confirm": "borrar x"}).status_code, 400)
        get_provider.return_value.delete_post.assert_not_called()

    def test_tiktok_refused(self):
        pp, url = self.make_pp("tiktok")
        self.assertEqual(self.client.post(url, {"confirm": "BORRAR"}).status_code, 400)

    @patch("providers.get_provider")
    def test_provider_error_is_spanish_400_and_not_marked(self, get_provider):
        get_provider.return_value.delete_post.side_effect = RuntimeError("boom")
        pp, url = self.make_pp("facebook")
        resp = self.client.post(url, {"confirm": "BORRAR"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("No se pudo borrar", resp.content.decode())
        pp.refresh_from_db()
        self.assertNotIn("deleted_on_platform_at", pp.platform_extra or {})

    @patch("providers.get_provider")
    def test_pp_of_another_workspace_is_404(self, get_provider):
        from apps.workspaces.models import Workspace

        pp, url = self.make_pp("facebook")
        other = Workspace.objects.create(organization=self.org, name="Otro")
        Post.objects.filter(id=pp.post_id).update(workspace=other)  # the URL still names our workspace
        self.assertEqual(self.client.post(url, {"confirm": "BORRAR"}).status_code, 404)
        get_provider.return_value.delete_post.assert_not_called()

    def test_unpublished_pp_is_400(self):
        pp, url = self.make_pp("facebook")
        PlatformPost.objects.filter(id=pp.id).update(status=PlatformPost.Status.SCHEDULED)
        self.assertEqual(self.client.post(url, {"confirm": "BORRAR"}).status_code, 400)

    @patch("providers.get_provider")
    def test_custom_role_only_deletes_on_channels_it_sees(self, get_provider):
        from apps.members.models import BUILTIN_ROLE_PERMISSIONS, CustomRole

        pp, url = self.make_pp("facebook")
        role = CustomRole.objects.create(
            organization=self.org,
            name="Borrador",
            permissions={**BUILTIN_ROLE_PERMISSIONS["editor"], "delete_published_posts": True},
        )
        WorkspaceMembership.objects.filter(user=self.user).update(
            workspace_role=WorkspaceMembership.WorkspaceRole.EDITOR, custom_role=role
        )
        self.assertEqual(self.client.post(url, {"confirm": "BORRAR"}).status_code, 404)  # channel not assigned
        get_provider.return_value.delete_post.assert_not_called()

    def test_non_owner_without_permission_is_403(self):
        pp, url = self.make_pp("facebook")
        WorkspaceMembership.objects.filter(user=self.user).update(
            workspace_role=WorkspaceMembership.WorkspaceRole.EDITOR
        )
        self.assertEqual(self.client.post(url, {"confirm": "BORRAR"}).status_code, 403)

    def test_compose_shows_delete_and_tiktok_note(self):
        pp, _ = self.make_pp("facebook")
        resp = self.client.get(
            reverse("composer:compose_edit", kwargs={"workspace_id": self.workspace.id, "post_id": pp.post_id})
        )
        self.assertContains(resp, "Borrar publicación")

    def test_tiktok_row_shows_note_without_button(self):
        from django.template.loader import render_to_string

        pp, _ = self.make_pp("tiktok")
        html = render_to_string("composer/partials/_published_target.html", {"workspace": self.workspace, "pp": pp})
        self.assertIn("TikTok no permite borrar por API", html)
        self.assertNotIn("Borrar publicación", html)
