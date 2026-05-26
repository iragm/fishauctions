from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0277_backfill_clubmember_user"),
    ]

    operations = [
        migrations.AddField(
            model_name="club",
            name="auction_email_member",
            field=models.ForeignKey(
                blank=True,
                help_text="Incoming mail for club-slug-auctions@your-domain is forwarded to this member.",
                null=True,
                on_delete=models.SET_NULL,
                related_name="club_auction_email_destinations",
                to="auctions.clubmember",
            ),
        ),
        migrations.AddField(
            model_name="club",
            name="membership_email_member",
            field=models.ForeignKey(
                blank=True,
                help_text="Incoming mail for club-slug-memberships@your-domain is forwarded to this member.",
                null=True,
                on_delete=models.SET_NULL,
                related_name="club_membership_email_destinations",
                to="auctions.clubmember",
            ),
        ),
    ]
