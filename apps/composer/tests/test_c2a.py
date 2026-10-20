"""C2a: revisor de calidad, persistencia de campos nuevos, «Hecho para niños» obligatorio y plantillas por formato."""

from django.test import SimpleTestCase
from django.urls import reverse

from apps.composer.builtin_templates import TEMPLATES, get_all_templates
from apps.composer.models import PlatformPost
from apps.composer.quality import can_promote, has_blocks, quality_warnings, visible_counter
from apps.composer.tests.test_c1 import C1Base
from apps.composer.views import parse_chapters
from apps.settings_manager.models import WorkspaceSetting
from apps.social_accounts.models import SocialAccount


def codes(warnings):
    return {w["code"] for w in warnings}


class QualityTests(SimpleTestCase):
    def test_placeholders_block(self):
        w = quality_warnings(platform="facebook", caption="Hola {{tema}}")
        assert has_blocks(w) and "placeholders" in codes(w)

    def test_cover_repeats_title_caps_and_hashtags(self):
        w = quality_warnings(
            platform="instagram",
            title="Cómo vender más",
            caption="ESTO ES MUY BUENO #a #b #c #d #e #f",
            cover_text="Vender hoy",
        )
        assert {"cover_repeats_title", "too_many_caps", "ig_hashtags"} <= codes(w)
        assert not has_blocks(w)

    def test_two_caps_words_are_fine_and_hashtags_do_not_count_as_caps(self):
        assert "too_many_caps" not in codes(
            quality_warnings(platform="facebook", caption="HOLA MUNDO #ABC https://A.MX/XYZ")
        )

    def test_facebook_link_suggests_first_comment(self):
        assert "fb_link_in_body" in codes(quality_warnings(platform="facebook", caption="mira https://a.mx"))
        assert "fb_link_in_body" not in codes(
            quality_warnings(platform="facebook", caption="mira", first_comment="https://a.mx")
        )

    def test_duration_and_aspect(self):
        w = quality_warnings(platform="instagram", post_type="reel", duration=120, width=1920, height=1080)
        assert {"duration", "aspect"} <= codes(w) and has_blocks(w)
        assert not quality_warnings(platform="youtube", post_type="short", duration=59, width=1080, height=1920)
        assert "duration" in codes(quality_warnings(platform="youtube", post_type="short", duration=200))

    def test_thumbnail_over_2mb_blocks(self):
        assert has_blocks(quality_warnings(platform="youtube", thumbnail_bytes=2 * 1024 * 1024 + 1))
        assert not quality_warnings(platform="youtube", thumbnail_bytes=1024)

    def test_banned_phrases_case_insensitive(self):
        w = quality_warnings(platform="facebook", caption="Oferta IMPERDIBLE hoy", banned_phrases=["imperdible", " "])
        assert "banned_phrase" in codes(w)

    def test_solo_organico_blocks_promotion_only(self):
        extra = {"solo_organico": True}
        assert not can_promote(extra) and can_promote({}) and can_promote(None)
        assert has_blocks(quality_warnings(platform="facebook", promoting=True, extra=extra))
        assert not quality_warnings(platform="facebook", extra=extra)

    def test_visible_counter(self):
        assert visible_counter("youtube", "short", "x" * 41, "")["over"] is True
        assert visible_counter("youtube", "", "x" * 70, "")["over"] is False
        assert visible_counter("instagram", "reel", "", "x" * 126) == {
            "limit": 125, "used": 126, "over": True, "what": "texto"
        }  # fmt: skip
        assert visible_counter("tiktok", "", "a", "b") is None


class ChaptersParseTests(SimpleTestCase):
    def test_text_and_json(self):
        assert parse_chapters("00:00 Intro\nbasura\n1:05:00 Final") == [
            {"t": "00:00", "title": "Intro"},
            {"t": "1:05:00", "title": "Final"},
        ]
        assert parse_chapters('[{"t": "00:00", "title": "A"}]') == [{"t": "00:00", "title": "A"}]
        assert parse_chapters("[roto") == []


