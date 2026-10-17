from django.urls import path

from . import views

app_name = "onboarding"

# The connection-link flow used to live here too: a magic link that let someone
# outside BBS connect their own accounts without having an account. In BBS the
# owner connects every channel from inside, so that flow — and the parallel OAuth
# implementation it carried alongside the normal one — is gone. What remains is
# the get-started checklist, which is unrelated and still useful.
urlpatterns = [
    path(
        "<uuid:workspace_id>/checklist/",
        views.checklist_partial,
        name="checklist_partial",
    ),
    path(
        "<uuid:workspace_id>/checklist/dismiss/",
        views.dismiss_checklist,
        name="dismiss_checklist",
    ),
]
