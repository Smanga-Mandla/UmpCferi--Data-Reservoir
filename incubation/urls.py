from django.urls import path

from incubation import views

app_name = "incubation"

urlpatterns = [
    path("", views.incubatee_list, name="list"),
    path("add/", views.incubatee_create, name="create"),
    path("import/", views.incubatee_import, name="import"),
    path("check-ins/", views.checkin_list, name="checkin_list"),
    path(
        "check-in/<int:link_id>/",
        views.public_checkin,
        name="public_checkin",
    ),
    path("<uuid:incubatee_id>/", views.incubatee_detail, name="detail"),
    path("<uuid:incubatee_id>/edit/", views.incubatee_update, name="update"),
    path("<uuid:incubatee_id>/delete/", views.incubatee_delete, name="delete"),
    path(
        "<uuid:incubatee_id>/onboarding/<int:step>/",
        views.onboarding_step,
        name="onboarding_step",
    ),
    path(
        "<uuid:incubatee_id>/assignments/",
        views.assignment_update,
        name="assignments",
    ),
    path(
        "<uuid:incubatee_id>/status/",
        views.lifecycle_update,
        name="status",
    ),
    path(
        "<uuid:incubatee_id>/documents/add/",
        views.incubatee_document_add,
        name="document_add",
    ),
    path(
        "<uuid:incubatee_id>/documents/<uuid:document_id>/download/",
        views.incubatee_document_download,
        name="document_download",
    ),
    path(
        "<uuid:incubatee_id>/evidence/<str:evidence_type>/<int:evidence_id>/download/",
        views.evidence_download,
        name="evidence_download",
    ),
    path(
        "<uuid:incubatee_id>/quarterly-reviews/add/",
        views.quarterly_review_add,
        name="quarterly_review_add",
    ),
    path(
        "<uuid:incubatee_id>/milestones/add/",
        views.milestone_add,
        name="milestone_add",
    ),
    path(
        "<uuid:incubatee_id>/mentoring-sessions/add/",
        views.mentoring_session_add,
        name="mentoring_session_add",
    ),
    path(
        "<uuid:incubatee_id>/exit/add/",
        views.exit_record_add,
        name="exit_add",
    ),
    path(
        "<uuid:incubatee_id>/follow-ups/<int:followup_id>/",
        views.followup_submit,
        name="followup_submit",
    ),
]
