"""Permission failures used to end at Django's bare error page.

There are 33 `raise PermissionDenied` in this codebase, each carrying a specific
reason, and none of them reached the user. Inside an HTMX swap it was worse: the
full error document — <html>, <head>, inline CSS — got spliced into whatever
panel made the request.
"""

import pytest
from django.core.exceptions import PermissionDenied
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.urls import path

from apps.common.middleware import HtmxErrorMiddleware
from apps.common.templatetags.common_extras import can


def _denied(request):
    raise PermissionDenied("Permission denied: reply_from_inbox")


urlpatterns = [path("denied/", _denied)]


# config/settings/test.py already runs with DEBUG=False, which is what makes
# Django serve the real error templates instead of its debug traceback.
@pytest.mark.urls(__name__)
class TestErrorPages:
    def test_403_renders_the_branded_page_with_the_reason(self, client):
        response = client.get("/denied/")

        assert response.status_code == 403
        body = response.content.decode()
        assert "Brightbean" in body
        # The specific reason is the whole point — a generic page is no better
        # than Django's.
        assert "reply_from_inbox" in body

    def test_404_names_the_path_that_missed(self, client):
        response = client.get("/no-such-page/")

        assert response.status_code == 404
        assert "/no-such-page/" in response.content.decode()


def test_500_template_renders_without_any_context():
    """Django's server_error view passes an empty context, so the page must not
    depend on context processors — they may be exactly what broke."""
    html = render_to_string("500.html", {})

    assert "Brightbean" in html
    assert "500" in html


class TestHtmxErrorMiddleware:
    def _response(self, status=403, content=b"<html><head></head><body>huge page</body></html>", html=True):
        from django.http import HttpResponse

        return HttpResponse(content, status=status, content_type="text/html" if html else "application/json")

    def _request(self, *, htmx=True, reason=""):
        request = RequestFactory().get("/x/")
        request.htmx = htmx
        if reason:
            request.permission_denied_reason = reason
        return request

    def test_error_page_is_replaced_on_htmx_requests(self):
        middleware = HtmxErrorMiddleware(lambda r: self._response())

        response = middleware(self._request())

        body = response.content.decode()
        assert "<html>" not in body
        assert "permission" in body.lower()
        # Status is preserved so HTMX still treats it as a failure.
        assert response.status_code == 403

    def test_reason_captured_from_the_exception_is_shown(self):
        middleware = HtmxErrorMiddleware(lambda r: self._response())

        response = middleware(self._request(reason="Insufficient workspace role."))

        assert "Insufficient workspace role." in response.content.decode()

    def test_process_exception_records_the_reason(self):
        middleware = HtmxErrorMiddleware(lambda r: self._response())
        request = self._request()

        assert middleware.process_exception(request, PermissionDenied("nope")) is None
        assert request.permission_denied_reason == "nope"

    def test_non_permission_exceptions_are_left_alone(self):
        middleware = HtmxErrorMiddleware(lambda r: self._response())
        request = self._request()

        middleware.process_exception(request, ValueError("unrelated"))

        assert not hasattr(request, "permission_denied_reason")

    def test_normal_requests_keep_the_full_page(self):
        middleware = HtmxErrorMiddleware(lambda r: self._response())

        response = middleware(self._request(htmx=False))

        assert "<html>" in response.content.decode()

    def test_successful_htmx_responses_are_untouched(self):
        middleware = HtmxErrorMiddleware(lambda r: self._response(status=200, content=b"<div>ok</div>"))

        response = middleware(self._request())

        assert response.content == b"<div>ok</div>"

    def test_json_errors_are_untouched(self):
        """The Agent API returns JSON errors; rewriting them would break clients."""
        middleware = HtmxErrorMiddleware(lambda r: self._response(content=b'{"detail":"nope"}', html=False))

        response = middleware(self._request())

        assert response.content == b'{"detail":"nope"}'


class TestCanFilter:
    def test_false_without_a_membership(self):
        request = RequestFactory().get("/")

        assert can(request, "reply_from_inbox") is False

    def test_reads_effective_permissions(self):
        request = RequestFactory().get("/")

        class Membership:
            effective_permissions = {"reply_from_inbox": True, "publish_directly": False}

        request.workspace_membership = Membership()

        assert can(request, "reply_from_inbox") is True
        assert can(request, "publish_directly") is False
        # An unknown key is denied rather than raising.
        assert can(request, "not_a_real_permission") is False
