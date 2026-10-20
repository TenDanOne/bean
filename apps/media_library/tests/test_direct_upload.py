"""Subida directa a R2 desde el navegador: presign + finalize (boto3/presign simulados)."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.media_library.models import MediaAsset, PendingUpload
from apps.media_library.quotas import StorageQuotaExceededError
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

MP4 = {"size": 1000, "media_type": "video", "mime": "video/mp4"}


class DirectUploadTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="u@example.com", password="x", tos_accepted_at=timezone.now())
        self.org = Organization.objects.create(name="Org")
        self.ws = Workspace.objects.create(organization=self.org, name="WS")
        OrgMembership.objects.create(user=self.user, organization=self.org, org_role=OrgMembership.OrgRole.OWNER)
        WorkspaceMembership.objects.create(
            user=self.user, workspace=self.ws, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        self.client.force_login(self.user)
        kw = {"workspace_id": self.ws.id}
        self.presign = reverse("media_library:upload_presign", kwargs=kw)
        self.finalize = reverse("media_library:upload_finalize", kwargs=kw)

    def _presign(self, **body):
        body = {"filename": "clip.mp4", "content_type": "video/mp4", "size": 1000, **body}
        return self.client.post(self.presign, body, content_type="application/json")

    def test_local_storage_answers_409_so_the_browser_falls_back(self):
        resp = self._presign()
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.json()["fallback"])

    @patch("apps.media_library.storage.is_s3_backend", return_value=True)
    @patch("apps.media_library.storage.presign_upload")
    def test_presign_returns_url_fields_and_records_pending(self, presign, _s3):
        presign.return_value = {"method": "POST", "url": "https://r2.example/bucket", "fields": {"key": "k"}}
        resp = self._presign()
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["url"], "https://r2.example/bucket")
        self.assertTrue(PendingUpload.objects.filter(id=data["upload_id"], workspace=self.ws).exists())

    @patch("apps.media_library.storage.is_s3_backend", return_value=True)
    def test_presign_validates_type_and_size_up_front(self, _s3):
        self.assertEqual(self._presign(content_type="text/html").status_code, 400)
        self.assertEqual(self._presign(size=5 * 1024**4).status_code, 400)
        self.assertEqual(self._presign(filename="").status_code, 400)

    def _pending(self, **kw):
        return PendingUpload.objects.create(
            organization=self.org,
            workspace=self.ws,
            created_by=self.user,
            storage_key="media_library/2026/10/abc.mp4",
            declared_content_type="video/mp4",
            declared_filename="clip.mp4",
            max_bytes=10**9,
            expires_at=kw.pop("expires_at", timezone.now() + timedelta(minutes=10)),
            **kw,
        )

    @patch("apps.media_library.views.process_media_asset")
    @patch("apps.media_library.services.inspect_uploaded_object", return_value=MP4)
    def test_finalize_creates_asset_once_and_queues_processing(self, _inspect, process):
        p = self._pending()
        first = self.client.post(self.finalize, {"upload_id": str(p.id)})
        again = self.client.post(self.finalize, {"upload_id": str(p.id)})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["id"], again.json()["id"])
        self.assertEqual(MediaAsset.objects.count(), 1)
        self.assertEqual(process.call_count, 2)

    @patch(
        "apps.media_library.services.inspect_uploaded_object",
        side_effect=StorageQuotaExceededError(used=1, limit=2, attempted=3),
    )
    def test_finalize_quota_is_413(self, _inspect):
        p = self._pending()
        self.assertEqual(self.client.post(self.finalize, {"upload_id": str(p.id)}).status_code, 413)
        self.assertFalse(MediaAsset.objects.exists())

    @patch("apps.media_library.services.inspect_uploaded_object", side_effect=FileNotFoundError("nunca llegó"))
    def test_finalize_missing_object_is_400(self, _inspect):
        p = self._pending()
        resp = self.client.post(self.finalize, {"upload_id": str(p.id)})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("nunca llegó", resp.json()["error"])

    def test_finalize_expired_unknown_and_foreign_workspace(self):
        expired = self._pending(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.client.post(self.finalize, {"upload_id": str(expired.id)}).status_code, 400)
        self.assertEqual(self.client.post(self.finalize, {"upload_id": "no-es-uuid"}).status_code, 400)
        other = Workspace.objects.create(organization=self.org, name="Otro")
        foreign = PendingUpload.objects.create(
            organization=self.org,
            workspace=other,
            storage_key="media_library/x.mp4",
            declared_filename="x.mp4",
            max_bytes=1,
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        self.assertEqual(self.client.post(self.finalize, {"upload_id": str(foreign.id)}).status_code, 404)

    @patch("apps.media_library.tasks.process_media_asset")
    @patch("apps.media_library.services.inspect_uploaded_object", return_value=MP4)
    def test_composer_upload_media_accepts_an_upload_id(self, _inspect, _process):
        p = self._pending()
        url = reverse("composer:upload_media", kwargs={"workspace_id": self.ws.id})
        resp = self.client.post(url, {"upload_id": str(p.id)})
        self.assertEqual(resp.status_code, 200, resp.content)
        asset = MediaAsset.objects.get()
        self.assertEqual(resp["X-Uploaded-Asset-Id"], str(asset.id))
        self.assertEqual(self.client.session[f"pending_media_{self.ws.id}"], [str(asset.id)])
