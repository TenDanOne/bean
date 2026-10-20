"""C1: arreglos de publicación (miniaturas, plantillas, marcadores, duración, vista previa, motor)."""

import io
from unittest.mock import MagicMock, patch

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from django.urls import reverse
from PIL import Image

from apps.composer.models import ContentCategory, PlatformPost, Post, PostMedia, PostTemplate
from apps.composer.services import media_duration_notice, media_error_for_platform
from apps.composer.tests.test_lote_a1 import Base
from apps.media_library.models import MediaAsset
from apps.notifications.models import Notification
from apps.publisher.engine import PublishEngine
from apps.social_accounts.models import SocialAccount


def _image_bytes(size, fmt="PNG"):
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, fmt)
    return buf.getvalue()


class DurationRulesTests(SimpleTestCase):
    def test_short_and_reel_limits(self):
        self.assertIn("3 minutos", media_error_for_platform("youtube", ["video"], "short", [181]))
        self.assertIsNone(media_error_for_platform("youtube", ["video"], "short", [180]))
        self.assertIn("entre 3 y 90", media_error_for_platform("instagram", ["video"], "reel", [91]))
        self.assertIn("entre 3 y 90", media_error_for_platform("instagram", ["video"], "reel", [2]))
        self.assertIsNone(media_error_for_platform("instagram", ["video"], "reel", [30]))

    def test_unknown_duration_gives_notice_not_error(self):
        self.assertIsNone(media_error_for_platform("instagram", ["video"], "reel", [0]))
        self.assertIn("se validará", media_duration_notice("reel", [0]))
        self.assertIsNone(media_duration_notice("reel", [30]))
        self.assertIsNone(media_duration_notice(None, [0]))


class C1Base(Base):
    def setUp(self):
        super().setUp()
        self.yt = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="youtube",
            account_platform_id="UC1",
            account_name="Canal",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )

    def asset(self, name="a.jpg", media_type="image", **kw):
        return MediaAsset.objects.create(
            workspace=self.workspace,
            organization=self.org,
            filename=name,
            file=f"media_library/{name}",
            media_type=media_type,
            uploaded_by=self.user,
            **kw,
        )


class ThumbnailUploadTests(C1Base):
    def setUp(self):
        super().setUp()
        self.url = reverse("composer:thumbnail_upload", kwargs={"workspace_id": self.workspace.id})

    def test_big_image_is_resized_to_1280x720_jpeg(self):
        f = SimpleUploadedFile("big.png", _image_bytes((2560, 1440)), content_type="image/png")
        resp = self.client.post(self.url, {"file": f})
        self.assertEqual(resp.status_code, 200, resp.content)
        asset = MediaAsset.objects.get(id=resp.json()["asset_id"])
        self.assertEqual(asset.mime_type, "image/jpeg")
        self.assertLessEqual(asset.file_size, 2 * 1024 * 1024)
        asset.file.open("rb")
        w, h = Image.open(asset.file).size
        self.assertLessEqual(w, 1280)
        self.assertLessEqual(h, 720)

    def test_small_image_is_kept_as_is(self):
        data = _image_bytes((640, 360))
        resp = self.client.post(self.url, {"file": SimpleUploadedFile("s.png", data, content_type="image/png")})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(MediaAsset.objects.get(id=resp.json()["asset_id"]).file_size, len(data))

    def test_spoofed_and_non_jpg_png_are_rejected(self):
        html = SimpleUploadedFile("x.jpg", b"<html>nope</html>", content_type="image/jpeg")
        self.assertEqual(self.client.post(self.url, {"file": html}).status_code, 400)
        gif = SimpleUploadedFile("x.gif", b"GIF89a" + b"0" * 30, content_type="image/gif")
        resp = self.client.post(self.url, {"file": gif})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("JPG o PNG", resp.json()["error"])
        self.assertFalse(MediaAsset.objects.exists())


