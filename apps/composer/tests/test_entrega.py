"""«¿Cómo lo mandas?»: modo de entrega por red (backend) y hoja de revisión del importador."""

from datetime import timedelta
from unittest.mock import patch

from django.test import SimpleTestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer import delivery
from apps.composer.models import PlatformPost, Post
from apps.composer.tests.test_import import ImportBase, make_zip
from apps.composer.tests.test_lote_a1 import Base
from apps.media_library.models import MediaAsset
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


class DeliveryUnitTests(SimpleTestCase):
    def test_instagram_y_facebook_sin_video_caen_en_a_mano(self):
        for platform, video in (("instagram", True), ("instagram_login", True), ("facebook", False)):
            extra = {}
            self.assertEqual(delivery.set_mode(platform, extra, "borrador", video=video), "a_mano")
            self.assertEqual(extra, {"manual_publish": True})

    def test_borrador_por_red(self):
        fb, yt, tt = {}, {}, {}
        self.assertEqual(delivery.set_mode("facebook", fb, "borrador", video=True), "borrador")
        self.assertEqual(delivery.set_mode("youtube", yt, "borrador"), "borrador")
        self.assertEqual(delivery.set_mode("tiktok", tt, "borrador"), "borrador")
        self.assertEqual((fb, yt, tt), ({"entrega": "borrador"}, {"entrega": "borrador"}, {"tiktok_mode": "draft"}))

    def test_directo_limpia_los_otros_modos(self):
        extra = {"entrega": "borrador", "manual_publish": True}
        delivery.set_mode("youtube", extra, "directo")
        self.assertEqual(extra, {})
        self.assertEqual(delivery.mode_of("youtube", {"manual_publish": True}), "a_mano")


class DeliveryViewTests(Base):
    def setUp(self):
        super().setUp()
        ok = SocialAccount.ConnectionStatus.CONNECTED
        self.yt, self.tt, self.ig = (
            SocialAccount.objects.create(
                workspace=self.workspace, platform=p, account_platform_id=p + "1", account_name=p, connection_status=ok
            )
            for p in ("youtube", "tiktok", "instagram")
        )

    def save(self, **data):
        accs = [self.fb, self.yt, self.tt, self.ig]
        resp = self.client.post(
            self.save_url,
            {"caption": "hola", "action": "save_draft", "selected_accounts": ",".join(str(a.id) for a in accs), **data},
        )
        self.assertIn(resp.status_code, (200, 204, 302), resp.content)
        return {
            pp.social_account.platform: pp.platform_extra
            for pp in PlatformPost.objects.filter(post=Post.objects.latest("created_at"))
        }

    def test_borrador_global_da_el_modo_de_cada_red(self):
        by = self.save(
            **{
                f"entrega_{self.fb.id}": "borrador",
                f"fb_post_type_{self.fb.id}": "reel",
                f"entrega_{self.yt.id}": "borrador",
                f"entrega_{self.tt.id}": "borrador",
                f"entrega_{self.ig.id}": "borrador",
            }
        )
        self.assertEqual(by["facebook"]["entrega"], "borrador")
        self.assertEqual(by["youtube"]["entrega"], "borrador")
        self.assertEqual(by["tiktok"]["tiktok_mode"], "draft")
        self.assertTrue(by["instagram"]["manual_publish"])
        self.assertNotIn("entrega", by["instagram"])

    def test_facebook_texto_cae_en_a_mano_y_directo_lo_quita(self):
        by = self.save(**{f"entrega_{self.fb.id}": "borrador"})
        self.assertTrue(by["facebook"]["manual_publish"])
        self.assertNotIn("entrega", by["facebook"])
        by = self.save(**{f"entrega_{self.fb.id}": "directo", f"entrega_{self.yt.id}": "borrador"})
        self.assertNotIn("manual_publish", by["facebook"])
        self.assertEqual(by["youtube"]["entrega"], "borrador")

    def test_etiquetas_finales_y_notas(self):
        def pp(account, **extra):
            post = Post.objects.create(workspace=self.workspace, caption="c")
            return PlatformPost.objects.create(
                post=post, social_account=account, status="published", platform_extra=extra
            )

        yt = pp(self.yt, entrega="borrador")
        self.assertEqual(yt.outcome_label, "Privado")
        self.assertEqual(yt.outcome_note, "Abre YouTube Studio, ponle música y cámbialo a Público.")
        fb = pp(self.fb, entrega="borrador")
        self.assertEqual(fb.outcome_label, "Borrador")
        self.assertIn("Meta Business Suite", fb.outcome_note)
        self.assertEqual(pp(self.ig, manual_publish=True).outcome_label, "A mano")
        self.assertEqual(pp(self.yt).outcome_label, "")


