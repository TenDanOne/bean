from django.urls import path

from . import views

urlpatterns = [
    path("review/", views.public_review, name="public_review"),
    path("legal/privacy/", views.public_privacy, name="public_privacy"),
    path("legal/terms/", views.public_terms, name="public_terms"),
    path("legal/support/", views.public_support, name="public_support"),
    path(
        "tiktokbTJ2ph9pXHrSfpXBB779Umzy76k70vQY.txt",
        views.tiktok_site_verification,
        name="tiktok_site_verification",
    ),
    path("", views.dashboard, name="dashboard"),
]
