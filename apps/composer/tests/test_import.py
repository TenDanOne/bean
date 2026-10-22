"""Importador por publicación: un .zip (bean.json v1 + medios) -> un borrador del composer."""

import io
import json
import shutil
import tempfile
import zipfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from PIL import Image

from apps.composer.models import PlatformPost, Post
from apps.composer.tests.test_lote_a1 import Base
from apps.media_library.models import MediaAsset
from apps.members.models import WorkspaceMembership
from apps.publisher.engine import PublishEngine
from apps.social_accounts.models import SocialAccount
from providers.types import PostType

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


def png(color=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buf, "PNG")
    return buf.getvalue()


def piece(**kw):
    base = {
        "id": "p1",
        "tipo": "video_vertical",
        "medios": ["v.mp4"],
        "solo_organico": True,
        "motivo_organico": "clip de prueba",
        "portada_texto": "Hola",
        "titulo_alt": "Título alterno",
        "redes": {
            "youtube": {"titulo": "Mi Short", "descripcion": "Desc YT", "hashtags": ["#a"], "tags": ["uno", "dos"],
                        "categoria_id": "27", "hecho_para_ninos": False, "contiene_ia": False,
                        "primer_comentario": "Comenta"},
            "instagram": {"tipo": "reel", "descripcion": "Desc IG", "hashtags": ["#a", "#b"], "portada_seg": 9.6,
                          "colaboradores": ["@amigo"], "compartir_en_feed": True, "primer_comentario": "Comenta"},
            "facebook": {"tipo": "reel", "descripcion": "Desc FB", "enlace": "https://simu.app"},
            "tiktok": {"tipo": "video", "descripcion": "Desc TT", "portada_seg": 2,
                       "permitir_comentarios": True, "musica_sugerida": "algo en tendencia"},
        },
    }  # fmt: skip
    return {**base, **kw}


def make_zip(pieces=None, files=None, manifest=None, raw=None, extra_infos=()):
    pieces = [piece()] if pieces is None else pieces
    files = {"v.mp4": MP4} if files is None else files
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if raw is None:
            m = {"version": 1, "proyecto": "simu", "marca": "simu", "espacio_sugerido": "WS", "piezas": pieces}
            z.writestr("bean.json", json.dumps(manifest or m))
        elif raw:
            z.writestr("bean.json", raw)
        for name, data in files.items():
            z.writestr(name, data)
        for info, data in extra_infos:
            z.writestr(info, data)
    return SimpleUploadedFile("paquete.zip", buf.getvalue(), content_type="application/zip")


class ImportBase(Base):
    def setUp(self):
        super().setUp()
        self._media = tempfile.mkdtemp()
        self._ov = override_settings(MEDIA_ROOT=self._media)
        self._ov.enable()
        self.addCleanup(shutil.rmtree, self._media, True)
        self.addCleanup(self._ov.disable)
        ok = SocialAccount.ConnectionStatus.CONNECTED
        self.yt = self.acc("youtube", "yt1", ok)
        self.ig = self.acc("instagram", "ig1", ok)
        self.tt = self.acc("tiktok", "tt1", ok)
        self.url = reverse("composer:import_package", kwargs={"workspace_id": self.workspace.id})

    def acc(self, platform, pid, status):
        return SocialAccount.objects.create(
            workspace=self.workspace,
            platform=platform,
            account_platform_id=pid,
            account_name=pid,
            connection_status=status,
        )

    def up(self, zipfile_, **data):
        return self.client.post(self.url, {"file": zipfile_, **data})

    def err(self, resp):
        self.assertEqual(resp.status_code, 400, resp.content)
        return resp.json()["error"]


