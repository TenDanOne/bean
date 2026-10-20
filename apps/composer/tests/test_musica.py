"""Música «en la app»: borrador de TikTok, «A mano» en IG/YouTube/FB, importador con musica/portadas."""

import os
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.composer.models import PlatformPost, Post
from apps.composer.tests.test_import import MP4, ImportBase, make_zip, piece, png
from apps.composer.views import _apply_c2_extras, _publish_state
from apps.publisher.engine import PublishEngine
from providers.types import PublishStatus

MUSICA = {
    "tiktok": "en_app", "instagram": "en_app", "youtube": "libre_en_mp4", "facebook": "libre_en_mp4",
    "sugerencia": "canción triste", "golpe_seg": 31.9, "nota": "en la luz",
}  # fmt: skip


def by_platform():
    return {pp.social_account.platform: pp for pp in PlatformPost.objects.all()}


class ImportMusicaTests(ImportBase):
    def test_en_app_y_libre_sin_medio_propio(self):
        p = piece(musica=MUSICA, portada_ruta="c.png")
        resp = self.up(make_zip([p], {"v.mp4": MP4, "c.png": png()}))
        self.assertEqual(resp.status_code, 200, resp.content)
        by = by_platform()
        t, i, y, f = (by[k].platform_extra for k in ("tiktok", "instagram", "youtube", "facebook"))
        self.assertEqual(t["tiktok_mode"], "draft")
        self.assertNotIn("manual_publish", t)
        self.assertTrue(i["manual_publish"] and f["manual_publish"])
        self.assertTrue(y["manual_publish"])  # libre_en_mp4 sin medios_por_red.youtube
        self.assertEqual(t["musica"], {"sugerencia": "canción triste", "golpe_seg": 31.9, "nota": "en la luz"})
        # portada_ruta de la pieza -> portada de IG; la imagen manda sobre portada_seg
        self.assertIn("ig_cover_asset_id", i)
        self.assertNotIn("thumb_offset", i)
        self.assertEqual(t["video_cover_timestamp_ms"], 2000)
        self.assertNotIn("thumbnail_asset_id", y)  # Short: sin miniatura

    def test_libre_en_mp4_con_medio_propio_no_es_a_mano(self):
        p = piece(musica={"youtube": "libre_en_mp4", "sugerencia": "x"}, medios_por_red={"youtube": ["y.mp4"]})
        resp = self.up(make_zip([p], {"v.mp4": MP4, "y.mp4": MP4 + b"1"}))
        self.assertEqual(resp.status_code, 200, resp.content)
        y = by_platform()["youtube"].platform_extra
        self.assertNotIn("manual_publish", y)
        self.assertNotIn("musica", y)

    def test_youtube_largo_usa_portada_ruta_como_miniatura(self):
        p = piece(tipo="video_horizontal", portada_ruta="c.png")
        resp = self.up(make_zip([p], {"v.mp4": MP4, "c.png": png()}))
        self.assertEqual(resp.status_code, 200, resp.content)
        by = by_platform()
        self.assertIn("thumbnail_asset_id", by["youtube"].platform_extra)
        self.assertIn("ig_cover_asset_id", by["instagram"].platform_extra)

    def test_portada_seg_de_la_pieza_es_el_fotograma_de_ig_sin_imagen(self):
        p = piece(portada_seg=4)
        del p["redes"]["instagram"]["portada_seg"]
        self.assertEqual(self.up(make_zip([p])).status_code, 200)
        self.assertEqual(by_platform()["instagram"].platform_extra["thumb_offset"], 4000)

    def test_musica_invalida_se_rechaza(self):
        resp = self.up(make_zip([piece(musica={"tiktok": "radio"})]))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("musica.tiktok", resp.json()["error"]["mensaje"])


REAL = os.environ.get("BEAN_TEST_PACKAGES_DIR", "")


@pytest.mark.skipif(not os.path.isdir(REAL), reason="define BEAN_TEST_PACKAGES_DIR con los zips reales")
class RealZipTests(ImportBase):
    def test_real_zips_importan(self):
        for name in sorted(os.listdir(REAL)):
            if not name.endswith(".zip"):
                continue
            with open(os.path.join(REAL, name), "rb") as fh:
                resp = self.up(SimpleUploadedFile(name, fh.read(), content_type="application/zip"))
            self.assertEqual(resp.status_code, 200, (name, resp.content))
        tt = list(PlatformPost.objects.filter(social_account__platform="tiktok"))
        self.assertTrue(tt and all(pp.platform_extra["tiktok_mode"] == "draft" for pp in tt))
        ig = list(PlatformPost.objects.filter(social_account__platform="instagram"))
        self.assertTrue(all(pp.platform_extra.get("manual_publish") for pp in ig))
        self.assertTrue(any(pp.platform_extra.get("ig_cover_asset_id") for pp in ig))
        self.assertTrue(all(pp.platform_extra["musica"]["sugerencia"] for pp in tt))


