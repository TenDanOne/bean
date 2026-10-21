"""Importador: endurecimiento (JSON hostil, medios por red, conversión para TikTok, espacio y autor)."""

import json
from unittest.mock import MagicMock, patch

from django.core.files.base import ContentFile
from django.urls import reverse

from apps.accounts.models import User
from apps.composer.models import PlatformPost, Post, PostMedia
from apps.composer.tests.test_import import ImportBase, make_zip, piece, png
from apps.media_library.models import MediaAsset
from apps.members.models import ChannelAssignment, OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.publisher.engine import PublishEngine
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from providers.types import PostType


class HardeningTests(ImportBase):
    def test_unused_media_of_skipped_network_does_not_break_import(self):
        p = piece(
            id="k1", tipo="carrusel", medios=["a.png", "b.png"], medios_por_red={"instagram": ["a.png"]},
            redes={"instagram": {"tipo": "carrusel", "descripcion": "x"}, "youtube": {"hecho_para_ninos": False}},
        )  # fmt: skip
        resp = self.up(make_zip([p], {"a.png": png(), "b.png": png((4, 4, 4))}))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(Post.objects.get().media_attachments.count(), 1)

    def test_deeply_nested_json_is_a_clean_error(self):
        e = self.err(self.up(make_zip(raw="[" * 200_000 + "]" * 200_000)))
        self.assertIn("no se puede leer", e["titulo"])

    def test_infinity_and_nan_are_rejected(self):
        base = json.dumps({"version": 1, "proyecto": "x", "piezas": [piece()]})
        bad = base.replace('"portada_seg": 9.6', '"portada_seg": Infinity')
        self.assertIn("portada_seg", self.err(self.up(make_zip(raw=bad)))["mensaje"])
        m = json.dumps({"version": 1, "proyecto": "x", "piezas": [piece(medios=[{"ruta": "v.mp4", "duracion_s": 0}])]})
        bad = m.replace('"duracion_s": 0', '"duracion_s": NaN')
        self.assertIn("duracion_s", self.err(self.up(make_zip(raw=bad)))["mensaje"])

    def test_tiktok_photos_convert_png_to_jpeg_and_skip_duet_stitch(self):
        p = piece(
            id="t1", tipo="imagen", medios=["a.png"], medios_por_red={"instagram": ["a.png"], "tiktok": ["a.png"]},
            redes={"instagram": {"tipo": "feed", "descripcion": "x"}, "tiktok": {"tipo": "fotos", "titulo": "T"}},
        )  # fmt: skip
        resp = self.up(make_zip([p], {"a.png": png()}))
        self.assertEqual(resp.status_code, 200, resp.content)
        by = {pp.social_account.platform: pp for pp in PlatformPost.objects.all()}
        self.assertEqual(MediaAsset.objects.count(), 1)  # un solo JPEG compartido por Instagram y TikTok
        self.assertEqual(MediaAsset.objects.get(id=by["tiktok"].platform_specific_media[0]).mime_type, "image/jpeg")
        self.assertEqual(by["instagram"].platform_specific_media, by["tiktok"].platform_specific_media)
        self.assertNotIn("disable_duet", by["tiktok"].platform_extra)
        self.assertNotIn("disable_stitch", by["tiktok"].platform_extra)

    def test_tiktok_only_png_makes_a_single_jpeg_asset(self):
        p = piece(id="t2", tipo="imagen", medios=["a.png"], redes={"tiktok": {"tipo": "fotos", "titulo": "T"}})
        self.assertEqual(self.up(make_zip([p], {"a.png": png()})).status_code, 200)
        self.assertEqual([a.mime_type for a in MediaAsset.objects.all()], ["image/jpeg"])
        self.assertEqual(Post.objects.get().media_attachments.count(), 1)