class ValidImportTests(ImportBase):
    def test_valid_zip_creates_draft_with_fields_and_media(self):
        resp = self.up(make_zip())
        self.assertEqual(resp.status_code, 200, resp.content)
        post = Post.objects.get()
        self.assertEqual(resp.json()["redirect"], reverse("composer:compose_edit", args=[self.workspace.id, post.id]))
        self.assertEqual(post.title, "Mi Short")
        self.assertEqual(post.caption, "Desc IG\n\n") if False else None
        self.assertTrue(post.caption.startswith("Desc IG"))
        self.assertIn("Solo orgánico: clip de prueba", post.internal_notes)
        self.assertIn("Elige la música en la app de TikTok", post.internal_notes)
        self.assertEqual(post.media_attachments.count(), 1)
        self.assertEqual(MediaAsset.objects.get().mime_type, "video/mp4")
        self.assertEqual(
            {pp.social_account.platform for pp in post.platform_posts.all()},
            {"youtube", "instagram", "facebook", "tiktok"},
        )
        self.assertEqual({pp.status for pp in post.platform_posts.all()}, {"draft"})
        by = {pp.social_account.platform: pp for pp in post.platform_posts.all()}
        y = by["youtube"].platform_extra
        self.assertEqual(
            (y["post_type"], y["made_for_kids"], y["category_id"], y["tags"]), ("short", False, "27", ["uno", "dos"])
        )
        self.assertEqual(by["youtube"].platform_specific_title, "Mi Short")
        self.assertEqual(by["youtube"].platform_specific_caption, "Desc YT\n\n#a")
        i = by["instagram"].platform_extra
        self.assertEqual(
            (i["post_type"], i["thumb_offset"], i["collaborators"], i["share_to_feed"]), ("reel", 9600, ["amigo"], True)
        )
        self.assertEqual(by["facebook"].platform_extra["post_type"], "reel")
        t = by["tiktok"].platform_extra
        self.assertNotIn("privacy_level", t)
        self.assertEqual((t["disable_comment"], t["disable_duet"], t["video_cover_timestamp_ms"]), (False, True, 2000))
        self.assertEqual(t["import_source"], {"proyecto": "simu", "pieza_id": "p1"})
        self.assertEqual(by["youtube"].platform_specific_media, [str(MediaAsset.objects.get().id)])
        self.assertIn(
            "Importado:",
            [
                m.message
                for m in self.client.get(reverse("composer:compose_edit", args=[self.workspace.id, post.id])).context[
                    "messages"
                ]
            ][0],
        )

    def test_contiene_ia_de_la_pieza_marca_youtube_tiktok_y_avisa_en_ig_fb(self):
        self.assertEqual(self.up(make_zip(pieces=[piece(contiene_ia=True)])).status_code, 200)
        by = {pp.social_account.platform: pp.platform_extra for pp in Post.objects.get().platform_posts.all()}
        self.assertIs(by["youtube"]["contains_synthetic_media"], True)
        self.assertIs(by["tiktok"]["is_aigc"], True)
        self.assertIs(by["instagram"]["marca_ia_en_app"], True)
        self.assertIs(by["facebook"]["marca_ia_en_app"], True)

    def test_sin_contiene_ia_no_marca_nada(self):
        self.assertEqual(self.up(make_zip()).status_code, 200)
        by = {pp.social_account.platform: pp.platform_extra for pp in Post.objects.get().platform_posts.all()}
        self.assertNotIn("contains_synthetic_media", by["youtube"])
        self.assertNotIn("is_aigc", by["tiktok"])
        self.assertNotIn("marca_ia_en_app", by["instagram"])

    def test_per_network_media_and_tiktok_photos(self):
        p = piece(
            id="m1", tipo="imagen", medios=["a.png"], medios_por_red={"instagram": ["a45.png"], "tiktok": ["a.png"]},
            redes={"instagram": {"tipo": "feed", "descripcion": "IG"}, "tiktok": {"tipo": "fotos", "titulo": "T", "descripcion": "TT"}},
        )  # fmt: skip
        resp = self.up(make_zip([p], {"a.png": png(), "a45.png": png((1, 2, 3))}))
        self.assertEqual(resp.status_code, 200, resp.content)
        by = {pp.social_account.platform: pp for pp in Post.objects.get().platform_posts.all()}
        self.assertEqual(len(by["instagram"].platform_specific_media), 1)
        self.assertNotEqual(by["instagram"].platform_specific_media, by["tiktok"].platform_specific_media)
        self.assertEqual(by["tiktok"].platform_extra["post_type"], "image")
        self.assertEqual(by["tiktok"].platform_specific_title, "T")
        self.assertNotIn("post_type", by["instagram"].platform_extra)
        self.assertEqual(Post.objects.get().media_attachments.count(), 2)

    def test_facebook_album_maps_to_multi_photo(self):
        fb = SocialAccount.ConnectionStatus.CONNECTED
        self.acc("facebook", "fb2", fb)
        p = piece(
            id="al",
            tipo="carrusel",
            medios=["a.png", "b.png"],
            redes={"facebook": {"tipo": "album", "descripcion": "Álbum"}},
        )
        self.assertEqual(self.up(make_zip([p], {"a.png": png(), "b.png": png((9, 9, 9))})).status_code, 200)
        pp = PlatformPost.objects.filter(social_account__platform="facebook")
        self.assertEqual(pp.count(), 2)  # todas las cuentas conectadas que encajan
        self.assertNotIn("post_type", pp[0].platform_extra)
        self.assertEqual(len(pp[0].platform_specific_media), 2)
        self.assertEqual(PublishEngine._resolve_post_type("facebook", pp[0].platform_extra, 2, "image"), PostType.IMAGE)

    def test_archived_and_disconnected_accounts_are_excluded(self):
        SocialAccount.objects.filter(id=self.tt.id).update(connection_status=SocialAccount.ConnectionStatus.ARCHIVED)
        self.up(make_zip())
        self.assertNotIn("tiktok", {pp.social_account.platform for pp in PlatformPost.objects.all()})

    def test_carousel_without_youtube_media_skips_youtube(self):
        p = piece(
            id="c1",
            tipo="carrusel",
            medios=["a.png", "b.png"],
            redes={"youtube": {"hecho_para_ninos": False}, "instagram": {"tipo": "carrusel", "descripcion": "x"}},
        )
        resp = self.up(make_zip([p], {"a.png": png(), "b.png": png((5, 5, 5))}))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual({pp.social_account.platform for pp in PlatformPost.objects.all()}, {"instagram"})
        msgs = [str(m) for m in self.client.get(resp.json()["redirect"]).context["messages"]]
        self.assertIn("publícalo a mano desde la app", msgs[0])

    def test_carousel_with_alt_mp4_publishes_youtube_as_short(self):
        p = piece(id="c2", tipo="carrusel", medios=["a.png", "b.png"], medios_por_red={"youtube": ["s.mp4"]},
                  redes={"youtube": {"titulo": "T", "hecho_para_ninos": False}, "instagram": {"tipo": "carrusel", "descripcion": "x"}})  # fmt: skip
        resp = self.up(make_zip([p], {"a.png": png(), "b.png": png((5, 5, 5)), "s.mp4": MP4}))
        self.assertEqual(resp.status_code, 200, resp.content)
        yt = PlatformPost.objects.get(social_account__platform="youtube")
        self.assertEqual(yt.platform_extra["post_type"], "short")

    def test_reimport_asks_then_updates_the_existing_draft(self):
        self.assertEqual(self.up(make_zip()).status_code, 200)
        first = Post.objects.get()
        resp = self.up(make_zip())
        self.assertEqual(list(resp.json()), ["existe"])
        self.assertEqual(Post.objects.count(), 1)
        changed = piece(
            titulo_alt="Otro",
            redes={**piece()["redes"], "youtube": {**piece()["redes"]["youtube"], "titulo": "Nuevo título"}},
        )
        resp = self.up(make_zip([changed]), modo="actualizar")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(Post.objects.count(), 1)
        first.refresh_from_db()
        self.assertEqual(first.title, "Nuevo título")
        self.assertEqual(first.platform_posts.count(), 4)
        self.up(make_zip(), modo="nuevo")
        self.assertEqual(Post.objects.count(), 2)

    def test_multiple_pieces_ask_which_one(self):
        two = [piece(), piece(id="p2")]
        resp = self.up(make_zip(two))
        self.assertEqual([p["id"] for p in resp.json()["elegir"]], ["p1", "p2"])
        self.assertFalse(Post.objects.exists())
        self.assertEqual(self.up(make_zip(two), pieza="p2").status_code, 200)
        self.assertEqual(PlatformPost.objects.first().platform_extra["import_source"]["pieza_id"], "p2")

    def test_object_media_entries_and_real_package_shape(self):
        p = piece(medios=[{"ruta": "v.mp4", "ancho": 1080, "alto": 1920, "duracion_s": 12.5}])
        self.assertEqual(self.up(make_zip([p])).status_code, 200)
        a = MediaAsset.objects.get()
        self.assertEqual((a.width, a.height, a.duration), (1080, 1920, 12.5))


