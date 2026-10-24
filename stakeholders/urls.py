from django.urls import path

from stakeholders import views

app_name = "stakeholders"

urlpatterns = [
    path("", views.stakeholder_list, name="list"),
    path("add/", views.stakeholder_create, name="create"),
    path("<int:stakeholder_id>/", views.stakeholder_detail, name="detail"),
    path("<int:stakeholder_id>/edit/", views.stakeholder_update, name="update"),
    path(
        "<int:stakeholder_id>/consent/add/",
        views.consent_add,
        name="consent_add",
    ),
    path("<int:stakeholder_id>/delete/", views.stakeholder_delete, name="delete"),
]
