from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0520_let_waiting_sellers_sell"),
    ]

    operations = [
        migrations.AddField(
            model_name="club",
            name="record_offsite_square_payments",
            field=models.BooleanField(
                default=False,
                verbose_name="Add Square payments taken outside this site to the treasurer report",
            ),
        ),
        migrations.AddField(
            model_name="clubmoney",
            name="external_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=255),
        ),
        migrations.AlterField(
            model_name="clubmoney",
            name="category",
            field=models.CharField(
                choices=[
                    ("auction_sale", "Auction sale (buyer payment)"),
                    ("auction_seller_payout", "Seller payout"),
                    ("tax", "Sales tax collected"),
                    ("membership", "Membership dues"),
                    ("invoice_adjustment", "Invoice adjustment"),
                    ("first_bid_payout", "First-bid payout"),
                    ("club_member_discount", "Club member discount"),
                    ("registration_fee", "Registration fee"),
                    ("rounding", "Invoice rounding"),
                    ("square_offsite", "Square payment outside this site"),
                    ("donation", "Donation"),
                    ("speaker_costs", "Speaker costs"),
                    ("meeting_location_cost", "Meeting location cost"),
                    ("refunds", "Refund"),
                    ("adjustment", "Balance adjustment"),
                ],
                max_length=40,
            ),
        ),
    ]