class ImportReviewTests(ImportBase):
    def apply(self, post_id, **data):
        url = reverse("composer:import_review", kwargs={"workspace_id": self.workspace.id})
        return self.client.post(url, {"post_id": post_id, **data})

    def revision(self):
        resp = self.up(make_zip(), revisar="1")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["revision"]

    def test_la_hoja_resume_cuentas_modo_ninos_y_zona(self):
        r = self.revision()
        self.assertEqual(r["zona"], self.workspace.effective_timezone or "UTC")
        self.assertEqual({c["plataforma"] for c in r["cuentas"]}, {"youtube", "instagram", "facebook", "tiktok"})
        self.assertEqual({c["modo"] for c in r["cuentas"]}, {"Directo"})
        self.assertEqual(r["ninos"]["valor"], "no")
        self.assertEqual(r["tipo"], "Video vertical")
        self.assertTrue(r["editor"].endswith(f"{r['post_id']}/"))
        self.assertEqual(Post.objects.count(), 1)

    def test_programar_deja_todo_programado_y_conserva_los_ajustes_de_youtube(self):
        r = self.revision()
        when = timezone.now() + timedelta(days=2)
        resp = self.apply(r["post_id"], accion="programar", ninos="si", fecha=when.strftime("%Y-%m-%d"), hora="10:00")
        self.assertEqual(resp.status_code, 200, resp.content)
        post = Post.objects.get()
        self.assertEqual({pp.status for pp in post.platform_posts.all()}, {"scheduled"})
        y = post.platform_posts.get(social_account=self.yt).platform_extra
        self.assertEqual((y["made_for_kids"], y["tags"], y["post_type"]), (True, ["uno", "dos"], "short"))
        self.assertEqual(y["category_id"], "27")

    def programar(self, r, **extra):
        when = timezone.now() + timedelta(days=2)
        return self.apply(
            r["post_id"], accion="programar", ninos="no", fecha=when.strftime("%Y-%m-%d"), hora="10:00", **extra
        )

    def test_portada_elegida_se_guarda_en_tiktok(self):
        r = self.revision()
        MediaAsset.objects.update(duration=12.5)  # fotogramas ofrecidos: 0, 3 y 6 s
        self.assertEqual(r["pieza"], [1, 1])
        resp = self.programar(r, portada="3000")
        self.assertEqual(resp.status_code, 200, resp.content)
        extra = Post.objects.get().platform_posts.get(social_account=self.tt).platform_extra
        self.assertEqual(extra["video_cover_timestamp_ms"], 3000)

    def test_sin_tocar_la_portada_se_conservan_los_ms_por_red(self):
        resp = self.programar(self.revision(), portada="paquete")  # lo que manda el navegador con la opción por defecto
        self.assertEqual(resp.status_code, 200, resp.content)
        by = {pp.social_account.platform: pp.platform_extra for pp in Post.objects.get().platform_posts.all()}
        self.assertEqual(by["instagram"]["thumb_offset"], 9600)
        self.assertEqual(by["tiktok"]["video_cover_timestamp_ms"], 2000)

    def test_portada_fuera_de_las_opciones_es_400(self):
        r = self.revision()
        MediaAsset.objects.update(duration=12.5)
        for bad in ("99999", "7", "12500", "-1", "x"):
            self.assertEqual(self.programar(r, portada=bad).status_code, 400, bad)

    def test_programar_con_fecha_pasada_falla(self):
        r = self.revision()
        resp = self.apply(r["post_id"], accion="programar", fecha="2020-01-01", hora="10:00")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual({pp.status for pp in Post.objects.get().platform_posts.all()}, {"draft"})

    def test_publicar_ahora_programa_a_ya_y_drena_la_cola(self):
        r = self.revision()
        with patch("apps.composer.views.PublishEngine") as engine, patch("apps.composer.views.drain_pending_tasks"):
            resp = self.apply(r["post_id"], accion="publicar", ninos="no")
        self.assertEqual(resp.status_code, 200, resp.content)
        engine.return_value.poll_and_publish.assert_called_once()
        self.assertEqual({pp.status for pp in Post.objects.get().platform_posts.all()}, {"scheduled"})

    def test_accion_invalida(self):
        r = self.revision()
        self.assertEqual(self.apply(r["post_id"], accion="x").status_code, 400)


class ImportReviewPermissionTests(ImportBase):
    def as_editor(self):
        """Importa como dueño (necesita cuentas visibles) y baja a editor: sin publish_directly ni edit_others_posts."""
        WorkspaceMembership.objects.filter(user=self.user, workspace=self.workspace).update(
            workspace_role=WorkspaceMembership.WorkspaceRole.EDITOR
        )

    def apply(self, post_id, **data):
        url = reverse("composer:import_review", kwargs={"workspace_id": self.workspace.id})
        return self.client.post(url, {"post_id": post_id, **data})

    def imported(self):
        post_id = self.up(make_zip(), revisar="1").json()["revision"]["post_id"]
        self.as_editor()
        return post_id

    def test_publicar_ahora_sin_publish_directly_es_403(self):
        self.assertEqual(self.apply(self.imported(), accion="publicar", ninos="no").status_code, 403)

    def test_post_de_otro_autor_sin_edit_others_posts_es_403(self):
        other = User.objects.create_user(email="o@example.com", password="x", tos_accepted_at=timezone.now())
        post = Post.objects.create(workspace=self.workspace, author=other, caption="c")
        self.as_editor()
        self.assertEqual(self.apply(post.id, accion="programar").status_code, 403)

    def test_post_de_otro_espacio_es_404(self):
        ws2 = Workspace.objects.create(organization=self.org, name="WS2")
        post = Post.objects.create(workspace=ws2, author=self.user, caption="c")
        self.assertEqual(self.apply(post.id, accion="programar").status_code, 404)

    def test_aprobacion_obligatoria_bloquea_programar(self):
        post_id = self.imported()
        self.workspace.approval_workflow_mode = "required_internal"
        self.workspace.save()
        when = timezone.now() + timedelta(days=2)
        resp = self.apply(post_id, accion="programar", fecha=when.strftime("%Y-%m-%d"), hora="10:00")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("approval", resp.json()["errors"])

    def test_override_con_marcador_bloquea_programar(self):
        post_id = self.imported()
        PlatformPost.objects.filter(post_id=post_id, social_account=self.yt).update(
            platform_specific_caption="Hola {{nombre}}"
        )
        when = timezone.now() + timedelta(days=2)
        resp = self.apply(post_id, accion="programar", fecha=when.strftime("%Y-%m-%d"), hora="10:00")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("placeholders", resp.json()["errors"])
