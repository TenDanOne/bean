"""Lote A1: aprobación obligatoria, overrides, autosave, publicados, CSV, plantillas, medios."""

from datetime import timedelta
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import PlatformPost, Post, PostTemplate
from apps.composer.services import media_error_for_platform
from apps.composer.views import _read_csv_rows
from apps.members.models import ChannelAssignment, OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


class Base(TestCase):
    role = WorkspaceMembership.WorkspaceRole.OWNER

    def setUp(self):
        self.user = User.objects.create_user(email="u@example.com", password="x", tos_accepted_at=timezone.now())
        self.org = Organization.objects.create(name="Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="WS")
        OrgMembership.objects.create(user=self.user, organization=self.org, org_role=OrgMembership.OrgRole.OWNER)
        WorkspaceMembership.objects.create(user=self.user, workspace=self.workspace, workspace_role=self.role)
        self.client.force_login(self.user)
        self.fb = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="facebook",
            account_platform_id="fb1",
            account_name="FB",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.save_url = reverse("composer:save_post", kwargs={"workspace_id": self.workspace.id})

    def schedule(self, **extra):
        when = timezone.now() + timedelta(days=2)
        data = {
            "caption": "hola",
            "action": "schedule",
            "selected_accounts": str(self.fb.id),
            "scheduled_date": when.strftime("%Y-%m-%d"),
            "scheduled_time": "10:00",
            **extra,
        }
        return self.client.post(self.save_url, data)


class ApprovalGateTests(Base):
    role = WorkspaceMembership.WorkspaceRole.EDITOR

    def test_required_approval_blocks_schedule_without_publish_directly(self):
        self.workspace.approval_workflow_mode = "required_internal"
        self.workspace.save()
        resp = self.schedule()
        self.assertEqual(resp.status_code, 400)
        self.assertIn("aprobación", resp.json()["errors"]["approval"])
        self.assertFalse(Post.objects.exists())

    def test_required_approval_blocks_queue(self):
        self.workspace.approval_workflow_mode = "required_internal"
        self.workspace.save()
        resp = self.client.post(
            self.save_url, {"caption": "x", "action": "add_to_queue", "selected_accounts": str(self.fb.id)}
        )
        self.assertEqual(resp.status_code, 400)

    def test_without_required_approval_schedule_passes_guard(self):
        self.assertEqual(self.schedule().status_code in (200, 204, 302), True)


class OverridesPersistTests(Base):
    def test_override_kept_when_field_not_submitted_and_cleared_when_blank(self):
        data = {"caption": "c", "selected_accounts": str(self.fb.id), f"override_caption_{self.fb.id}": "solo FB"}
        self.client.post(self.save_url, data)
        pp = PlatformPost.objects.get()
        self.assertEqual(pp.platform_specific_caption, "solo FB")
        edit = reverse("composer:save_post_edit", kwargs={"workspace_id": self.workspace.id, "post_id": pp.post_id})
        self.client.post(edit, {"caption": "c", "selected_accounts": str(self.fb.id)})
        pp.refresh_from_db()
        self.assertEqual(pp.platform_specific_caption, "solo FB")
        self.client.post(
            edit, {"caption": "c", "selected_accounts": str(self.fb.id), f"override_caption_{self.fb.id}": ""}
        )
        pp.refresh_from_db()
        self.assertIsNone(pp.platform_specific_caption)

    def test_compose_preloads_overrides(self):
        post = Post.objects.create(workspace=self.workspace, author=self.user, caption="c")
        PlatformPost.objects.create(post=post, social_account=self.fb, platform_specific_caption="solo FB")
        resp = self.client.get(
            reverse("composer:compose_edit", kwargs={"workspace_id": self.workspace.id, "post_id": post.id})
        )
        self.assertEqual(resp.context["platform_overrides"][str(self.fb.id)]["caption"], "solo FB")


class AutosaveScopeTests(Base):
    def test_autosave_rejects_account_from_other_workspace(self):
        other_ws = Workspace.objects.create(organization=self.org, name="Otro")
        foreign = SocialAccount.objects.create(
            workspace=other_ws,
            platform="facebook",
            account_platform_id="fb2",
            account_name="Foreign",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        url = reverse("composer:autosave", kwargs={"workspace_id": self.workspace.id})
        self.client.post(url, {"caption": "x", "selected_accounts": f"{foreign.id},{self.fb.id}"})
        accounts = set(PlatformPost.objects.values_list("social_account_id", flat=True))
        self.assertEqual(accounts, {self.fb.id})

    def test_published_post_is_read_only(self):
        post = Post.objects.create(workspace=self.workspace, author=self.user, caption="original")
        PlatformPost.objects.create(post=post, social_account=self.fb, status="published")
        kw = {"workspace_id": self.workspace.id, "post_id": post.id}
        self.client.post(reverse("composer:autosave_edit", kwargs=kw), {"caption": "cambiado"})
        resp = self.client.post(reverse("composer:save_post_edit", kwargs=kw), {"caption": "cambiado"})
        self.assertEqual(resp.status_code, 400)
        post.refresh_from_db()
        self.assertEqual(post.caption, "original")


class CsvTests(Base):
    def test_cp1252_and_semicolon(self):
        raw = "fecha;plataforma;texto\n2030-01-01;facebook;añejo ñandú\n".encode("cp1252")
        rows = _read_csv_rows(raw)
        self.assertEqual(rows[1], ["2030-01-01", "facebook", "añejo ñandú"])

    def _import(self, csv_text, mapping):
        up = SimpleUploadedFile("a.csv", csv_text.encode("cp1252"), content_type="text/csv")
        self.client.post(reverse("composer:csv_upload", kwargs={"workspace_id": self.workspace.id}), {"csv_file": up})
        self.client.post(
            reverse("composer:csv_preview", kwargs={"workspace_id": self.workspace.id}),
            {f"map_{k}": v for k, v in mapping.items()},
        )
        return self.client.post(reverse("composer:csv_confirm_import", kwargs={"workspace_id": self.workspace.id}))

    def test_import_skips_rows_without_accounts_and_reports(self):
        resp = self._import(
            "fecha;plataforma;texto\n2030-01-01;facebook;uno\n2030-01-02;;dos\n",
            {"date": 0, "platforms": 1, "caption": 2},
        )
        self.assertEqual(Post.objects.count(), 1)
        self.assertEqual(resp.context["error_count"], 1)
        self.assertEqual(resp.context["row_errors"][0]["row"], 3)

    def test_required_approval_imports_dated_rows_as_drafts(self):
        self.workspace.approval_workflow_mode = "required_internal"
        self.workspace.save()
        WorkspaceMembership.objects.filter(user=self.user).update(workspace_role="editor")
        for m in WorkspaceMembership.objects.filter(user=self.user):
            ChannelAssignment.objects.create(membership=m, social_account=self.fb, can_publish=True)
        self._import("fecha;plataforma;texto\n2030-01-01;facebook;uno\n", {"date": 0, "platforms": 1, "caption": 2})
        pp = PlatformPost.objects.get()
        self.assertEqual(pp.status, "draft")
        self.assertIsNotNone(pp.post.proposed_publish_at)


class TemplateApplyTests(Base):
    def test_template_applies_accounts_and_first_comment(self):
        tpl = PostTemplate.objects.create(
            workspace=self.workspace,
            name="t",
            template_data={"caption": "cap", "first_comment": "primero", "platform_account_ids": [str(self.fb.id)]},
        )
        resp = self.client.get(
            reverse("composer:compose", kwargs={"workspace_id": self.workspace.id}) + f"?template={tpl.id}"
        )
        self.assertEqual(resp.context["selected_account_ids"], [str(self.fb.id)])
        self.assertEqual(resp.context["form"].initial["first_comment"], "primero")


class MediaValidationTests(SimpleTestCase):
    def test_rules(self):
        self.assertIn("video", media_error_for_platform("youtube", []))
        self.assertIn("imágenes", media_error_for_platform("tiktok", ["image"]))
        self.assertIsNone(media_error_for_platform("tiktok", ["video"]))
        self.assertIn("Instagram", media_error_for_platform("instagram", []))
        self.assertIsNone(media_error_for_platform("instagram", ["image"]))
        self.assertIsNone(media_error_for_platform("facebook", []))


class ScheduleMediaGuardTests(Base):
    def test_instagram_without_media_cannot_be_scheduled(self):
        ig = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="instagram",
            account_platform_id="ig1",
            account_name="IG",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        with patch("apps.composer.views.PublishEngine"):
            resp = self.schedule(selected_accounts=str(ig.id))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Instagram", resp.json()["errors"]["media"])
