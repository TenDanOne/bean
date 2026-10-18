import base64
import hashlib
import hmac
import json

import pytest
from django.test import override_settings
from django.urls import reverse

from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


def test_public_review_page_is_available_without_authentication(client):
    response = client.get(reverse("public_review"))

    assert response.status_code == 200
    assert b"BEAN" in response.content
    assert b"TikTok integration" in response.content


def test_public_legal_pages_are_available_without_authentication(client):
    for name, marker in (
        ("public_privacy", b"Privacy Policy"),
        ("public_terms", b"Terms of Service"),
        ("public_support", b"Support"),
    ):
        response = client.get(reverse(name))

        assert response.status_code == 200
        assert marker in response.content
        assert b"dario.curiel@unx.mx" in response.content


def test_tiktok_verification_file_is_served_at_domain_root(client):
    response = client.get(reverse("tiktok_site_verification"))

    assert response.status_code == 200
    assert response["Content-Type"] == "text/plain; charset=utf-8"
    assert response.content == b"tiktok-developers-site-verification=bTJ2ph9pXHrSfpXBB779Umzy76k70vQY\n"


# --- landing, data deletion and Meta callbacks ---

SECRET = "meta-test-secret"
META_SETTINGS = {"facebook": {"app_id": "1", "app_secret": SECRET}}


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _signed(payload, secret=SECRET):
    body = _b64(json.dumps(payload).encode())
    return _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()) + "." + body


def test_landing_is_public_and_dashboard_unchanged_when_authenticated(client, user):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Entrar" in response.content
    assert b"noindex" not in response.content

    client.force_login(user)
    response = client.get("/")
    assert b"Entrar" not in response.content


def test_review_keeps_noindex(client):
    assert b"noindex" in client.get(reverse("public_review")).content


def test_data_deletion_page_is_public(client):
    response = client.get(reverse("public_data_deletion"))
    assert response.status_code == 200
    assert b'id="es"' in response.content and b'id="en"' in response.content


@pytest.fixture
def fb_account(db, organization):
    return SocialAccount.objects.create(
        workspace=Workspace.objects.create(name="WS", organization=organization),
        platform="facebook",
        account_platform_id="12345",
        account_name="Page",
        oauth_access_token="tok",
    )


@override_settings(PLATFORM_CREDENTIALS_FROM_ENV=META_SETTINGS)
def test_deletion_callback_archives_account_and_returns_code(client, fb_account):
    response = client.post(
        reverse("meta_data_deletion_callback"),
        {"signed_request": _signed({"algorithm": "HMAC-SHA256", "user_id": "12345"})},
    )
    assert response.status_code == 200
    body = response.json()
    fb_account.refresh_from_db()
    assert fb_account.connection_status == "archived"
    assert fb_account.oauth_access_token == ""
    assert client.get(reverse("public_data_deletion_status", args=[body["confirmation_code"]])).status_code == 200
    assert body["url"].endswith(f"/{body['confirmation_code']}/")


@override_settings(PLATFORM_CREDENTIALS_FROM_ENV=META_SETTINGS)
def test_deletion_callback_rejects_bad_signature_and_algorithm(client, fb_account):
    url = reverse("meta_data_deletion_callback")
    bad = _signed({"algorithm": "HMAC-SHA256", "user_id": "12345"}, secret="other")
    assert client.post(url, {"signed_request": bad}).status_code == 400
    assert client.post(url, {"signed_request": _signed({"algorithm": "none", "user_id": "1"})}).status_code == 400
    assert client.post(url, {"signed_request": "garbage"}).status_code == 400
    assert client.post(url).status_code == 400
    fb_account.refresh_from_db()
    assert fb_account.connection_status == "connected"


@override_settings(PLATFORM_CREDENTIALS_FROM_ENV=META_SETTINGS)
def test_deletion_callback_unknown_user_still_returns_code(client, db):
    response = client.post(
        reverse("meta_data_deletion_callback"),
        {"signed_request": _signed({"algorithm": "HMAC-SHA256", "user_id": "nobody"})},
    )
    assert response.status_code == 200
    assert response.json()["confirmation_code"]


@override_settings(PLATFORM_CREDENTIALS_FROM_ENV={"facebook": {"app_id": "", "app_secret": ""}})
def test_deletion_callback_without_secret_is_503(client):
    assert client.post(reverse("meta_data_deletion_callback"), {"signed_request": "a.b"}).status_code == 503


@override_settings(PLATFORM_CREDENTIALS_FROM_ENV=META_SETTINGS)
def test_deauthorize_callback_archives_account(client, fb_account):
    response = client.post(
        reverse("meta_deauthorize_callback"),
        {"signed_request": _signed({"algorithm": "HMAC-SHA256", "user_id": "12345"})},
    )
    assert response.status_code == 200
    fb_account.refresh_from_db()
    assert fb_account.connection_status == "archived"


def test_deletion_status_invalid_code_is_404(client):
    assert client.get(reverse("public_data_deletion_status", args=["nope"])).status_code == 404