class DisplayThumbnailTests(C1Base):
    def test_platform_post_and_post_prefer_the_chosen_thumbnail(self):
        thumb = self.asset("thumb.jpg")
        video = self.asset("v.mp4", "video")
        post = Post.objects.create(workspace=self.workspace, author=self.user, caption="c")
        PostMedia.objects.create(post=post, media_asset=video, position=0)
        pp = PlatformPost.objects.create(
            post=post, social_account=self.yt, platform_extra={"thumbnail_asset_id": str(thumb.id)}
        )
        self.assertEqual(pp.display_thumbnail_url, thumb.file.url)
        self.assertEqual(post.display_thumbnail_url, thumb.file.url)

    def test_empty_when_none_or_malformed(self):
        post = Post.objects.create(workspace=self.workspace, author=self.user, caption="c")
        pp = PlatformPost.objects.create(post=post, social_account=self.yt, platform_extra={})
        self.assertEqual(pp.display_thumbnail_url, "")
        pp.platform_extra = {"thumbnail_asset_id": "not-a-uuid"}
        self.assertEqual(pp.display_thumbnail_url, "")
        self.assertEqual(post.display_thumbnail_url, "")

    def test_queue_list_renders_with_chosen_thumbnail(self):
        thumb = self.asset("thumb.jpg")
        video = self.asset("v.mp4", "video")
        post = Post.objects.create(workspace=self.workspace, author=self.user, caption="c")
        PostMedia.objects.create(post=post, media_asset=video, position=0)
        PlatformPost.objects.create(
            post=post,
            social_account=self.yt,
            status="draft",
            platform_extra={"thumbnail_asset_id": str(thumb.id)},
        )
        resp = self.client.get(reverse("calendar:publish_tab_drafts", kwargs={"workspace_id": self.workspace.id}))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, thumb.file.url)


class PreviewC1Tests(C1Base):
    def setUp(self):
        super().setUp()
        self.url = reverse("composer:preview", kwargs={"workspace_id": self.workspace.id})

    def test_post_type_reaches_the_preview_and_short_warns_about_thumbnail(self):
        thumb = self.asset("t.jpg")
        resp = self.client.post(
            self.url,
            {
                "caption": "C",
                "selected_accounts": str(self.yt.id),
                f"yt_post_type_{self.yt.id}": "short",
                f"yt_thumbnail_asset_id_{self.yt.id}": str(thumb.id),
            },
        )
        self.assertEqual(resp.context["previews"][0]["post_type"], "short")
        self.assertContains(resp, "no aplica miniaturas personalizadas a los Shorts")

    def test_pending_media_keep_attach_order_and_duration_notice(self):
        first = self.asset("b.mp4", "video", duration=0)
        second = self.asset("a.mp4", "video", duration=0)
        s = self.client.session
        s[f"pending_media_{self.workspace.id}"] = [str(first.id), str(second.id)]
        s.save()
        ig = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="instagram",
            account_platform_id="ig1",
            account_name="IG",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        resp = self.client.post(
            self.url,
            {"caption": "C", "selected_accounts": str(ig.id), f"ig_post_type_{ig.id}": "reel"},
        )
        self.assertEqual([m["filename"] for m in resp.context["media_items"]], ["b.mp4", "a.mp4"])
        self.assertContains(resp, "se validará al procesarlo")

    def test_known_bad_duration_is_flagged(self):
        long_video = self.asset("l.mp4", "video", duration=400)
        s = self.client.session
        s[f"pending_media_{self.workspace.id}"] = [str(long_video.id)]
        s.save()
        resp = self.client.post(
            self.url,
            {"caption": "C", "selected_accounts": str(self.yt.id), f"yt_post_type_{self.yt.id}": "short"},
        )
        self.assertContains(resp, "3 minutos o menos")


