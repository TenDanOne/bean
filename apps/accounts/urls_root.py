from django.urls import path

from . import views

urlpatterns = [
    path("review/", views.public_review, name="public_review"),
    path("legal/privacy/", views.public_privacy, name="public_privacy"),
    path("legal/terms/", views.public_terms, name="public_terms"),
    path("legal/support/", views.public_support, name="public_support"),
    path("legal/data-deletion/", views.public_data_deletion, name="public_data_deletion"),
    path("legal/data-deletion/callback/", views.meta_data_deletion_callback, name="meta_data_deletion_callback"),
    path(
        "legal/data-deletion/status/<str:code>/", views.public_data_deletion_status, name="public_data_deletion_status"
    ),
    path("legal/meta/deauthorize/", views.meta_deauthorize_callback, name="meta_deauthorize_callback"),
    path(
        "tiktokbTJ2ph9pXHrSfpXBB779Umzy76k70vQY.txt",
        views.tiktok_site_verification,
        name="tiktok_site_verification",
    ),
    path("", views.home, name="dashboard"),
]
