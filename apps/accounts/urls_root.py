from django.urls import path

from . import views

urlpatterns = [
    path("review/", views.public_review, name="public_review"),
    path("privacidad/", views.public_legal_doc, {"lang": "es", "doc": "privacy"}, name="legal_es_privacy"),
    path("terminos/", views.public_legal_doc, {"lang": "es", "doc": "terms"}, name="legal_es_terms"),
    path(
        "borrado-de-datos/",
        views.public_legal_doc,
        {"lang": "es", "doc": "data_deletion"},
        name="legal_es_data_deletion",
    ),
    path("contacto/", views.public_legal_doc, {"lang": "es", "doc": "contact"}, name="legal_es_contact"),
    path("en/privacy/", views.public_legal_doc, {"lang": "en", "doc": "privacy"}, name="legal_en_privacy"),
    path("en/terms/", views.public_legal_doc, {"lang": "en", "doc": "terms"}, name="legal_en_terms"),
    path(
        "en/data-deletion/",
        views.public_legal_doc,
        {"lang": "en", "doc": "data_deletion"},
        name="legal_en_data_deletion",
    ),
    path("en/contact/", views.public_legal_doc, {"lang": "en", "doc": "contact"}, name="legal_en_contact"),
    path("legal/", views.public_legal, name="public_legal"),
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