class C2Base(C1Base):
    def setUp(self):
        super().setUp()
        self.ig = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="instagram",
            account_platform_id="ig1",
            account_name="IG",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )

    def draft(self, post_id=None, **fields):
        data = {
            "caption": "hola",
            "action": "save_draft",
            "selected_accounts": f"{self.yt.id},{self.ig.id},{self.fb.id}",
            **fields,
        }
        url = (
            self.save_url
            if post_id is None
            else reverse("composer:save_post_edit", kwargs={"workspace_id": self.workspace.id, "post_id": post_id})
        )
        resp = self.client.post(url, data)
        self.assertIn(resp.status_code, (200, 204, 302))
        return resp

    def extra(self, account):
        return PlatformPost.objects.get(social_account=account).platform_extra


class PersistenceTests(C2Base):
    def test_save_post_stores_new_fields_per_account(self):
        cover = self.asset("cover.jpg")
        y, i, f = self.yt.id, self.ig.id, self.fb.id
        self.draft(
            **{
                f"yt_category_id_{y}": "27",
                f"yt_playlist_id_{y}": "PLabc-1",
                f"yt_made_for_kids_{y}": "false",
                f"yt_synthetic_media_{y}": "true",
                f"yt_chapters_{y}": "00:00 Intro\n00:30 Tema\n01:10 Cierre",
                f"solo_organico_{y}": "true",
                f"ig_cover_asset_id_{i}": str(cover.id),
                f"ig_thumb_offset_{i}": "1500",
                f"ig_collaborators_{i}": "@uno, dos tres cuatro",
                f"ig_share_to_feed_{i}": "false",
                f"fb_post_type_{f}": "reel",
                f"fb_link_{f}": "https://a.mx/x",
            }
        )
        yt = self.extra(self.yt)
        assert yt["category_id"] == "27" and yt["playlist_id"] == "PLabc-1"
        assert yt["made_for_kids"] is False and yt["self_declared_made_for_kids"] is False
        assert yt["contains_synthetic_media"] is True and yt["solo_organico"] is True
        assert [c["t"] for c in yt["chapters"]] == ["00:00", "00:30", "01:10"]
        ig = self.extra(self.ig)
        assert ig["ig_cover_asset_id"] == str(cover.id) and ig["thumb_offset"] == 1500
        assert ig["collaborators"] == ["uno", "dos", "tres"] and ig["share_to_feed"] is False
        fb = self.extra(self.fb)
        assert fb["post_type"] == "reel" and fb["link"] == "https://a.mx/x"

    def test_invalid_values_are_dropped_and_foreign_cover_rejected(self):
        from apps.organizations.models import Organization
        from apps.workspaces.models import Workspace

        other_ws = Workspace.objects.create(organization=Organization.objects.create(name="O2"), name="W2")
        from apps.media_library.models import MediaAsset

        foreign = MediaAsset.objects.create(
            workspace=other_ws,
            organization=other_ws.organization,
            filename="f.jpg",
            file="media_library/f.jpg",
            media_type="image",
            uploaded_by=self.user,
        )
        y, i = self.yt.id, self.ig.id
        self.draft(
            **{
                f"yt_category_id_{y}": "999",
                f"yt_playlist_id_{y}": "no valido!",
                f"ig_cover_asset_id_{i}": str(foreign.id),
                f"ig_thumb_offset_{i}": "-3",
                f"fb_link_{self.fb.id}": "javascript:alert(1)",
            }
        )
        assert "category_id" not in self.extra(self.yt) and "playlist_id" not in self.extra(self.yt)
        assert "ig_cover_asset_id" not in self.extra(self.ig) and "thumb_offset" not in self.extra(self.ig)
        assert "link" not in self.extra(self.fb)

    def test_partial_save_keeps_previous_c2_fields(self):
        y = self.yt.id
        self.draft(**{f"yt_made_for_kids_{y}": "true", f"yt_category_id_{y}": "10"})
        self.draft(post_id=PlatformPost.objects.get(social_account=self.yt).post_id)  # sin los campos
        yt = self.extra(self.yt)
        assert yt["made_for_kids"] is True and yt["category_id"] == "10"

    def test_autosave_persists_new_fields(self):
        y = self.yt.id
        resp = self.client.post(
            reverse("composer:autosave", kwargs={"workspace_id": self.workspace.id}),
            {
                "caption": "x",
                "selected_accounts": f"{y},{self.ig.id}",
                f"yt_made_for_kids_{y}": "true",
                f"ig_collaborators_{self.ig.id}": "uno",
            },
        )
        assert resp.status_code == 200
        assert self.extra(self.yt)["made_for_kids"] is True
        assert self.extra(self.ig)["collaborators"] == ["uno"]


