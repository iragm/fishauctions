"""The hand-set half of a club's stage, and why a club stopped.

The field defaults to ``prospect``, which is right for every club club discovery is about to add
and wrong for every club already here: those were all added by hand by somebody who had decided to
publish them, which is exactly the approval this field records. So the rows already in the table are
moved to ``listed`` in the same migration -- without that step, deploying this empties the club map.
"""

from django.db import migrations, models


def approve_the_clubs_already_here(apps, schema_editor):
    Club = apps.get_model("auctions", "Club")
    Club.objects.update(outreach_stage="listed")


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0429_form_failure_abandonment"),
    ]

    operations = [
        migrations.AddField(
            model_name="club",
            name="outreach_stage",
            field=models.CharField(
                choices=[
                    ("prospect", "Found, not approved"),
                    ("contacted", "Contacted, no reply yet"),
                    ("listed", "Approved and listed"),
                ],
                db_index=True,
                default="prospect",
                help_text="The half of a club's progress that no query can answer. Only 'Approved and listed' puts a club on the map, in club search and in the dropdowns -- ClubHealth derives everything after that from rows and never writes here.",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="club",
            name="stall_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Not known"),
                    ("no_reply", "Never replied"),
                    ("no_auction", "No auction coming up"),
                    ("uses_other", "Uses something else"),
                    ("paper", "Paper works fine"),
                    ("cost", "Cost"),
                    ("not_interested", "Not interested"),
                    ("folded", "Club has folded"),
                ],
                default="",
                help_text="Why this club stopped where it did, in a word that can be counted. Set it when somebody answers; notes are free text and cannot say which objection is worth fixing.",
                max_length=20,
            ),
        ),
        migrations.RunPython(approve_the_clubs_already_here, migrations.RunPython.noop),
    ]
