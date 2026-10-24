from django.urls import path

from partners import views

app_name = "partners"

urlpatterns = [
    path("", views.partner_list, name="partner_list"),
    path("add/", views.partner_create, name="partner_create"),
    path("mous/", views.mou_list, name="mou_list"),
    path("mous/add/", views.mou_create, name="mou_create"),
    path("mous/alerts/", views.mou_expiry_alerts, name="mou_expiry_alerts"),
    path("mous/<uuid:mou_id>/", views.mou_detail, name="mou_detail"),
    path("mous/<uuid:mou_id>/edit/", views.mou_update, name="mou_update"),
    path("mous/<uuid:mou_id>/delete/", views.mou_delete, name="mou_delete"),
    path("mous/<uuid:mou_id>/history/", views.mou_history, name="mou_history"),
    path("mous/<uuid:mou_id>/attachments/add/", views.mou_attachment_add, name="attachment_add"),
    path(
        "mous/<uuid:mou_id>/attachments/<uuid:attachment_id>/download/",
        views.mou_attachment_download,
        name="attachment_download",
    ),
    path("mous/<uuid:mou_id>/obligations/add/", views.mou_obligation_form, name="obligation_add"),
    path(
        "mous/<uuid:mou_id>/obligations/<int:obligation_id>/edit/",
        views.mou_obligation_form,
        name="obligation_update",
    ),
    path(
        "mous/<uuid:mou_id>/obligations/<int:obligation_id>/delete/",
        views.mou_obligation_delete,
        name="obligation_delete",
    ),
    path(
        "<uuid:partner_id>/images/<uuid:image_id>/",
        views.partner_image,
        name="partner_image",
    ),
    path("<uuid:partner_id>/", views.partner_detail, name="partner_detail"),
    path("<uuid:partner_id>/edit/", views.partner_update, name="partner_update"),
    path("<uuid:partner_id>/delete/", views.partner_delete, name="partner_delete"),
    path(
        "<uuid:partner_id>/representatives/add/",
        views.representative_add,
        name="representative_add",
    ),
    path(
        "<uuid:partner_id>/representatives/<int:stakeholder_id>/remove/",
        views.representative_remove,
        name="representative_remove",
    ),
]
