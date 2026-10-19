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


LEGAL_PAGES = [
    ("/privacidad/", "es", "Política de privacidad", "CUND000714T89"),
    ("/terminos/", "es", "Términos de uso", "Jalisco"),
    ("/borrado-de-datos/", "es", "Borrado de datos", "dario.curiel@unx.mx"),
    ("/contacto/", "es", "Contacto", "Calle Diego Rivera 100"),
    ("/en/privacy/", "en", "Privacy Policy", "CUND000714T89"),
    ("/en/terms/", "en", "Terms of Service", "Jalisco"),
    ("/en/data-deletion/", "en", "Data deletion", "dario.curiel@unx.mx"),
    ("/en/contact/", "en", "Contact", "Calle Diego Rivera 100"),
]


@pytest.mark.parametrize(("url", "lang", "title", "marker"), LEGAL_PAGES)
def test_legal_pages_are_public_with_title_and_lang(client, url, lang, title, marker):
    response = client.get(url)
    html = response.content.decode()

    assert response.status_code == 200
    assert f'<html lang="{lang}">' in html
    assert f"<title>{title} | BEAN by ITHOS</title>" in html
    assert f"<h1>{title}</h1>" in html
    assert marker in html
    assert 'aria-current="page"' in html
    assert "<script>" not in html  # CSP: el JS vive en static/js/legal.js
    assert "AGPL-3.0" in html


def test_legal_language_switch_links_to_same_document(client):
    html = client.get("/privacidad/").content.decode()
    assert 'href="/en/privacy/" lang="en"' in html
    html = client.get("/en/data-deletion/").content.decode()
    assert 'href="/borrado-de-datos/" lang="es"' in html


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("/legal/", "/privacidad/"),
        ("/legal/privacy/", "/en/privacy/"),
        ("/legal/terms/", "/en/terms/"),
        ("/legal/support/", "/en/contact/"),
        ("/legal/data-deletion/", "/en/data-deletion/"),
    ],
)
def test_old_legal_urls_redirect_permanently(client, old, new):
    response = client.get(old)

    assert response.status_code == 301
    assert response["Location"] == new


def test_old_url_names_still_resolve():
    assert reverse("public_legal") == "/legal/"
    assert reverse("public_privacy") == "/legal/privacy/"
    assert reverse("meta_data_deletion_callback") == "/legal/data-deletion/callback/"
    assert reverse("public_data_deletion_status", args=["abc"]) == "/legal/data-deletion/status/abc/"


def test_landing_links_to_legal_and_shows_disclaimer(client):
    html = client.get("/").content.decode()

    assert 'href="/privacidad/"' in html and 'href="/borrado-de-datos/"' in html
    assert "no est&aacute; afiliado ni avalado por Meta, TikTok ni Google" in html


def test_404_renders_branded_page(client):
    response = client.get("/no-existe-xyz/")

    assert response.status_code == 404
    assert b"Volver al inicio" in response.content and b"Ir a Publicar" in response.content


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
    response = client.get(reverse("public_data_deletion"), follow=True)
    assert response.status_code == 200
    assert response.redirect_chain == [("/en/data-deletion/", 301)]
    assert b"bor-how" in response.content


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
def test_callback_matches_saved_meta_user_id_and_spares_other_orgs(client, fb_account, db):
    from apps.organizations.models import Organization

    fb_account.account_platform_id = "page-1"
    fb_account.platform_user_id = "meta-user-9"
    fb_account.save()
    other_ws = Workspace.objects.create(name="Other", organization=Organization.objects.create(name="Other org"))
    other = SocialAccount.objects.create(
        workspace=other_ws,
        platform="facebook",
        account_platform_id="page-2",
        platform_user_id="meta-user-8",
        account_name="Other",
        oauth_access_token="tok",
    )
    response = client.post(
        reverse("meta_deauthorize_callback"),
        {"signed_request": _signed({"algorithm": "HMAC-SHA256", "user_id": "meta-user-9"})},
    )
    assert response.status_code == 200
    fb_account.refresh_from_db()
    other.refresh_from_db()
    assert fb_account.connection_status == "archived"
    assert other.connection_status != "archived"


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
