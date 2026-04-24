from django.db import migrations


def delete_baiduspider_pageviews(apps, schema_editor):
    PageView = apps.get_model("auctions", "PageView")
    PageView.objects.filter(user_agent__icontains="baiduspider").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0231_alter_auction_custom_field_1_and_more"),
    ]

    operations = [
        migrations.RunPython(delete_baiduspider_pageviews, migrations.RunPython.noop),
    ]
