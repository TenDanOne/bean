from pathlib import Path

from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory
from django.urls import reverse

from apps.composer.views import preview
from apps.social_accounts.models import SocialAccount

from .test_unsplash import ComposerTestCase


class ComposerPreviewTests(ComposerTestCase):
    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        self.preview_url = reverse("composer:preview", kwargs={"workspace_id": self.workspace.id})
        self.account = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="facebook",
            account_platform_id="facebook-page-1",
            account_name="Facebook Page",
        )

    def _preview_post(self, data):
        request = self.factory.post(self.preview_url, data)
        request.user = self.user
        SessionMiddleware(lambda req: None).process_request(request)
        request.session.save()
        return preview(request, self.workspace.id)

    def test_compose_page_posts_live_preview_form_data(self):
        template = Path("templates/composer/compose.html").read_text()

        self.assertIn("hx-post=\"{% url 'composer:preview' workspace_id=workspace.id %}\"", template)
        self.assertNotIn("hx-get=\"{% url 'composer:preview' workspace_id=workspace.id %}\"", template)

    def test_preview_accepts_large_caption_in_post_body(self):
        caption = "Long preview caption. " * 300

        response = self._preview_post(
            {
                "title": "Preview title",
                "caption": caption,
                "selected_accounts": str(self.account.id),
            },
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Facebook Page", content)
        self.assertIn("6600/63206", content)
        self.assertIn("Long preview caption. Long preview caption.", content)

    def test_preview_rejects_get_requests(self):
        response = self.client.get(
            self.preview_url,
            {
                "caption": "This belongs in the request body.",
                "selected_accounts": str(self.account.id),
            },
        )

        self.assertEqual(response.status_code, 405)


class YouTubeThumbnailPreviewTests(ComposerTestCase):
    """The custom thumbnail is what viewers see in the feed, so the preview has to
    show it. It used to render a frame of the video instead, leaving the uploaded
    thumbnail visible only inside its own uploader box."""

    def setUp(self):
        super().setUp()
        from apps.media_library.models import MediaAsset

        self.yt = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="youtube",
            account_platform_id="UC-thumb",
            account_name="Channel",
        )
        self.asset = MediaAsset.objects.create(
            workspace=self.workspace,
            filename="thumb.jpg",
            file="media_library/thumb.jpg",
            media_type="image",
            uploaded_by=self.user,
        )
        self.url = reverse("composer:preview", kwargs={"workspace_id": self.workspace.id})

    def _post(self, **extra):
        data = {"title": "T", "caption": "C", "selected_accounts": str(self.yt.id)}
        data.update(extra)
        return self.client.post(self.url, data)

    def test_chosen_thumbnail_reaches_the_preview(self):
        response = self._post(**{f"yt_thumbnail_asset_id_{self.yt.id}": str(self.asset.id)})

        self.assertEqual(response.context["previews"][0]["thumbnail_url"], self.asset.file.url)
        self.assertContains(response, "Custom thumbnail")

    def test_no_thumbnail_leaves_the_video_frame_in_place(self):
        response = self._post()

        self.assertEqual(response.context["previews"][0]["thumbnail_url"], "")
        self.assertNotContains(response, "Custom thumbnail")

    def test_malformed_id_does_not_500_the_live_preview(self):
        """This runs on every keystroke; a half-typed value must not break it."""
        response = self._post(**{f"yt_thumbnail_asset_id_{self.yt.id}": "not-a-uuid"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["previews"][0]["thumbnail_url"], "")

    def test_asset_from_another_workspace_is_ignored(self):
        from apps.media_library.models import MediaAsset
        from apps.organizations.models import Organization
        from apps.workspaces.models import Workspace

        other_ws = Workspace.objects.create(organization=Organization.objects.create(name="Other"), name="Other WS")
        foreign = MediaAsset.objects.create(
            workspace=other_ws, filename="x.jpg", file="media_library/x.jpg", media_type="image"
        )

        response = self._post(**{f"yt_thumbnail_asset_id_{self.yt.id}": str(foreign.id)})

        self.assertEqual(response.context["previews"][0]["thumbnail_url"], "")