class PlaceholderGuardTests(C1Base):
    def test_schedule_with_unfilled_marker_is_blocked_but_draft_is_allowed(self):
        resp = self.schedule(caption="Hola {{tema}}")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("marcadores", resp.json()["errors"]["placeholders"])
        self.assertFalse(Post.objects.exists())
        draft = self.client.post(
            self.save_url, {"caption": "Hola {{tema}}", "action": "save_draft", "selected_accounts": str(self.fb.id)}
        )
        self.assertIn(draft.status_code, (200, 204, 302))
        self.assertEqual(Post.objects.count(), 1)

    def test_marker_in_override_blocks_publish_now(self):
        resp = self.client.post(
            self.save_url,
            {
                "caption": "ok",
                "action": "publish_now",
                "selected_accounts": str(self.fb.id),
                f"override_caption_{self.fb.id}": "{{ pendiente }}",
            },
        )
        self.assertEqual(resp.status_code, 400)


class TemplateRoundTripTests(C1Base):
    def test_save_as_template_keeps_title_extras_and_overrides_then_compose_applies_them(self):
        cat = ContentCategory.objects.create(workspace=self.workspace, name="Edu")
        thumb = self.asset("t.jpg")
        post = Post.objects.create(
            workspace=self.workspace, author=self.user, title="Titulo", caption="Cuerpo", tags=["uno"], category=cat
        )
        PlatformPost.objects.create(
            post=post,
            social_account=self.yt,
            platform_specific_caption="Solo YT",
            platform_extra={
                "privacy_status": "unlisted",
                "tags": ["a"],
                "thumbnail_asset_id": str(thumb.id),
                "id": "resultado-de-publicacion",
            },
        )
        resp = self.client.post(
            reverse("composer:save_as_template", kwargs={"workspace_id": self.workspace.id, "post_id": post.id}),
            {"template_name": "Mi plantilla"},
        )
        self.assertIn(resp.status_code, (200, 204, 302))
        tpl = PostTemplate.objects.get()
        data = tpl.template_data
        self.assertEqual(data["title"], "Titulo")
        yt_id = str(self.yt.id)
        self.assertEqual(data["platform_extras"][yt_id]["privacy_status"], "unlisted")
        self.assertNotIn("id", data["platform_extras"][yt_id])
        self.assertEqual(data["platform_overrides"][yt_id]["caption"], "Solo YT")

        resp = self.client.get(
            reverse("composer:compose", kwargs={"workspace_id": self.workspace.id}),
            {"template": str(tpl.id)},
        )
        ctx = resp.context
        self.assertEqual(ctx["form"].initial["title"], "Titulo")
        self.assertEqual(ctx["initial_tags"], ["uno"])
        self.assertEqual(ctx["initial_category_id"], str(cat.id))
        self.assertEqual(ctx["platform_extras"][yt_id]["privacy_status"], "unlisted")
        self.assertEqual(ctx["platform_extras"][yt_id]["thumbnail_url"], thumb.file.url)
        self.assertEqual(ctx["platform_overrides"][yt_id]["caption"], "Solo YT")


class EngineC1Tests(C1Base):
    def _post(self, **extra):
        video = MediaAsset(
            workspace=self.workspace,
            organization=self.org,
            filename="v.mp4",
            media_type="video",
            uploaded_by=self.user,
        )
        video.file.save("v.mp4", ContentFile(b"x" * 10), save=True)
        post = Post.objects.create(workspace=self.workspace, author=self.user, caption="c", tags=["interna"])
        PostMedia.objects.create(post=post, media_asset=video, position=0)
        return PlatformPost.objects.create(post=post, social_account=self.yt, status="publishing", platform_extra=extra)

    def _dispatch(self, pp):
        provider = MagicMock()
        provider.supported_post_types = []
        provider.publish_post.return_value = MagicMock(platform_post_id="v", url=None, extra={}, pending=False)
        with (
            patch("apps.publisher.engine._resolve_publish_credentials", return_value={}),
            patch("apps.publisher.engine.get_provider", return_value=provider),
            patch.object(SocialAccount, "get_fresh_access_token", return_value="tok"),
        ):
            PublishEngine()._dispatch_to_provider(pp)
        return provider.publish_post.call_args.args[1].extra

    def test_internal_post_tags_are_not_sent_as_public_tags(self):
        self.assertEqual(self._dispatch(self._post(tags=["publica"]))["tags"], ["publica"])
        self.assertNotIn("tags", self._dispatch(self._post()))

    def test_thumbnail_rejection_keeps_published_and_notifies(self):
        pp = self._post()
        result = {
            "success": True,
            "platform_post_id": "vid",
            "url": None,
            "response": {"id": "vid", "thumbnail_error": "YouTube dijo no"},
            "pending": False,
        }
        with patch.object(PublishEngine, "_dispatch_to_provider", return_value=result):
            PublishEngine()._publish_platform_post(pp)
        pp.refresh_from_db()
        self.assertEqual(pp.status, PlatformPost.Status.PUBLISHED)
        self.assertEqual(pp.platform_extra["thumbnail_error"], "YouTube dijo no")
        self.assertTrue(
            Notification.objects.filter(
                user=self.user, body__contains="YouTube rechazó la miniatura: YouTube dijo no"
            ).exists()
        )


