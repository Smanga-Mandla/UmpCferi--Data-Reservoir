from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.access import (
    accessible_queryset,
    has_access,
    require_access,
    soft_delete_record,
)
from accounts.models import Role, RoleGrant
from audit.services import log_sensitive_record_view
from stakeholders.access import can_view_contact_details

from interactions.forms import (
    EventAttendanceForm,
    FollowUpTaskForm,
    InteractionFilterForm,
    InteractionForm,
)
from interactions.models import (
    EventAttendance,
    FollowUpTask,
    Interaction,
    InteractionHistory,
)

RESOURCE = RoleGrant.Resource.INTERACTIONS


def _board_only(user):
    return (
        not user.is_superuser
        and set(user.roles.values_list("code", flat=True)) == {Role.Code.BOARD_MEMBER}
    )


def _get_interaction(user, interaction_id, action=RoleGrant.Action.READ):
    interaction = get_object_or_404(
        Interaction.objects.prefetch_related(
            "participants",
            "incubatees",
            "partners",
            "assigned_users",
            "follow_up_tasks__owner",
            "attendance__stakeholder",
        ).select_related("owner", "created_by", "updated_by"),
        pk=interaction_id,
    )
    if not has_access(user, RESOURCE, action, interaction):
        raise PermissionDenied
    return interaction


def _interaction_snapshot(interaction):
    return {
        "occurred_at": interaction.occurred_at.isoformat(),
        "duration_minutes": interaction.duration_minutes,
        "interaction_type": interaction.interaction_type,
        "sensitivity": interaction.sensitivity,
        "participants": sorted(
            (
                {
                    "stakeholder_id": str(link.stakeholder_id)
                    if link.stakeholder_id
                    else None,
                    "display_name": link.stakeholder.name
                    if link.stakeholder_id
                    else link.display_name,
                }
                for link in interaction.participant_links.select_related("stakeholder")
            ),
            key=lambda participant: participant["display_name"].casefold(),
        ),
        "incubatee_ids": sorted(str(pk) for pk in interaction.incubatees.values_list("pk", flat=True)),
        "partner_ids": sorted(str(pk) for pk in interaction.partners.values_list("pk", flat=True)),
        "topics": interaction.topics,
        "outcome": interaction.outcome,
    }


def _record_history(interaction, actor, change_type):
    InteractionHistory.objects.create(
        interaction=interaction,
        change_type=change_type,
        snapshot=_interaction_snapshot(interaction),
        changed_by=actor,
    )


def _assign_related_users(interaction):
    user_ids = {interaction.created_by_id}
    for incubatee in interaction.incubatees.all():
        user_ids.update(incubatee.assigned_users.values_list("pk", flat=True))
        user_ids.update(
            value
            for value in (incubatee.owner_id, incubatee.created_by_id, incubatee.staff_owner_id)
            if value
        )
    for partner in interaction.partners.all():
        user_ids.update(partner.assigned_users.values_list("pk", flat=True))
        user_ids.update(value for value in (partner.owner_id, partner.created_by_id) if value)
    interaction.assigned_users.set(user_ids)


def _list_row(user, interaction):
    board_only = _board_only(user)
    participants = [
        {
            "name": link.stakeholder.name if link.stakeholder_id else link.display_name,
            "stakeholder": link.stakeholder,
            "can_view_contact": (
                can_view_contact_details(user, link.stakeholder)
                if link.stakeholder_id
                else False
            ),
        }
        for link in interaction.participant_links.select_related("stakeholder")
        if not board_only
        and (
            not link.stakeholder_id
            or has_access(
                user,
                RoleGrant.Resource.STAKEHOLDERS,
                RoleGrant.Action.READ,
                link.stakeholder,
            )
        )
    ]
    incubatees = [
        incubatee
        for incubatee in interaction.incubatees.all()
        if has_access(
            user,
            RoleGrant.Resource.INCUBATEES,
            RoleGrant.Action.READ,
            incubatee,
        )
    ]
    partners = [
        partner
        for partner in interaction.partners.all()
        if has_access(
            user,
            RoleGrant.Resource.PARTNERS_MOUS,
            RoleGrant.Action.READ,
            partner,
        )
    ]
    return {
        "interaction": interaction,
        "participants": participants,
        "incubatees": incubatees,
        "partners": partners,
        "can_update": has_access(user, RESOURCE, RoleGrant.Action.UPDATE, interaction),
        "can_delete": has_access(user, RESOURCE, RoleGrant.Action.DELETE, interaction),
        "board_only": board_only,
    }


