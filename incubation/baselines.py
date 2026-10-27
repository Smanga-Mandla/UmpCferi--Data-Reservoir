import json

from django.core.serializers.json import DjangoJSONEncoder

from incubation.models import IncubateeAccountBusiness, IncubateeBaselineSnapshot


def capture_incubatee_baseline(incubatee, actor, source=None):
    excluded = {
        "id",
        "created_by",
        "updated_by",
        "created_at",
        "updated_at",
        "deleted_at",
        "deleted_by",
        "owner",
        "assigned_users",
    }
    snapshot = {
        field.name: getattr(incubatee, field.attname)
        for field in incubatee._meta.concrete_fields
        if field.name not in excluded and field.attname not in excluded
    }
    snapshot["date_joined_cferi"] = (
        IncubateeAccountBusiness.objects.filter(incubatee=incubatee)
        .order_by("linked_at", "pk")
        .values_list("linked_at", flat=True)
        .first()
        or incubatee.created_at
    )
    founders = []
    for link in incubatee.founders.filter(is_active=True).select_related("founder__stakeholder"):
        profile = link.founder
        founders.append(
            {
                "stakeholder_id": profile.stakeholder_id,
                "name": profile.stakeholder.name,
                "id_number_encrypted": profile.id_number_encrypted,
                "gender": profile.gender,
                "date_of_birth": profile.date_of_birth,
                "race": profile.race,
                "disability_status": profile.disability_status,
                "highest_qualification": profile.highest_qualification,
                "prior_business_experience": profile.prior_business_experience,
                "ownership_percentage": link.ownership_percentage,
            }
        )
    return IncubateeBaselineSnapshot.objects.create(
        incubatee=incubatee,
        snapshot=json.loads(json.dumps(snapshot, cls=DjangoJSONEncoder)),
        founder_snapshot=json.loads(json.dumps(founders, cls=DjangoJSONEncoder)),
        source=source or incubatee.source,
        changed_by=actor,
    )
