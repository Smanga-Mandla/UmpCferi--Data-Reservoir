from django.urls import path

from reporting.views import (
    DashboardIndexView,
    ImpactDashboardView,
    RoleDashboardView,
    analytics_api,
    audit_log,
    dashboard_export,
    impact_export,
    interaction_export,
    list_export,
)

app_name = "reporting"

urlpatterns = [
    path("", DashboardIndexView.as_view(), name="dashboard"),
    path("impact/", ImpactDashboardView.as_view(), name="impact"),
    path("impact/export/<str:format_name>/", impact_export, name="impact_export"),
    path(
        "dashboard/export/<str:role_code>/<str:format_name>/",
        dashboard_export,
        name="dashboard_export",
    ),
    path(
        "interactions/export/<str:format_name>/",
        interaction_export,
        name="interaction_export",
    ),
    path(
        "lists/<str:dataset>/export/<str:format_name>/",
        list_export,
        name="list_export",
    ),
    path("analytics.json", analytics_api, name="analytics_api"),
    path("audit/", audit_log, name="audit_log"),
    path("super-admin/", RoleDashboardView.as_view(role_code="super_admin"), name="super_admin"),
    path("system-admin/", RoleDashboardView.as_view(role_code="system_admin"), name="system_admin"),
    path("admin/", RoleDashboardView.as_view(role_code="admin"), name="admin"),
    path("board-member/", RoleDashboardView.as_view(role_code="board_member"), name="board_member"),
    path("cferi-staff/", RoleDashboardView.as_view(role_code="cferi_staff"), name="cferi_staff"),
    path("freelancer/", RoleDashboardView.as_view(role_code="freelancer"), name="freelancer"),
    path("external-guest/", RoleDashboardView.as_view(role_code="external_guest"), name="external_guest"),
]
