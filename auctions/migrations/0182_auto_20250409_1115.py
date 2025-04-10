# Generated by Django 5.1.6 on 2025-04-09 15:15

from django.db import migrations


def delete_googlebot_pageviews(apps, schema_editor):
    PageView = apps.get_model("auctions", "PageView")
    PageView.objects.filter(user_agent__icontains="googlebot").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0181_alter_pageview_ip_address"),
    ]

    operations = [
        migrations.RunPython(delete_googlebot_pageviews),
    ]
