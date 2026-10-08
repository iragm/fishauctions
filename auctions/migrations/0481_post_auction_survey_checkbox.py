"""``Auction.post_auction_survey`` becomes a checkbox: "No feedback" is off, either way of asking is on.

Where it asks now follows from the auction (``auction_survey.asks_in_invoice``). Invoices are always
emailed, never pushed, so the push preference's help text says so.
"""

from django.db import migrations, models


def copy_choice(apps, schema_editor):
    Auction = apps.get_model("auctions", "Auction")
    Auction.objects.filter(post_auction_survey="none").update(ask_for_feedback=False)


def copy_back(apps, schema_editor):
    Auction = apps.get_model("auctions", "Auction")
    Auction.objects.filter(ask_for_feedback=False).update(post_auction_survey="none")


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0480_invoice_open_times_abandoned_bids"),
    ]

    operations = [
        migrations.AddField(
            model_name="auction",
            name="ask_for_feedback",
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(copy_choice, copy_back),
        migrations.RemoveField(model_name="auction", name="post_auction_survey"),
        migrations.RenameField(model_name="auction", old_name="ask_for_feedback", new_name="post_auction_survey"),
        migrations.AlterField(
            model_name="auction",
            name="post_auction_survey",
            field=models.BooleanField(
                default=True, help_text="Ask people how the auction went", verbose_name="Ask for feedback"
            ),
        ),
        migrations.AlterField(
            model_name="userdata",
            name="push_notifications_instead_of_email",
            field=models.BooleanField(
                blank=True,
                default=False,
                help_text="Get notifications in the app instead of emails, for everything except invoices and account emails like password resets. Requires the app to be installed and signed in. Auctions near you arrive as notifications too.",
            ),
        ),
    ]
