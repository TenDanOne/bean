from django.urls import reverse

from apps.composer.tests.test_unsplash import ComposerTestCase


class TikTokComposerComplianceTests(ComposerTestCase):
    """TikTok Direct Post audit: consent next to the publish button and duration limit hint."""

    def test_compose_renders_consent_and_duration_hint(self):
        response = self.client.get(reverse("composer:compose", kwargs={"workspace_id": self.workspace.id}))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('id="tiktok-consent"', html)
        self.assertIn("Confirmación de uso de música", html)
        self.assertIn("TikTok acepta videos de hasta", html)
        # Consent must precede the action bar so it sits by the publish/schedule button.
        self.assertLess(html.index('id="tiktok-consent"'), html.index('id="publish-action-btn"'))
