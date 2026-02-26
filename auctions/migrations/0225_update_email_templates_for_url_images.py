# Generated manually to fix email templates to support URL-only lot images
from django.db import migrations

OLD_THUMBNAIL_SNIPPET = (
    "{% if lot.thumbnail %}<img src='https://{{ domain }}{{ lot.thumbnail.image.lot_list.url }}'></img><br>{% endif %}"
)
NEW_THUMBNAIL_SNIPPET = "{% if lot.thumbnail %}{% if lot.thumbnail.image %}<img src='https://{{ domain }}{{ lot.thumbnail.image.lot_list.url }}'></img>{% else %}<img src='{{ lot.thumbnail.url }}' style='max-width:250px; max-height:150px; object-fit:cover;'></img>{% endif %}<br>{% endif %}"


def update_email_templates(apps, schema_editor):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    for template in EmailTemplate.objects.filter(html_content__contains=OLD_THUMBNAIL_SNIPPET):
        template.html_content = template.html_content.replace(OLD_THUMBNAIL_SNIPPET, NEW_THUMBNAIL_SNIPPET)
        template.save()


def reverse_func(apps, schema_editor):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    for template in EmailTemplate.objects.filter(html_content__contains=NEW_THUMBNAIL_SNIPPET):
        template.html_content = template.html_content.replace(NEW_THUMBNAIL_SNIPPET, OLD_THUMBNAIL_SNIPPET)
        template.save()


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0224_lot_image_url_lot_use_images_from_lotimage_url_and_more"),
        ("post_office", "0011_models_help_text"),
    ]

    operations = [
        migrations.RunPython(update_email_templates, reverse_func),
    ]