class ThumbnailTenancyTests(C1Base):
    def setUp(self):
        super().setUp()
        from apps.organizations.models import Organization
        from apps.workspaces.models import Workspace

        other_org = Organization.objects.create(name="Otra")
        other_ws = Workspace.objects.create(organization=other_org, name="Ajeno")
        self.foreign = MediaAsset.objects.create(
            workspace=other_ws,
            organization=other_org,
            filename="f.jpg",
            file="media_library/f.jpg",
            media_type="image",
        )

    def test_save_ignores_foreign_thumbnail_id_and_keeps_own(self):
        own = self.asset("own.jpg")
        data = {"caption": "c", "selected_accounts": str(self.yt.id)}
        self.client.post(self.save_url, {**data, f"yt_thumbnail_asset_id_{self.yt.id}": str(self.foreign.id)})
        self.assertIsNone(PlatformPost.objects.get().platform_extra["thumbnail_asset_id"])
        PlatformPost.objects.all().delete()
        self.client.post(self.save_url, {**data, f"yt_thumbnail_asset_id_{self.yt.id}": str(own.id)})
        self.assertEqual(PlatformPost.objects.get().platform_extra["thumbnail_asset_id"], str(own.id))

    def test_template_with_foreign_thumbnail_is_dropped(self):
        tpl = PostTemplate.objects.create(
            workspace=self.workspace,
            name="t",
            template_data={
                "caption": "x",
                "platform_account_ids": [str(self.yt.id)],
                "platform_extras": {str(self.yt.id): {"thumbnail_asset_id": str(self.foreign.id)}},
            },
        )
        resp = self.client.get(
            reverse("composer:compose", kwargs={"workspace_id": self.workspace.id}), {"template": str(tpl.id)}
        )
        extra = resp.context["platform_extras"][str(self.yt.id)]
        self.assertIsNone(extra["thumbnail_asset_id"])
        self.assertNotIn("thumbnail_url", extra)

    def test_engine_does_not_fetch_foreign_thumbnail(self):
        foreign = self.foreign
        foreign.file.save("f.jpg", ContentFile(b"\xff\xd8\xff secret"), save=True)
        pp = EngineC1Tests._post(self, thumbnail_asset_id=str(foreign.id))
        extra = EngineC1Tests._dispatch(self, pp)
        self.assertNotIn("thumbnail_file", extra)


class ThumbnailBombTests(C1Base):
    def test_oversized_pixel_count_is_rejected_before_decoding(self):
        data = _image_bytes((10, 10))
        f = SimpleUploadedFile("b.png", data, content_type="image/png")
        with patch("apps.composer.views._THUMBNAIL_MAX_PIXELS", 50):
            resp = self.client.post(
                reverse("composer:thumbnail_upload", kwargs={"workspace_id": self.workspace.id}), {"file": f}
            )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("megapíxeles", resp.json()["error"])
