from django.db import migrations


def create_default_roles_for_existing_clubs(apps, schema_editor):
    """Create default roles for clubs that were created before this migration."""
    Club = apps.get_model("auctions", "Club")
    ClubRole = apps.get_model("auctions", "ClubRole")
    ClubPermission = apps.get_model("auctions", "ClubPermission")

    default_roles = [
        {"name": "View club list", "permissions": ["permission_view"]},
        {"name": "Update users", "permissions": ["permission_view", "permission_add_edit"]},
        {"name": "Change club permissions", "permissions": ["permission_view", "permission_edit_club"]},
        {"name": "Export", "permissions": ["permission_view", "permission_add_edit", "permission_export"]},
    ]

    for club in Club.objects.all():
        for role_def in default_roles:
            role, created = ClubRole.objects.get_or_create(club=club, name=role_def["name"])
            if created:
                perms = ClubPermission.objects.filter(name__in=role_def["permissions"])
                role.permissions.set(perms)


def reverse_migration(apps, schema_editor):
    pass  # Don't remove roles on reverse - they may be customized


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0236_update_privacy_blog_for_clubs"),
    ]

    operations = [
        migrations.RunPython(create_default_roles_for_existing_clubs, reverse_migration),
    ]
