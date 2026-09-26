"""Put the sender-identification footer on every emailed template.

Two of the twenty carried a postal address (``weekly_promo_email``, ``join_auction_reminder``) and
seven carried an opt-out link. CASL wants the sender named with a postal address in every commercial
message, and most of the traffic here is Canadian or US club mail, so the footer goes on all of them
rather than on the ones somebody classified as marketing. ``auctions/email_footer.py`` has the
reasoning; the tags render nothing but the site name when no address is configured, and add the
opt-out line only where the send site passed an ``unsubscribe`` token.

The two templates that already said this in prose lose their own copy of the legal half and keep
their "manage your notifications" link, so there is one footer, in one place, and nothing to drift.
"""

from django.db import migrations

HTML_FOOTER = "{% load email_tags %}{% email_footer %}"
TEXT_FOOTER = "{% load email_tags %}{% email_footer_text %}"

#: ``(template name, field, old, new)``. The legal half of two hand-written footers, replaced by the
#: link they also carried; the shared footer supplies the address and the opt-out.
INLINE_FOOTERS = [
    (
        "weekly_promo_email",
        "content",
        "Unsubscribe from these emails: https://{{ domain }}/unsubscribe/{{unsubscribe}}/ "
        "Snail mail address: {{ mailing_address }}",
        "Manage your notifications: https://{{ domain }}/notifications/",
    ),
    (
        "weekly_promo_email",
        "html_content",
        '<br><small><a href="https://{{ domain }}/notifications/">Manage your notifications</a> or '
        '<a href="https://{{ domain }}/unsubscribe/{{unsubscribe}}/">unsubscribe</a><br>'
        "Snail mail address: {{ mailing_address }}</small>",
        '<br><small><a href="https://{{ domain }}/notifications/">Manage your notifications</a></small>',
    ),
    (
        "join_auction_reminder",
        "content",
        "Turn these emails off at https://{{ domain }}/notifications/ or unsubscribe from everything: "
        "https://{{ domain }}/unsubscribe/{{unsubscribe}}/ Snail mail address: {{ mailing_address }}",
        "Turn these emails off at https://{{ domain }}/notifications/",
    ),
    (
        "join_auction_reminder",
        "html_content",
        "<br><small>Turn these emails off under your "
        "<a href='https://{{ domain }}/notifications/'>preferences</a>, or "
        '<a href="https://{{ domain }}/unsubscribe/{{unsubscribe}}/">unsubscribe</a> from this kind of thing, '
        "or snail mail us at {{ mailing_address }}</small>",
        "<br><small>Turn these emails off under your "
        "<a href='https://{{ domain }}/notifications/'>preferences</a></small>",
    ),
]


def forwards(apps, schema_editor):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    for name, field, old, new in INLINE_FOOTERS:
        for template in EmailTemplate.objects.filter(name=name):
            body = getattr(template, field) or ""
            if old in body:
                setattr(template, field, body.replace(old, new))
                template.save(update_fields=[field])
    for template in EmailTemplate.objects.all():
        changed = []
        for field, footer in (("html_content", HTML_FOOTER), ("content", TEXT_FOOTER)):
            body = getattr(template, field) or ""
            # An empty field is a template with no such part; adding a footer would give it one.
            if body.strip() and footer not in body:
                setattr(template, field, body.rstrip() + "\n" + footer + "\n")
                changed.append(field)
        if changed:
            template.save(update_fields=changed)


def backwards(apps, schema_editor):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    for template in EmailTemplate.objects.all():
        changed = []
        for field, footer in (("html_content", HTML_FOOTER), ("content", TEXT_FOOTER)):
            body = getattr(template, field) or ""
            if footer in body:
                setattr(template, field, body.replace("\n" + footer + "\n", "").replace(footer, ""))
                changed.append(field)
        if changed:
            template.save(update_fields=changed)
    for name, field, old, new in INLINE_FOOTERS:
        for template in EmailTemplate.objects.filter(name=name):
            body = getattr(template, field) or ""
            if new in body:
                setattr(template, field, body.replace(new, old))
                template.save(update_fields=[field])


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0450_alter_donationvendor_status"),
        ("post_office", "0011_models_help_text"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
