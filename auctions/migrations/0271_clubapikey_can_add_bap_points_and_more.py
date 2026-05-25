from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0270_enable_category_field_for_all_auctions"),
    ]

    operations = [
        migrations.AddField(
            model_name="clubapikey",
            name="can_add_bap_points",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="clubapikey",
            name="can_add_club_members",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="clubapikey",
            name="can_read_club_member_list",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="clubapikey",
            name="can_update_club_members",
            field=models.BooleanField(default=False),
        ),
    ]
