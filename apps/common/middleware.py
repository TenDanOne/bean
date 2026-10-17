"""Cross-cutting request middleware."""

import logging

from django.core.exceptions import PermissionDenied
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)

# Error responses worth rewriting for HTMX. 404 is included because a stale
# panel link is a common way to hit one.
_HTMX_ERROR_STATUSES = {403, 404, 500}

_HTMX_ERROR_MESSAGES = {
    403: "You don't have permission to do this.",
    404: "That's no longer there — refresh the page.",
    500: "Something went wrong. The details are in the server log.",
}


class HtmxErrorMiddleware:
    """Swap full error pages for a compact block on HTMX requests.

    Django's error views return a whole HTML document. On a normal navigation
    that's right; inside an HTMX swap it means a complete page — <html>, <head>,
    inline CSS and all — gets spliced into whatever panel made the request. The
    user sees a mangled layout instead of an error.

    Only the body is replaced; the status code is left alone so HTMX still
    treats the response as a failure and callers that inspect it keep working.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def process_exception(self, request, exception):
        """Stash a PermissionDenied's message before Django turns it into a 403.

        By the time the response exists the exception is gone, and every
        ``raise PermissionDenied`` in this codebase carries a specific reason
        worth showing. Returning None leaves normal exception handling alone.
        """
        if isinstance(exception, PermissionDenied):
            request.permission_denied_reason = str(exception)
        return None

    def __call__(self, request):
        response = self.get_response(request)

        if not getattr(request, "htmx", False):
            return response
        if response.status_code not in _HTMX_ERROR_STATUSES:
            return response
        # Anything that isn't a rendered HTML page (a JSON API error, a
        # streaming response) is left untouched.
        if not response.get("Content-Type", "").startswith("text/html"):
            return response
        if getattr(response, "streaming", False):
            return response

        detail = _extract_permission_detail(request, response.status_code)
        html = render_to_string(
            "errors/_htmx_error.html",
            {
                "status_code": response.status_code,
                "message": _HTMX_ERROR_MESSAGES.get(response.status_code, "Request failed."),
                "detail": detail,
            },
            request=request,
        )
        response.content = html.encode("utf-8")
        response["Content-Length"] = str(len(response.content))
        return response


def _extract_permission_detail(request, status_code):
    """The specific reason a 403 was raised, when one was recorded.

    ``PermissionDenied`` is raised with a useful message all over this codebase;
    the exception itself isn't reachable from here, so views that want the detail
    surfaced set it on the request.
    """
    if status_code != 403:
        return ""
    return getattr(request, "permission_denied_reason", "")
