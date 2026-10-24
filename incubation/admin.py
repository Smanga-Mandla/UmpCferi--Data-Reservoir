from django.contrib import admin

from incubation.models import Cohort, Programme, ProgrammeCostObservation


@admin.register(Programme)
class ProgrammeAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)


@admin.register(Cohort)
class CohortAdmin(admin.ModelAdmin):
    list_display = ("name", "programme", "start_date", "end_date", "is_active")
    list_filter = ("programme", "is_active")
    search_fields = ("name", "programme__name")


@admin.register(ProgrammeCostObservation)
class ProgrammeCostObservationAdmin(admin.ModelAdmin):
    list_display = ("programme", "period_start", "amount_zar", "source", "recorded_by")
    list_filter = ("programme", "period_start")
    readonly_fields = ("recorded_at",)

    def has_change_permission(self, request, obj=None):
        return obj is None and super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False
