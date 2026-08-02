# Generated manually: the auto-created single-club membership is the member's own record
from django.db import migrations


def mark_single_club_memberships_member_owned(apps, schema_editor):
    """Undo the ``admin_edited=True`` default for rows nobody at the club ever asked for.

    In single-club mode every account gets a ClubMember the moment it is created
    (auctions.site_setup.ensure_single_club_membership_for_user), built out of what the person typed
    into the signup form.  It took the model default of ``admin_edited=True``, which told account
    deletion the club owned it — so deleting an account left the person's real name and email address
    sitting in the club's roster, on the site's default configuration.

    ``source="single_club_mode"`` is written only by that signal, so it identifies exactly the rows
    that were created for the person rather than by an admin.  A row an admin has edited since has
    been through one of the ClubMember admin forms, which set ``admin_edited=True`` again on save.
    """
    ClubMember = apps.get_model("auctions", "ClubMember")
    ClubMember.objects.filter(source="single_club_mode", admin_edited=True).update(admin_edited=False)


def reverse_func(apps, schema_editor):
    # Deliberately not reversed: flipping these back to True would re-assert that the club owns
    # details it never collected.  Rolling back the code is enough.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0353_privacy_blog_account_deletion"),
    ]

    operations = [
        migrations.RunPython(mark_single_club_memberships_member_owned, reverse_func),
    ]
