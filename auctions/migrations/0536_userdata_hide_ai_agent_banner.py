from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0535_club_icon_email"),
    ]

    operations = [
        migrations.AddField(
            model_name="userdata",
            name="hide_ai_agent_banner",
            field=models.BooleanField(
                default=False,
                help_text=(
                    'Set by "Not interested in AI features" on the connect-an-AI-agent banner of the auction and club pages.'
                ),
            ),
        ),
    ]