@login_required
def interaction_list(request):
    form = InteractionFilterForm(request.user, request.GET or None)
    interactions = accessible_queryset(
        request.user,
        RESOURCE,
        RoleGrant.Action.READ,
        Interaction.objects.select_related("owner", "created_by").prefetch_related(
            "participants", "incubatees", "partners"
        ),
    )
    if form.is_valid():
        interaction_type = form.cleaned_data.get("interaction_type")
        date_from = form.cleaned_data.get("date_from")
        date_to = form.cleaned_data.get("date_to")
        incubatee = form.cleaned_data.get("incubatee")
        partner = form.cleaned_data.get("partner")
        if interaction_type:
            interactions = interactions.filter(interaction_type=interaction_type)
        if date_from:
            interactions = interactions.filter(occurred_at__date__gte=date_from)
        if date_to:
            interactions = interactions.filter(occurred_at__date__lte=date_to)
        if incubatee:
            interactions = interactions.filter(incubatees=incubatee)
        if partner:
            interactions = interactions.filter(partners=partner)
    rows = [
        _list_row(request.user, interaction)
        for interaction in interactions.distinct()
        if has_access(request.user, RESOURCE, RoleGrant.Action.READ, interaction)
    ]
    return render(
        request,
        "interactions/interaction_list.html",
        {
            "filter_form": form,
            "rows": rows,
            "can_create": has_access(request.user, RESOURCE, RoleGrant.Action.CREATE),
            "can_export": has_access(request.user, RESOURCE, RoleGrant.Action.READ),
        },
    )


@login_required
def interaction_detail(request, interaction_id):
    interaction = _get_interaction(request.user, interaction_id)
    if interaction.sensitivity in {"confidential", "restricted"}:
        log_sensitive_record_view(request, interaction)
    row = _list_row(request.user, interaction)
    row.update(
        {
            "history": interaction.history.select_related("changed_by"),
            "tasks": (
                interaction.follow_up_tasks.select_related("owner", "created_by")
                if not _board_only(request.user)
                else ()
            ),
            "attendance": [
                entry
                for entry in interaction.attendance.select_related(
                    "stakeholder", "recorded_by"
                )
                if not _board_only(request.user)
                and (
                    not entry.stakeholder_id
                    or has_access(
                        request.user,
                        RoleGrant.Resource.STAKEHOLDERS,
                        RoleGrant.Action.READ,
                        entry.stakeholder,
                    )
                )
            ],
            "can_update": has_access(request.user, RESOURCE, RoleGrant.Action.UPDATE, interaction),
            "can_delete": has_access(request.user, RESOURCE, RoleGrant.Action.DELETE, interaction),
        }
    )
    row["task_form"] = (
        FollowUpTaskForm(request.user, interaction) if row["can_update"] else None
    )
    row["task_owner_choices"] = (
        row["task_form"].fields["owner"].queryset
        if row["task_form"]
        else ()
    )
    row["task_status_choices"] = FollowUpTask.Status.choices
    row["attendance_form"] = (
        EventAttendanceForm(request.user, interaction)
        if row["can_update"]
        and interaction.interaction_type
        in (Interaction.Type.EVENT, Interaction.Type.WORKSHOP)
        else None
    )
    row["show_attendance"] = not _board_only(request.user) and interaction.interaction_type in (
        Interaction.Type.EVENT,
        Interaction.Type.WORKSHOP,
    )
    row["attendance_status_choices"] = EventAttendance.Status.choices
    return render(request, "interactions/interaction_detail.html", row)