class InstagramJpegTests(ImportBase):
    def test_import_png_facebook_keeps_original_instagram_gets_jpeg(self):
        p = piece(
            id="j1", tipo="imagen", medios=["a.png"],
            redes={"instagram": {"tipo": "feed", "descripcion": "x"}, "facebook": {"tipo": "feed", "descripcion": "x"}},
        )  # fmt: skip
        self.assertEqual(self.up(make_zip([p], {"a.png": png()})).status_code, 200)
        by = {pp.social_account.platform: pp for pp in PlatformPost.objects.all()}
        mime = lambda pp: MediaAsset.objects.get(id=pp.platform_specific_media[0]).mime_type  # noqa: E731
        self.assertEqual((mime(by["instagram"]), mime(by["facebook"])), ("image/jpeg", "image/png"))

    @patch("apps.publisher.engine.media_error_for_platform", return_value=None)
    @patch("apps.publisher.engine.get_provider")
    @patch("apps.publisher.engine._resolve_publish_credentials", return_value={})
    def test_publish_png_reaches_instagram_as_jpeg_once_and_facebook_as_png(self, _creds, get_provider, _err):
        fb = self.fb
        asset = MediaAsset.objects.create(
            workspace=self.workspace, organization=self.org, filename="a.png", mime_type="image/png",
            media_type="image", uploaded_by=self.user,
        )  # fmt: skip
        asset.file.save("a.png", ContentFile(png()))
        post = Post.objects.create(workspace=self.workspace, author=self.user, caption="x")
        PostMedia.objects.create(post=post, media_asset=asset, position=0)
        ig_pp, fb_pp = (
            PlatformPost.objects.create(post=post, social_account=a, status="scheduled") for a in (self.ig, fb)
        )
        sent = {}

        def publish(_token, content):
            sent.setdefault(content.media_urls[0].rsplit(".", 1)[-1], 0)
            sent[content.media_urls[0].rsplit(".", 1)[-1]] += 1
            return MagicMock(platform_post_id="1", url="u", extra={})

        provider = MagicMock(supported_post_types=[PostType.IMAGE], publish_post=publish)
        get_provider.return_value = provider
        engine = PublishEngine()
        engine._dispatch_to_provider(fb_pp)
        engine._dispatch_to_provider(ig_pp)
        engine._dispatch_to_provider(ig_pp)  # reintento: no vuelve a convertir
        self.assertEqual(sent, {"png": 1, "jpg": 2})
        self.assertEqual(MediaAsset.objects.filter(mime_type="image/jpeg").count(), 1)


