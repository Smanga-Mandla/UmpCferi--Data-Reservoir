from django.db import migrations


ROLE_CODES = (
    "super_admin",
    "system_admin",
    "admin",
    "board_member",
    "cferi_staff",
    "freelancer",
    "external_guest",
)


def create_roles(apps, schema_editor):
    role_model = apps.get_model("accounts", "Role")
    database = schema_editor.connection.alias
    role_model.objects.using(database).bulk_create(
        [role_model(code=code) for code in ROLE_CODES],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_roles, migrations.RunPython.noop),
    ]
