from django.db import migrations


def create_default_permissions(apps, schema_editor):
    ClubPermission = apps.get_model("auctions", "ClubPermission")
    permissions = [
        ("permission_admin", "Full admin access"),
        ("permission_view", "View members"),
        ("permission_export", "Export member data"),
        ("permission_add_edit", "Add and edit members"),
        ("permission_edit_club", "Edit club settings"),
    ]
    for name, description in permissions:
        ClubPermission.objects.get_or_create(name=name, defaults={"description": description})


def reverse_migration(apps, schema_editor):
    pass  # Don't delete permissions on reverse - they may be in use


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0234_remove_clubmember_contact_and_more"),
    ]
    operations = [
        migrations.RunPython(create_default_permissions, reverse_migration),
    ]
