"""Joins two migrations that both took number 0504 on staging; it changes nothing."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0504_library_web_visibility_and_picture_resize"),
        ("auctions", "0504_print_reminder_links_the_bagging_guide"),
    ]

    operations = []
