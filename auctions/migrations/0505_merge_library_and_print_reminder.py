from django.db import migrations


class Migration(migrations.Migration):
    """Two branches each added a 0504 the same day; neither touches the other's tables."""

    dependencies = [
        ("auctions", "0504_library_web_visibility_and_picture_resize"),
        ("auctions", "0504_print_reminder_links_the_bagging_guide"),
    ]

    operations = []