class OwnMediaSyncTests(ImportBase):
    def _imported(self):
        p = piece(id="s1", tipo="imagen", medios=["a.png"], redes={"instagram": {"tipo": "feed", "descripcion": "x"}})
        self.up(make_zip([p], {"a.png": png()}))
        return Post.objects.get(), PlatformPost.objects.get()

    def test_manual_add_and_remove_update_per_network_lists(self):
        post, pp = self._imported()
        extra = MediaAsset.objects.create(
            workspace=self.workspace, organization=self.org, filename="n.png",
            file="media_library/n.png", media_type="image", uploaded_by=self.user,
        )  # fmt: skip
        url = reverse("composer:attach_media", args=[self.workspace.id, post.id])
        r = self.client.post(url, {"media_asset_id": str(extra.id)})
        self.assertEqual(r.status_code, 200, r.content)
        pp.refresh_from_db()
        self.assertIn(str(extra.id), pp.platform_specific_media)
        pm = PostMedia.objects.get(post=post, media_asset=extra)
        self.client.post(reverse("composer:remove_media", args=[self.workspace.id, post.id, pm.id]))
        pp.refresh_from_db()
        self.assertNotIn(str(extra.id), pp.platform_specific_media)
        self.assertEqual(len(pp.platform_specific_media), 1)

    def test_engine_sends_only_the_network_own_media(self):
        assets = []
        for n in ("1.mp4", "2.mp4"):
            a = MediaAsset(
                workspace=self.workspace, organization=self.org, filename=n, media_type="video", uploaded_by=self.user
            )
            a.file.save(n, ContentFile(n.encode() * 5), save=True)
            assets.append(a)
        post = Post.objects.create(workspace=self.workspace, author=self.user, caption="c")
        for i, a in enumerate(assets):
            PostMedia.objects.create(post=post, media_asset=a, position=i)
        pp = PlatformPost.objects.create(
            post=post, social_account=self.yt, status="publishing", platform_specific_media=[str(assets[1].id)]
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
        content = provider.publish_post.call_args.args[1]
        self.assertEqual(len(content.media_files), 1)
        self.assertTrue(content.media_urls[0].endswith(assets[1].file.url))


class WorkspaceAndAuthorTests(ImportBase):
    def setUp(self):
        super().setUp()
        self.workspace.name = "Actual"
        self.workspace.save()

    def _other_ws(self, org, role):
        ws = Workspace.objects.create(organization=org, name="WS")
        WorkspaceMembership.objects.create(user=self.user, workspace=ws, workspace_role=role)
        return ws

    def _suggest(self):
        m = {"version": 1, "proyecto": "simu", "espacio_sugerido": "WS", "piezas": [piece()]}
        return make_zip(manifest=m)

    def test_suggested_workspace_is_used_when_member_with_permissions(self):
        target = self._other_ws(self.org, WorkspaceMembership.WorkspaceRole.OWNER)
        SocialAccount.objects.create(
            workspace=target,
            platform="youtube",
            account_platform_id="y2",
            account_name="y",
            connection_status="connected",
        )
        resp = self.up(self._suggest())
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(Post.objects.get().workspace_id, target.id)
        self.assertIn(str(target.id), resp.json()["redirect"])

    def test_suggested_workspace_without_permissions_falls_back_to_current(self):
        self._other_ws(self.org, WorkspaceMembership.WorkspaceRole.VIEWER)
        self.assertEqual(self.up(self._suggest()).status_code, 200)
        self.assertEqual(Post.objects.get().workspace_id, self.workspace.id)

    def test_suggested_workspace_of_another_org_is_ignored(self):
        other_org = Organization.objects.create(name="Otra")
        OrgMembership.objects.create(user=self.user, organization=other_org, org_role=OrgMembership.OrgRole.OWNER)
        self._other_ws(other_org, WorkspaceMembership.WorkspaceRole.OWNER)
        self.assertEqual(self.up(self._suggest()).status_code, 200)
        self.assertEqual(Post.objects.get().workspace_id, self.workspace.id)


class OtherAuthorReimportTests(ImportBase):
    role = WorkspaceMembership.WorkspaceRole.EDITOR

    def setUp(self):
        super().setUp()
        m = WorkspaceMembership.objects.get(user=self.user, workspace=self.workspace)
        for acc in (self.yt, self.ig, self.tt, self.fb):  # el editor solo ve los canales que le asignan
            ChannelAssignment.objects.create(membership=m, social_account=acc, can_publish=True)

    def test_cannot_overwrite_a_draft_of_another_author(self):
        author = User.objects.create_user(email="a@example.com", password="x")
        WorkspaceMembership.objects.create(user=author, workspace=self.workspace, workspace_role="owner")
        self.assertEqual(self.up(make_zip()).status_code, 200)
        mine = Post.objects.get()
        Post.objects.filter(id=mine.id).update(author=author)  # el borrador existente es de otra persona
        perms = WorkspaceMembership.objects.get(user=self.user, workspace=self.workspace).effective_permissions
        self.assertFalse(perms.get("edit_others_posts"))
        resp = self.up(make_zip())  # no puede editarlo: no ofrece «existe» y crea uno nuevo
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(Post.objects.count(), 2)
        mine.refresh_from_db()
        self.assertEqual(mine.author_id, author.pk)
