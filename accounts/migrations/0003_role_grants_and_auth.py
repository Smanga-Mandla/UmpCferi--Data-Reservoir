from django.db import migrations, models
import django.db.models.deletion


def seed_permission_matrix(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    RoleGrant = apps.get_model("accounts", "RoleGrant")
    db = schema_editor.connection.alias

    def grants(role_code, resource, actions, scope="all"):
        role = Role.objects.using(db).get(code=role_code)
        for action in actions:
            RoleGrant.objects.using(db).get_or_create(
                role=role,
                resource=resource,
                action=action,
                scope=scope,
            )

    crud = ("create", "read", "update", "delete")
    cru = ("create", "read", "update")
    ru = ("read", "update")
    cr = ("create", "read")
    read = ("read",)

    resources = {
        "users": "users",
        "confidential_documents": "centre_confidential_documents",
        "general_documents": "centre_general_documents",
        "partners": "partners_mous",
        "incubatees": "incubatees",
        "checkins": "checkins_metrics",
        "stakeholders": "stakeholders",
        "interactions": "interactions",
        "reports": "reports",
        "audit": "audit_log",
    }

    for resource in (
        resources["users"],
        resources["confidential_documents"],
        resources["general_documents"],
        resources["partners"],
        resources["incubatees"],
        resources["checkins"],
        resources["stakeholders"],
        resources["interactions"],
    ):
        grants("super_admin", resource, crud)
    grants("super_admin", resources["reports"], read, "all")
    grants("super_admin", resources["audit"], read)

    grants("system_admin", resources["users"], crud)
    grants("system_admin", resources["general_documents"], read)
    grants("system_admin", resources["partners"], read)
    grants("system_admin", resources["incubatees"], read)
    grants("system_admin", resources["stakeholders"], read)
    grants("system_admin", resources["reports"], read, "system_health")
    grants("system_admin", resources["audit"], read)

    grants("admin", resources["users"], read)
    grants("admin", resources["confidential_documents"], cru)
    for resource in (
        resources["general_documents"],
        resources["partners"],
        resources["incubatees"],
        resources["checkins"],
        resources["stakeholders"],
        resources["interactions"],
    ):
        grants("admin", resource, crud)
    grants("admin", resources["reports"], read, "all")

    grants("board_member", resources["confidential_documents"], read)
    grants("board_member", resources["general_documents"], read)
    grants("board_member", resources["partners"], read)
    grants("board_member", resources["incubatees"], read, "aggregate")
    grants("board_member", resources["checkins"], read, "aggregate")
    grants("board_member", resources["stakeholders"], read)
    grants("board_member", resources["interactions"], read)
    grants("board_member", resources["reports"], read, "limited")

    grants("cferi_staff", resources["general_documents"], read)
    grants("cferi_staff", resources["partners"], read)
    grants("cferi_staff", resources["incubatees"], cru, "assigned")
    grants("cferi_staff", resources["checkins"], cru, "assigned")
    grants("cferi_staff", resources["stakeholders"], cru, "assigned")
    grants("cferi_staff", resources["interactions"], cru, "assigned")
    grants("cferi_staff", resources["reports"], read, "assigned")

    grants("freelancer", resources["incubatees"], ru, "assigned")
    grants("freelancer", resources["checkins"], cr, "assigned")
    grants("freelancer", resources["stakeholders"], read, "limited")
    grants("freelancer", resources["interactions"], cr, "own")
    grants("freelancer", resources["reports"], read, "own")

    grants("external_guest", resources["partners"], read, "own")
    grants("external_guest", resources["incubatees"], ru, "own")
    grants("external_guest", resources["checkins"], cr, "own")
    grants("external_guest", resources["reports"], read, "own")


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_seed_roles"),
    ]

    operations = [
        migrations.CreateModel(
            name="LoginThrottle",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("email", models.EmailField(max_length=254, unique=True)),
                ("failed_attempts", models.PositiveSmallIntegerField(default=0)),
                ("locked_until", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="TOTPDevice",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(default="Authenticator", max_length=100)),
                ("secret_encrypted", models.TextField()),
                ("confirmed", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="totp_devices", to="accounts.user")),
            ],
            options={"ordering": ("created_at",)},
        ),
        migrations.CreateModel(
            name="RecordShare",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("object_id", models.CharField(max_length=64)),
                ("actions", models.JSONField(default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("content_type", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="contenttypes.contenttype")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_record_shares", to="accounts.user")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="record_shares", to="accounts.user")),
            ],
        ),
        migrations.AddConstraint(
            model_name="recordshare",
            constraint=models.UniqueConstraint(fields=("content_type", "object_id", "user"), name="unique_record_share_user"),
        ),
        migrations.CreateModel(
            name="RoleGrant",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("resource", models.CharField(choices=[("users", "Users and roles"), ("centre_confidential_documents", "Centre documents (confidential)"), ("centre_general_documents", "Centre documents (general)"), ("partners_mous", "Partners and MOUs"), ("incubatees", "Incubatees"), ("checkins_metrics", "Check-ins and metrics"), ("stakeholders", "Stakeholder directory"), ("interactions", "Interactions log"), ("reports", "Reports and dashboards"), ("audit_log", "Audit log")], max_length=40)),
                ("action", models.CharField(choices=[("create", "Create"), ("read", "Read"), ("update", "Update"), ("delete", "Delete")], max_length=8)),
                ("scope", models.CharField(choices=[("all", "All records"), ("assigned", "Assigned records"), ("own", "Own records"), ("aggregate", "Aggregated data only"), ("system_health", "System health only"), ("limited", "Limited/explicitly shared")], max_length=16)),
                ("role", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="grants", to="accounts.role")),
            ],
            options={"ordering": ("role__code", "resource", "action", "scope")},
        ),
        migrations.AddConstraint(
            model_name="rolegrant",
            constraint=models.UniqueConstraint(fields=("role", "resource", "action", "scope"), name="unique_role_resource_action_scope"),
        ),
        migrations.RunPython(seed_permission_matrix, migrations.RunPython.noop),
    ]
