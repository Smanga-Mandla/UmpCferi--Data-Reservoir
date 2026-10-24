from django.urls import path

from interactions import views

app_name = "interactions"

urlpatterns = [
    path("", views.interaction_list, name="list"),
    path("add/", views.interaction_create, name="create"),
    path("<int:interaction_id>/", views.interaction_detail, name="detail"),
    path("<int:interaction_id>/edit/", views.interaction_update, name="update"),
    path("<int:interaction_id>/delete/", views.interaction_delete, name="delete"),
    path(
        "<int:interaction_id>/tasks/add/",
        views.follow_up_task_add,
        name="task_add",
    ),
    path(
        "<int:interaction_id>/tasks/<int:task_id>/update/",
        views.follow_up_task_update,
        name="task_update",
    ),
    path(
        "<int:interaction_id>/attendance/add/",
        views.attendance_add,
        name="attendance_add",
    ),
    path(
        "<int:interaction_id>/attendance/<int:attendance_id>/update/",
        views.attendance_update,
        name="attendance_update",
    ),
]
