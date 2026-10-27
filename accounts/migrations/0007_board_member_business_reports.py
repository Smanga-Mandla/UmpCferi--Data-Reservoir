from django.db import migrations


def enable_board_business_reports(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    RoleGrant = apps.get_model("accounts", "RoleGrant")
    database = schema_editor.connection.alias
    board_role = Role.objects.using(database).get(code="board_member")
    grants = RoleGrant.objects.using(database).filter(
        role=board_role,
        resource="reports",
        action="read",
    )
    grants.delete()
    RoleGrant.objects.using(database).create(
        role=board_role,
        resource="reports",
        action="read",
        scope="all",
    )


def restore_limited_board_reports(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    RoleGrant = apps.get_model("accounts", "RoleGrant")
    database = schema_editor.connection.alias
    board_role = Role.objects.using(database).get(code="board_member")
    RoleGrant.objects.using(database).filter(
        role=board_role,
        resource="reports",
        action="read",
    ).delete()
    RoleGrant.objects.using(database).create(
        role=board_role,
        resource="reports",
        action="read",
        scope="limited",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0006_alter_role_code"),
    ]

    operations = [
        migrations.RunPython(
            enable_board_business_reports,
            restore_limited_board_reports,
        ),
    ]