def _interaction_form(request, interaction=None):
    if interaction is None:
        require_access(request.user, RESOURCE, RoleGrant.Action.CREATE)
    else:
        require_access(request.user, RESOURCE, RoleGrant.Action.UPDATE, interaction)
    form = InteractionForm(
        request.user,
        request.POST or None,
        instance=interaction,
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            saved = form.save(commit=False)
            if interaction is None:
                saved.created_by = request.user
                saved.owner = request.user
            else:
                saved.updated_by = request.user
            saved.save()
            form.save_m2m()
            saved.participants.set(form.cleaned_data["participants"])
            saved.participant_links.filter(stakeholder__isnull=True).delete()
            for participant_name in form.cleaned_data["additional_participants"]:
                saved.participant_links.create(display_name=participant_name)
            _assign_related_users(saved)
            _record_history(
                saved,
                request.user,
                InteractionHistory.ChangeType.CREATED
                if interaction is None
                else InteractionHistory.ChangeType.UPDATED,
            )
        messages.success(request, "Interaction saved.")
        return redirect("interactions:detail", interaction_id=saved.pk)
    return render(
        request,
        "interactions/interaction_form.html",
        {
            "form": form,
            "interaction": interaction,
            "title": "New interaction" if interaction is None else "Edit interaction",
        },
    )


@login_required
def interaction_create(request):
    return _interaction_form(request)


@login_required
def interaction_update(request, interaction_id):
    interaction = _get_interaction(
        request.user, interaction_id, RoleGrant.Action.UPDATE
    )
    return _interaction_form(request, interaction)


@login_required
@require_POST
def interaction_delete(request, interaction_id):
    interaction = _get_interaction(
        request.user, interaction_id, RoleGrant.Action.DELETE
    )
    with transaction.atomic():
        _record_history(
            interaction, request.user, InteractionHistory.ChangeType.DELETED
        )
        soft_delete_record(request.user, RESOURCE, interaction)
    messages.success(request, "Interaction was soft-deleted; its history is retained.")
    return redirect("interactions:list")


@login_required
@require_POST
def follow_up_task_add(request, interaction_id):
    interaction = _get_interaction(
        request.user, interaction_id, RoleGrant.Action.UPDATE
    )
    form = FollowUpTaskForm(request.user, interaction, request.POST)
    if form.is_valid():
        task = form.save(commit=False)
        task.interaction = interaction
        task.created_by = request.user
        task.save()
        messages.success(request, "Follow-up task assigned.")
    else:
        messages.error(request, "Follow-up task was not saved. Check the entered details.")
    return redirect("interactions:detail", interaction_id=interaction.pk)


@login_required
@require_POST
def follow_up_task_update(request, interaction_id, task_id):
    interaction = _get_interaction(
        request.user, interaction_id, RoleGrant.Action.UPDATE
    )
    task = get_object_or_404(
        FollowUpTask, pk=task_id, interaction=interaction
    )
    form = FollowUpTaskForm(
        request.user, interaction, request.POST, instance=task
    )
    if form.is_valid():
        form.save()
        messages.success(request, "Follow-up task updated.")
    else:
        messages.error(request, "Follow-up task was not updated. Check the entered details.")
    return redirect("interactions:detail", interaction_id=interaction.pk)


@login_required
@require_POST
def attendance_add(request, interaction_id):
    interaction = _get_interaction(
        request.user, interaction_id, RoleGrant.Action.UPDATE
    )
    if interaction.interaction_type not in (
        Interaction.Type.EVENT,
        Interaction.Type.WORKSHOP,
    ):
        raise PermissionDenied("Attendance registers are only available for events and workshops.")
    form = EventAttendanceForm(request.user, interaction, request.POST)
    if form.is_valid():
        attendance = form.save(commit=False)
        attendance.interaction = interaction
        attendance.recorded_by = request.user
        try:
            with transaction.atomic():
                attendance.full_clean()
                attendance.save()
        except (IntegrityError, ValidationError):
            messages.error(request, "This participant already has an attendance entry.")
        else:
            messages.success(request, "Attendance recorded.")
    else:
        messages.error(request, "Attendance was not recorded. Select an interaction participant.")
    return redirect("interactions:detail", interaction_id=interaction.pk)


@login_required
@require_POST
def attendance_update(request, interaction_id, attendance_id):
    interaction = _get_interaction(
        request.user, interaction_id, RoleGrant.Action.UPDATE
    )
    attendance = get_object_or_404(
        EventAttendance, pk=attendance_id, interaction=interaction
    )
    form = EventAttendanceForm(
        request.user, interaction, request.POST, instance=attendance
    )
    if form.is_valid():
        attendance = form.save(commit=False)
        try:
            with transaction.atomic():
                attendance.full_clean()
                attendance.save()
        except (IntegrityError, ValidationError):
            messages.error(request, "Attendance was not updated. Check for a duplicate entry.")
        else:
            messages.success(request, "Attendance updated.")
    else:
        messages.error(request, "Attendance was not updated.")
    return redirect("interactions:detail", interaction_id=interaction.pk)
