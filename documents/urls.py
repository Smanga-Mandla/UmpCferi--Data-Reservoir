from django.urls import path

from documents import views

app_name = "documents"

urlpatterns = [
    path("", views.document_list, name="list"),
    path("upload/", views.document_create, name="create"),
    path("compliance/", views.compliance_status, name="compliance"),
    path("alerts/", views.expiry_alert_list, name="alerts"),
    path("<uuid:document_id>/", views.document_detail, name="detail"),
    path("<uuid:document_id>/replace/", views.document_replace, name="replace"),
    path("<uuid:document_id>/archive/", views.document_archive, name="archive"),
    path("<uuid:document_id>/versions/", views.document_versions, name="versions"),
    path(
        "<uuid:document_id>/versions/<uuid:version_id>/download/",
        views.document_download,
        name="version_download",
    ),
    path("<uuid:document_id>/download/", views.document_download, name="download"),
]