class MadeForKidsGuardTests(C2Base):
    def sched(self, **extra):
        return self.schedule(selected_accounts=str(self.yt.id), **extra)

    def test_youtube_schedule_requires_explicit_made_for_kids(self):
        resp = self.sched()
        assert resp.status_code == 400
        assert "Hecho para niños" in resp.json()["errors"]["made_for_kids"]

    def test_both_answers_pass_the_kids_check(self):
        for value in ("true", "false"):
            resp = self.sched(**{f"yt_made_for_kids_{self.yt.id}": value})
            errors = resp.json().get("errors", {}) if resp.status_code == 400 else {}
            assert "made_for_kids" not in errors

    def test_bad_chapters_block_schedule(self):
        resp = self.sched(
            **{f"yt_made_for_kids_{self.yt.id}": "false", f"yt_chapters_{self.yt.id}": "00:00 a\n00:30 b"}
        )
        assert resp.status_code == 400 and "3 capítulos" in resp.json()["errors"]["chapters"]

    def test_non_youtube_targets_are_not_asked(self):
        resp = self.schedule()
        errors = resp.json().get("errors", {}) if resp.status_code == 400 else {}
        assert "made_for_kids" not in errors


class PreviewQualityTests(C2Base):
    def test_preview_returns_warnings_visible_counter_and_banned_phrases(self):
        WorkspaceSetting.objects.create(workspace=self.workspace, key="composer.banned_phrases", value=["gratis"])
        resp = self.client.post(
            reverse("composer:preview", kwargs={"workspace_id": self.workspace.id}),
            {"title": "T", "caption": "Todo gratis {{x}}", "selected_accounts": str(self.fb.id)},
        )
        p = resp.context["previews"][0]
        assert {"placeholders", "banned_phrase"} <= codes(p["quality"])
        assert p["visible"]["limit"] == 125


class TemplateTests(SimpleTestCase):
    def test_six_new_format_templates_and_old_ones_intact(self):
        ids = [t["id"] for t in get_all_templates()]
        assert len(ids) == len(set(ids)) == 118
        by_id = {t["id"]: t for t in TEMPLATES}
        assert by_id[162]["body"] == "{{gancho}}\n{{valor}}\n{{cta}}: {{link}}\n{{lema}}"
        assert by_id[163]["body"] == "{{gancho}}\n\n{{resumen}}\n\n{{capitulos}}\n\n{{links}}\n{{hashtags}}"
        assert by_id[164]["body"] == by_id[165]["body"] == "{{gancho}}\n{{cuerpo}}\n{{cta}}\n{{hashtags}}"
        assert by_id[166]["body"] == "{{beneficio}}\n{{prueba}}\n{{cta}}: {{link}}"
        assert by_id[167]["body"] == "{{gancho}}\n\n{{parrafos}}\n\n{{pregunta}}"
        for tid in range(162, 168):
            f = by_id[tid]["formato"]
            assert {"slug", "plataformas", "post_type", "campos", "primer_comentario", "medio"} <= set(f)


class EngineCoverTests(C2Base):
    def _dispatch(self, cover_ref):
        from unittest.mock import MagicMock, patch

        from django.core.files.base import ContentFile

        from apps.composer.models import Post, PostMedia
        from apps.media_library.models import MediaAsset
        from apps.publisher.engine import PublishEngine

        video = MediaAsset(
            workspace=self.workspace, organization=self.org, filename="v.mp4", media_type="video", uploaded_by=self.user
        )
        video.file.save("v.mp4", ContentFile(b"x" * 10), save=True)
        post = Post.objects.create(workspace=self.workspace, author=self.user, caption="c")
        PostMedia.objects.create(post=post, media_asset=video, position=0)
        pp = PlatformPost.objects.create(
            post=post,
            social_account=self.ig,
            status="publishing",
            platform_extra={"ig_cover_asset_id": cover_ref, "post_type": "reel"},
        )
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

    def test_cover_asset_becomes_public_url(self):
        from django.core.files.base import ContentFile

        cover = self.asset("c2.jpg")
        cover.file.save("c2.jpg", ContentFile(b"\xff\xd8\xff"), save=True)
        extra = self._dispatch(str(cover.id))
        assert "c2" in extra["cover_url"] and "ig_cover_asset_id" not in extra

    def test_missing_cover_is_ignored(self):
        import uuid

        assert "cover_url" not in self._dispatch(str(uuid.uuid4()))