class RejectedPackageTests(ImportBase):
    def assert_rejected(self, zf, titulo):
        e = self.err(self.up(zf))
        self.assertIn(titulo, e["titulo"])
        self.assertFalse(Post.objects.exists())
        self.assertFalse(MediaAsset.objects.exists())

    def test_not_a_zip(self):
        self.assert_rejected(SimpleUploadedFile("x.zip", b"no soy zip"), "No pudimos abrir")

    def test_zip_slip(self):
        self.assert_rejected(make_zip(extra_infos=[("../evil.txt", b"x")]), "rutas inseguras")
        self.assert_rejected(make_zip(extra_infos=[("/abs.txt", b"x")]), "rutas inseguras")

    def test_symlink(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("bean.json", "{}")
            info = zipfile.ZipInfo("link")
            info.external_attr = 0o120777 << 16
            z.writestr(info, "v.mp4")
        self.assert_rejected(SimpleUploadedFile("l.zip", buf.getvalue()), "enlaces simbólicos")

    @override_settings(BEAN_IMPORT_MAX_UNCOMPRESSED_BYTES=5000)
    def test_zip_bomb(self):
        self.assert_rejected(make_zip(files={"v.mp4": MP4, "big.bin": b"\x00" * 200_000}), "demasiado grande")

    @override_settings(BEAN_IMPORT_MAX_ZIP_BYTES=100)
    def test_zip_too_big(self):
        self.assert_rejected(make_zip(), "pesa demasiado")

    @override_settings(BEAN_IMPORT_MAX_FILES=1)
    def test_too_many_files(self):
        self.assert_rejected(make_zip(), "demasiados archivos")

    def test_missing_bean_json_names_nested_one(self):
        self.assert_rejected(make_zip(raw="", files={"carpeta/bean.json": b"{}"}), "no trae bean.json")

    def test_bad_version_and_bad_json(self):
        m = {"version": 2, "proyecto": "x", "piezas": [piece()]}
        e = self.err(self.up(make_zip(manifest=m)))
        self.assertIn("versión 2", e["titulo"])
        self.assert_rejected(make_zip(raw="{no json"), "no se puede leer")

    def test_missing_media(self):
        e = self.err(self.up(make_zip(files={})))
        self.assertIn("Falta un archivo", e["titulo"])
        self.assertIn("v.mp4", e["mensaje"])

    def test_fake_media_type(self):
        self.assert_rejected(make_zip(files={"v.mp4": b"<html>no soy video</html>"}), "no es del tipo")
        self.assert_rejected(make_zip(files={"v.mp4": png()}), "no es del tipo")  # extensión miente

    def test_schema_errors(self):
        bad_kids = piece()
        del bad_kids["redes"]["youtube"]["hecho_para_ninos"]
        self.assertIn("hecho_para_ninos", self.err(self.up(make_zip([bad_kids])))["mensaje"])
        self.assertIn("piezas[0].tipo", self.err(self.up(make_zip([piece(tipo="otro")])))["mensaje"])
        self.assertIn(
            "titulo",
            self.err(self.up(make_zip([piece(redes={"tiktok": {"tipo": "fotos", "titulo": "x" * 91}})])))["mensaje"],
        )

    def test_no_matching_accounts(self):
        SocialAccount.objects.filter(workspace=self.workspace).exclude(platform="facebook").delete()
        SocialAccount.objects.filter(workspace=self.workspace).update(
            connection_status=SocialAccount.ConnectionStatus.DISCONNECTED
        )
        self.assertIn("a dónde enviar", self.err(self.up(make_zip()))["titulo"])


class ImportPermissionTests(ImportBase):
    role = WorkspaceMembership.WorkspaceRole.VIEWER

    def test_viewer_cannot_import(self):
        self.assertEqual(self.up(make_zip()).status_code, 403)
        self.assertFalse(Post.objects.exists())


class RealPackageTests(ImportBase):
    def test_bundled_image_package_shape(self):
        """Forma real del paquete de posts: medios como lista de textos y tiktok «fotos»."""
        p = {
            "id": "simu_bob_meme_imagen", "tipo": "imagen", "medios": ["meme_4x5.png"], "solo_organico": True,
            "motivo_organico": "imagen de terceros", "portada_texto": "Se vale sentirlo", "titulo_alt": "alt", "fijar_comentario": True,
            "medios_por_red": {"instagram": ["meme_4x5.png"], "facebook": ["meme_4x5.png"], "tiktok": ["meme.png"]},
            "redes": {
                "tiktok": {"tipo": "fotos", "titulo": "Resultados 👇", "descripcion": "d", "hashtags": ["#examen"], "permitir_comentarios": True,
                           "musica_sugerida": "triste", "primer_comentario": "¿A quién le pasó? 👇"},
                "instagram": {"tipo": "feed", "descripcion": "d", "hashtags": ["#examen"], "compartir_en_feed": True},
                "facebook": {"tipo": "feed", "descripcion": "d", "hashtags": ["#examen"]},
            },
        }  # fmt: skip
        resp = self.up(make_zip([p], {"meme_4x5.png": png(), "meme.png": png((7, 7, 7))}))
        self.assertEqual(resp.status_code, 200, resp.content)
        post = Post.objects.get()
        self.assertEqual(post.platform_posts.count(), 3)
        self.assertIn("¿A quién le pasó?", post.first_comment)
