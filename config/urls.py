from django.contrib import admin
from django.urls import include, path
from django.views.generic import TemplateView

from config.health import health_check

handler403 = "config.errors.permission_denied"

urlpatterns = [
    path("", TemplateView.as_view(template_name="pages/home.html"), name="home"),
    path("health/", health_check, name="health"),
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("documents/", include("documents.urls")),
    path("partners/", include("partners.urls")),
    path("stakeholders/", include("stakeholders.urls")),
    path("incubatees/", include("incubation.urls")),
    path("interactions/", include("interactions.urls")),
    path("dashboard/", include("reporting.urls")),
]