class EngineManualTests(ImportBase):
    def _pp(self, acc, **extra):
        if not hasattr(self, "post"):
            self.post = Post.objects.create(workspace=self.workspace, author=self.user, caption="x")
        return PlatformPost.objects.create(post=self.post, social_account=acc, status="scheduled", platform_extra=extra)

    def test_manual_publish_no_llama_al_proveedor_ni_al_resto(self):
        manual = self._pp(self.ig, manual_publish=True)
        with patch.object(PublishEngine, "_dispatch_to_provider") as disp:
            result = PublishEngine()._publish_platform_post(manual)
        disp.assert_not_called()
        self.assertTrue(result["success"])
        manual.refresh_from_db()
        self.assertEqual((manual.status, manual.platform_post_id, manual.outcome_label), ("published", "", "A mano"))
        self.assertEqual(_publish_state(manual), "manual")

    def test_reintento_manual_no_llama_al_proveedor(self):
        manual = self._pp(self.fb, manual_publish=True)
        manual.retry_count = 2
        manual.save()
        with patch.object(PublishEngine, "_dispatch_to_provider") as disp:
            result = PublishEngine()._publish_platform_post(manual)
        disp.assert_not_called()
        self.assertTrue(result["success"])
        manual.refresh_from_db()
        self.assertEqual(manual.status, "published")

    def test_fallo_al_marcar_manual_no_deja_publishing(self):
        manual = self._pp(self.fb, manual_publish=True)
        manual.status = "publishing"
        manual.save()
        with patch.object(PublishEngine, "_mark_published", side_effect=RuntimeError("db")):
            result = PublishEngine()._publish_platform_post(manual)
        self.assertFalse(result["success"])
        manual.refresh_from_db()
        self.assertNotEqual(manual.status, "publishing")

    def test_etiqueta_borrador_exige_modo_draft_y_clonar_no_copia_resultados(self):
        from apps.composer.services import clone_post

        pp = self._pp(self.tt, tiktok_draft=True, tiktok_mode="direct", thumbnail_error="x", musica={"nota": "n"})
        pp.status = "published"
        self.assertEqual(pp.outcome_label, "")
        pp.platform_extra["tiktok_mode"] = "draft"
        self.assertEqual(pp.outcome_label, "Borrador en TikTok")
        pp.save()
        copia = clone_post(self.post).platform_posts.get()
        self.assertEqual(copia.platform_extra, {"tiktok_mode": "draft", "musica": {"nota": "n"}})

    def test_guardia_no_exige_nada_a_cuentas_a_mano(self):
        from apps.composer.views import _is_manual

        req = MagicMock()
        req.POST = {f"manual_publish_{self.yt.id}": "true"}
        self.assertTrue(_is_manual(req, None, str(self.yt.id)))
        self._pp(self.yt, manual_publish=True)
        req.POST = {}
        self.assertTrue(_is_manual(req, self.post.id, str(self.yt.id)))
        req.POST = {f"manual_publish_{self.yt.id}": "false"}
        self.assertFalse(_is_manual(req, self.post.id, str(self.yt.id)))

    def test_borrador_de_tiktok_queda_como_estado_final(self):
        pp = self._pp(self.tt, tiktok_mode="draft")
        pp.status, pp.platform_post_id = "publishing", "v_inbox_file~1"
        pp.save()
        provider = MagicMock()
        provider.check_publish_status.return_value = PublishStatus("published", extra={"tiktok_draft": True})
        with (
            patch("apps.publisher.engine.get_provider", return_value=provider),
            patch("apps.publisher.engine._resolve_publish_credentials", return_value={}),
            patch("apps.social_accounts.models.SocialAccount.get_fresh_access_token", return_value="t"),
        ):
            PublishEngine()._check_pending_publishes()
        pp.refresh_from_db()
        self.assertEqual(pp.status, "published")
        self.assertEqual(pp.platform_post_id, "v_inbox_file~1")
        self.assertEqual((pp.outcome_label, _publish_state(pp)), ("Borrador en TikTok", "draft"))
        self.assertIn("Termínalo en la app de TikTok", pp.outcome_note)

    def test_nota_con_cancion_y_golpe(self):
        pp = self._pp(self.ig, manual_publish=True, musica={"sugerencia": "triste", "golpe_seg": 31.9})
        pp.status = "published"
        self.assertIn("Canción sugerida: triste, golpe en 31,9 s", pp.outcome_note)

    def test_formulario_guarda_modo_y_casilla_por_cuenta(self):
        req = MagicMock()
        req.POST = {f"tiktok_mode_{self.tt.id}": "draft", f"manual_publish_{self.ig.id}": "true"}
        out = _apply_c2_extras(req, self.workspace, str(self.tt.id), "tiktok", {}, {"musica": {"nota": "n"}})
        self.assertEqual((out["tiktok_mode"], out["musica"]), ("draft", {"nota": "n"}))
        out = _apply_c2_extras(req, self.workspace, str(self.ig.id), "instagram", {}, {})
        self.assertIs(out["manual_publish"], True)
        req.POST = {f"manual_publish_{self.ig.id}": "false"}
        out = _apply_c2_extras(req, self.workspace, str(self.ig.id), "instagram", {}, {"manual_publish": True})
        self.assertNotIn("manual_publish", out)
        req.POST = {f"tiktok_mode_{self.tt.id}": "hack"}
        self.assertNotIn("tiktok_mode", _apply_c2_extras(req, self.workspace, str(self.tt.id), "tiktok", {}, {}))
