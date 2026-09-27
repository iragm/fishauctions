"""The weekly promo email becomes one message per promoted auction (``auction_promos``).

``AuctionCampaign.kind`` separates the promo's sent log from the join reminder's rows, and
``promo_key`` makes a promo row unique per auction and user in the database. ``uuid`` gets the index
every tracked page view's lookup needed. The weekly email's counters and schedule go; its last send
time stays, renamed, for the switchover. Its ``EmailTemplate`` row stays: ``post_office.Email``
cascades from its template, so deleting it would take every weekly email ever sent out of the log.
"""

import uuid

from django.db import migrations, models

TEXT = """Hello {{ name }},

{{ auction.title }} is an {{ kind }}{% if distance %} {{ distance }} from you{% endif %}{% if when %}, and {{ when }}{% endif %}.
{% if multiple_locations %}Your nearest pickup location is {{ location.name }}.
{% elif location.name %}It's at {{ location.name }}.
{% endif %}
Read the rules and join: {{ auction_url }}&uid={{ unsubscribe }}
{% if lots %}
Some lots you might like:
{% for lot in lots %}
* {{ lot.lot_name }}: https://{{ lot.full_lot_link }}?src={{ uuid }}
{% endfor %}{% endif %}
You're getting this because you asked to hear about auctions near you. Change that, or how far away counts as near: https://{{ domain }}/notifications/
{% load email_tags %}{% email_footer_text %}
"""

HTML = """Hello {{ name }},<br><br>

<a href="{{ auction_url }}&uid={{ unsubscribe }}"><b>{{ auction.title }}</b></a> is an {{ kind }}{% if distance %} {{ distance }} from you{% endif %}{% if when %}, and {{ when }}{% endif %}.
{% if multiple_locations %}Your nearest pickup location is {{ location.name }}.{% elif location.name %}It's at {{ location.name }}.{% endif %}<br><br>

<a href="{{ auction_url }}&uid={{ unsubscribe }}">Read the rules and join</a><br><br>

{% if lots %}<h3>Some lots you might like</h3>
{% for lot in lots %}
<b>{{ lot.lot_name }}</b><br>
<a href="https://{{ lot.full_lot_link }}?src={{ uuid }}">{% if lot.thumbnail %}{% if lot.thumbnail.image %}<img src='https://{{ domain }}{{ lot.thumbnail.image.lot_list.url }}'></img>{% else %}<img src='{{ lot.thumbnail.url }}' style='max-width:250px; max-height:150px; object-fit:cover;'></img>{% endif %}<br>{% endif %}View this lot</a><br><br>
{% endfor %}{% endif %}

<small>You're getting this because you asked to hear about auctions near you. <a href="https://{{ domain }}/notifications/">Change that</a>, or how far away counts as near.</small>
{% load email_tags %}{% email_footer %}
"""


def create_template(apps, schema_editor):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    EmailTemplate.objects.update_or_create(
        name="auction_promo_email",
        defaults={
            "subject": "{{ auction.title }}: an {{ kind }} near you",
            "content": TEXT,
            "html_content": HTML,
        },
    )


def delete_template(apps, schema_editor):
    apps.get_model("post_office", "EmailTemplate").objects.filter(name="auction_promo_email").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0464_print_custom_random_label"),
        ("post_office", "0011_models_help_text"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="auction",
            name="promo_push_notifications_sent",
        ),
        migrations.RemoveField(
            model_name="auction",
            name="weekly_promo_emails_sent",
        ),
        # Kept, renamed: the first weeks of auction_promos read it so nobody is emailed about an auction
        # the weekly email just listed.
        migrations.RenameField(
            model_name="userdata",
            old_name="last_promo_email_sent_at",
            new_name="last_weekly_promo_sent_at",
        ),
        migrations.RemoveField(
            model_name="userdata",
            name="next_promo_email_at",
        ),
        migrations.AddField(
            model_name="auctioncampaign",
            name="kind",
            field=models.CharField(
                choices=[
                    ("view", "Viewed; join reminder"),
                    ("promo", "Promoted to them"),
                ],
                default="view",
                max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name="userdata",
            name="email_me_about_new_auctions",
            field=models.BooleanField(
                blank=True,
                default=True,
                help_text="Once per auction, a day after bidding opens, when one of its pickup locations is near you",
                verbose_name="Tell me about online auctions near me",
            ),
        ),
        migrations.AlterField(
            model_name="userdata",
            name="email_me_about_new_in_person_auctions",
            field=models.BooleanField(
                blank=True,
                default=True,
                help_text="Once per auction, about a week before it starts",
                verbose_name="Tell me about in-person auctions near me",
            ),
        ),
        migrations.AlterField(
            model_name="userdata",
            name="push_notifications_instead_of_email",
            field=models.BooleanField(
                blank=True,
                default=False,
                help_text="Get notifications in the app instead of emails, for everything except account emails like password resets. Requires the app to be installed and signed in. Auctions near you arrive as notifications too.",
            ),
        ),
        migrations.AddField(
            model_name="auctioncampaign",
            name="promo_key",
            field=models.CharField(
                blank=True,
                editable=False,
                help_text="auction:user on promo rows, empty on the rest: the database, not a read-then-write, is what stops two runs of auction_promos telling the same person twice.",
                max_length=50,
                null=True,
                unique=True,
            ),
        ),
        migrations.AlterField(
            model_name="auctioncampaign",
            name="uuid",
            field=models.CharField(blank=True, db_index=True, default=uuid.uuid4, max_length=255),
        ),
        migrations.AlterField(
            model_name="userdata",
            name="last_weekly_promo_sent_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When the retired weekly promo email last went to this user. auction_promos won't email about an auction it already listed. Nothing writes it; drop it once no window open at the switch is still open.",
                null=True,
            ),
        ),
        migrations.RunPython(create_template, delete_template),
    ]
