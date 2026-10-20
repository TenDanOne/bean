"""C2b: composer en 3 pasos (render), bloqueo del servidor, playlists acotadas, orden de medios, Ayuda y ajustes."""

from unittest.mock import MagicMock, patch

from django.urls import reverse

from apps.composer.models import PlatformPost, Post, PostMedia
from apps.composer.tests.test_c2a import C2Base
from apps.members.models import WorkspaceMembership
from apps.organizations.models import Organization
from apps.settings_manager.models import WorkspaceSetting
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


class ComposeRenderTests(C2Base):
    def setUp(self):
        super().setUp()
        self.url = reverse("composer:compose", kwargs={"workspace_id": self.workspace.id})

    def test_three_steps_formats_and_account_names(self):
        html = self.client.get(self.url).content.decode()
        for text in (
            "Paso 1 de 3",
            "Paso 2 de 3",
            "Paso 3 de 3",
            "Reel / Short / TikTok vertical",
            "Texto para Facebook",
        ):
            self.assertIn(text, html)
        self.assertIn('role="tablist"', html)
        self.assertIn('role="tabpanel"', html)
        self.assertIn('id="review-live"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn("aria-required", html)
        # nombre de plataforma para personas, nunca el id interno
        self.assertNotIn(">instagram_login<", html)
        self.assertIn("Publicar ahora", html)
        self.assertIn("publish-action-btn", html)

    def test_calendar_date_opens_schedule_mode_with_timezone(self):
        html = self.client.get(self.url + "?scheduled_date=2099-01-02&scheduled_time=10:00").content.decode()
        self.assertIn("mode: 'schedule'", html)
        self.assertIn('value="2099-01-02"', html)
        self.assertIn(self.workspace.effective_timezone, html)

    def test_plain_open_defaults_to_draft(self):
        self.assertIn("mode: 'draft'", self.client.get(self.url).content.decode())

    def test_template_gallery_lists_format_templates_first(self):
        html = self.client.get(
            reverse("composer:create_landing", kwargs={"workspace_id": self.workspace.id}) + "?tab=templates"
        ).content.decode()
        self.assertLess(
            html.index("Reel / Short / TikTok vertical"),
            html.index("Tip rápido") if "Tip rápido" in html else len(html),
        )

    def test_sidebar_has_single_new_entry_help_and_no_duplicate_idea_link(self):
        html = self.client.get(self.url).content.decode()
        self.assertNotIn("Crear idea</span>", html)
        self.assertIn('href="/ayuda/"', html)
        self.assertIn("Plantillas", html)


class PreviewReviewTests(C2Base):
    def test_preview_returns_review_panel_and_per_account_figures(self):
        resp = self.client.post(
            reverse("composer:preview", kwargs={"workspace_id": self.workspace.id}),
            {
                "caption": "Hola {{tema}}",
                "selected_accounts": f"{self.yt.id},{self.fb.id}",
                f"yt_post_type_{self.yt.id}": "short",
            },
        )
        self.assertContains(resp, f'data-pv-acc="{self.yt.id}"')
        self.assertContains(resp, 'id="review-panel"')
        self.assertContains(resp, "marcadores")
        self.assertContains(resp, "Bloquea")
        self.assertEqual(len(resp.context["review_blocks"]), 2)


class ServerBlockTests(C2Base):
    def stage_video(self, duration=60):
        video = self.asset("v.mp4", "video", duration=duration)
        s = self.client.session
        s[f"pending_media_{self.workspace.id}"] = [str(video.id)]
        s.save()

    def sched(self, **extra):
        return self.schedule(selected_accounts=str(self.yt.id), **extra)

    def test_kids_required_blocks_schedule_but_not_draft(self):
        self.stage_video()
        self.assertEqual(self.sched().status_code, 400)
        ok = self.sched(**{f"yt_made_for_kids_{self.yt.id}": "false"})
        self.assertNotEqual(ok.status_code, 400, ok.content)

    def test_thumbnail_over_2mb_blocks_on_server(self):
        self.stage_video()
        big = self.asset("big.jpg", file_size=3 * 1024 * 1024)
        resp = self.sched(
            **{f"yt_made_for_kids_{self.yt.id}": "false", f"yt_thumbnail_asset_id_{self.yt.id}": str(big.id)}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("2 MB", resp.json()["errors"]["thumbnail"])
        small = self.asset("small.jpg", file_size=1024)
        ok = self.sched(
            **{f"yt_made_for_kids_{self.yt.id}": "false", f"yt_thumbnail_asset_id_{self.yt.id}": str(small.id)}
        )
        self.assertNotEqual(ok.status_code, 400, ok.content)

    def test_duration_blocks_on_server(self):
        video = self.asset("long.mp4", "video", duration=400)
        s = self.client.session
        s[f"pending_media_{self.workspace.id}"] = [str(video.id)]
        s.save()
        resp = self.sched(**{f"yt_made_for_kids_{self.yt.id}": "false", f"yt_post_type_{self.yt.id}": "short"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("3 minutos", resp.json()["errors"]["media"])


class PlaylistEndpointTests(C2Base):
    def url(self, account, workspace=None):
        return reverse(
            "composer:youtube_playlists",
            kwargs={"workspace_id": (workspace or self.workspace).id, "account_id": account.id},
        )

    @patch("providers.get_provider")
    def test_lists_playlists_of_the_channel(self, get_provider):
        get_provider.return_value = MagicMock(list_playlists=MagicMock(return_value=[{"id": "PL1", "title": "Tips"}]))
        resp = self.client.get(self.url(self.yt))
        self.assertEqual(resp.json(), {"playlists": [{"id": "PL1", "title": "Tips"}]})

    @patch("providers.get_provider")
    def test_provider_failure_is_502_not_500(self, get_provider):
        get_provider.return_value = MagicMock(list_playlists=MagicMock(side_effect=RuntimeError("boom")))
        self.assertEqual(self.client.get(self.url(self.yt)).status_code, 502)

    def test_scoped_to_youtube_accounts_of_this_workspace(self):
        self.assertEqual(self.client.get(self.url(self.fb)).status_code, 404)
        other = Workspace.objects.create(organization=Organization.objects.create(name="O2"), name="Otro")
        foreign = SocialAccount.objects.create(
            workspace=other,
            platform="youtube",
            account_platform_id="UCx",
            account_name="Ajeno",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.assertEqual(self.client.get(self.url(foreign)).status_code, 404)
        self.assertIn(self.client.get(self.url(foreign, other)).status_code, (403, 404))

    def test_member_without_channel_access_is_refused(self):
        WorkspaceMembership.objects.filter(user=self.user).update(
            workspace_role=WorkspaceMembership.WorkspaceRole.EDITOR
        )
        self.assertEqual(self.client.get(self.url(self.yt)).status_code, 403)


class ReorderMediaTests(C2Base):
    def test_reorders_post_media(self):
        post = Post.objects.create(workspace=self.workspace, author=self.user, caption="c")
        a, b = self.asset("a.jpg"), self.asset("b.jpg")
        PostMedia.objects.create(post=post, media_asset=a, position=0)
        PostMedia.objects.create(post=post, media_asset=b, position=1)
        url = reverse("composer:reorder_media_post", kwargs={"workspace_id": self.workspace.id, "post_id": post.id})
        resp = self.client.post(url, {"order": f"{b.id},{a.id}"})
        self.assertEqual(resp.status_code, 204)
        self.assertEqual([m.media_asset_id for m in post.media_attachments.all()], [b.id, a.id])

    def test_reorders_pending_media_and_ignores_foreign_ids(self):
        a, b = self.asset("a.jpg"), self.asset("b.jpg")
        s = self.client.session
        s[f"pending_media_{self.workspace.id}"] = [str(a.id), str(b.id)]
        s.save()
        url = reverse("composer:reorder_media", kwargs={"workspace_id": self.workspace.id})
        self.client.post(url, {"order": f"{b.id},00000000-0000-0000-0000-000000000000,{a.id}"})
        self.assertEqual(self.client.session[f"pending_media_{self.workspace.id}"], [str(b.id), str(a.id)])

    def test_media_rows_render_with_asset_ids_for_ordering(self):
        post = Post.objects.create(workspace=self.workspace, author=self.user, caption="c")
        a = self.asset("a.jpg")
        PostMedia.objects.create(post=post, media_asset=a, position=0)
        html = self.client.get(
            reverse("composer:compose_edit", kwargs={"workspace_id": self.workspace.id, "post_id": post.id})
        ).content.decode()
        self.assertIn(f'data-asset-id="{a.id}"', html)
        self.assertIn('data-mv="-1"', html)


class HelpPageTests(C2Base):
    def test_help_page_lists_glossary_and_explains_jargon(self):
        resp = self.client.get(reverse("ayuda"))
        self.assertEqual(resp.status_code, 200)
        for text in ("Mapa de botones", "Hecho para niños", "Stitch", "LUFS", "BEAN publica solo cada 5 minutos"):
            self.assertContains(resp, text)
        self.assertContains(resp, 'id="g-cola"')
        self.assertContains(resp, 'id="gq"')

    def test_help_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("ayuda")).status_code, 302)

    def test_composer_embeds_help_entries_for_the_question_marks(self):
        html = self.client.get(reverse("composer:compose", kwargs={"workspace_id": self.workspace.id})).content.decode()
        self.assertIn('id="help-data"', html)
        self.assertIn('data-help="formato"', html)


class BannedPhrasesSettingTests(C2Base):
    def test_workspace_settings_saves_phrases_used_by_the_reviewer(self):
        url = reverse("workspaces:settings", kwargs={"workspace_id": self.workspace.id})
        self.client.post(
            url,
            {"action": "banned_phrases", "banned_phrases": "puntaje garantizado\n\n Solo hoy \npuntaje garantizado"},
        )
        value = WorkspaceSetting.objects.get(workspace=self.workspace, key="composer.banned_phrases").value
        self.assertEqual(value, ["puntaje garantizado", "Solo hoy"])
        self.assertContains(self.client.get(url), "puntaje garantizado")
        resp = self.client.post(
            reverse("composer:preview", kwargs={"workspace_id": self.workspace.id}),
            {"caption": "Es PUNTAJE GARANTIZADO ya", "selected_accounts": str(self.fb.id)},
        )
        self.assertContains(resp, "Frases prohibidas")

    def test_viewer_cannot_change_phrases(self):
        WorkspaceMembership.objects.filter(user=self.user).update(
            workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER
        )
        url = reverse("workspaces:settings", kwargs={"workspace_id": self.workspace.id})
        self.assertEqual(self.client.post(url, {"action": "banned_phrases", "banned_phrases": "x"}).status_code, 403)


class PlaylistNotificationTests(C2Base):
    def test_playlist_error_does_not_say_thumbnail_was_rejected(self):
        from apps.notifications.models import Notification
        from apps.publisher.engine import PublishEngine
        from providers.youtube import PLAYLIST_ERROR_PREFIX

        post = Post.objects.create(workspace=self.workspace, author=self.user, caption="c")
        pp = PlatformPost.objects.create(
            post=post, social_account=self.yt, platform_extra={"thumbnail_error": f"{PLAYLIST_ERROR_PREFIX} 403"}
        )
        PublishEngine()._notify_thumbnail_error(pp)
        n = Notification.objects.get(user=self.user)
        self.assertNotIn("miniatura", n.body)
        self.assertIn("lista de reproducción", n.body)
        pp.platform_extra = {"thumbnail_error": f"too big | {PLAYLIST_ERROR_PREFIX} 403"}
        Notification.objects.all().delete()
        PublishEngine()._notify_thumbnail_error(pp)
        body = Notification.objects.get(user=self.user).body
        self.assertIn("miniatura", body)
        self.assertIn("lista de reproducción", body)


class ApprovalGuardTests(C2Base):
    def test_submit_for_approval_also_requires_kids_and_valid_chapters(self):
        resp = self.client.post(
            self.save_url,
            {"caption": "c", "action": "submit_for_approval", "selected_accounts": str(self.yt.id)},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("made_for_kids", resp.json()["errors"])
        resp = self.client.post(
            self.save_url,
            {
                "caption": "c",
                "action": "submit_for_approval",
                "selected_accounts": str(self.yt.id),
                f"yt_made_for_kids_{self.yt.id}": "false",
                f"yt_chapters_{self.yt.id}": "00:00 a\n00:05 b",
            },
        )
        self.assertIn("chapters", resp.json()["errors"])

    def test_reorder_without_change_does_not_send_approved_post_back(self):
        post = Post.objects.create(workspace=self.workspace, author=self.user, caption="c")
        a = self.asset("a.jpg")
        PostMedia.objects.create(post=post, media_asset=a, position=0)
        url = reverse("composer:reorder_media_post", kwargs={"workspace_id": self.workspace.id, "post_id": post.id})
        with patch("apps.composer.views._revert_approved_to_review") as revert:
            self.client.post(url, {"order": str(a.id)})
        revert.assert_not_called()
