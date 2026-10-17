from django.urls import reverse


def test_public_review_page_is_available_without_authentication(client):
    response = client.get(reverse("public_review"))

    assert response.status_code == 200
    assert b"BrightBean Studio" in response.content
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
