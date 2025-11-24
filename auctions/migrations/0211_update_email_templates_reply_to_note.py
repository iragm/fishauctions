# Generated manually to add reply-to notes to email templates
from django.db import migrations


def update_email_templates_with_reply_to(apps, schema_editor):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")

    templates_to_update = [
        {
            "name": "non_auction_lot_seller",
            "subject": "{{ lot.lot_name }} is now sold!",
            "content": """Hi {{lot.user.first_name}},

You've sold {{ lot.lot_name }} for ${{lot.winning_price}} to {{ lot.winner.first_name}} {{lot.winner.last_name}}.

Reply to this email to contact the winner and coordinate payment and lot exchange.

Best wishes,
{{domain}}

Make sure to direct your reply to {{reply_to_email}}""",
            "html_content": """Hi {{lot.user.first_name}},<br><br>

You've sold {{ lot.lot_name }} for ${{lot.winning_price}} to {{ lot.winner.first_name}} {{lot.winner.last_name}}<br><br>

Reply to this email to contact the winner and coordinate payment and lot exchange.<br><br>

Best wishes,<br>
{{domain}}<br><br>

<small>Make sure to direct your reply to {{reply_to_email}}</small>""",
        },
        {
            "name": "non_auction_lot_winner",
            "subject": "{{ lot.lot_name }} is now sold!",
            "content": """Hi {{lot.winner.first_name}},

You've won {{ lot.lot_name }} for ${{lot.winning_price}} from {{ lot.user.first_name }} {{ lot.user.last_name }}.

Reply to this email to contact the seller and coordinate payment and lot exchange.

Best wishes,
{{domain}}

Make sure to direct your reply to {{reply_to_email}}""",
            "html_content": """Hi {{lot.winner.first_name}},<br><br>

You've won {{ lot.lot_name }} for ${{lot.winning_price}} from {{ lot.user.first_name }} {{ lot.user.last_name }}.<br><br>

Reply to this email to contact the seller and coordinate payment and lot exchange.<br><br>

Best wishes,<br>
{{domain}}<br><br>

<small>Make sure to direct your reply to {{reply_to_email}}</small>""",
        },
        {
            "name": "invoice_ready",
            "subject": "{{ subject }}",
            "content": """Hello {{ name }},

{{ subject }}.  {% if invoice.user_should_be_paid %}You will be paid{% else %}You owe a total of{% endif %} ${{ invoice.absolute_amount|floatformat:2}}{% if location %}

{% if invoice.status == "PAID" %}Your invoice has been paid in full, this is just a receipt for your records.{% endif %}

{% if invoice.auction.is_online %}
You must meet at {{ location }} to exchange your lots.

See you there!{% endif %}{% endif %}

https://{{domain}}/invoices/{{invoice.no_login_link}}/

{{ invoice.auction.invoice_payment_instructions | default:""}}

Best wishes,
{{domain}}

Make sure to direct your reply to {{reply_to_email}}""",
            "html_content": """Hello {{ name }},<br><br>

{{ subject }}.  {% if invoice.user_should_be_paid %}You will be paid{% else %}You owe a total of{% endif %} ${{ invoice.absolute_amount|floatformat:2}}{% if location %}
{% if invoice.status == "PAID" %}<br><br>Your invoice has been paid in full, this is just a receipt for your records.{% endif %}
{% if invoice.auction.is_online %}<br><br>
You must meet at {{ location }} to exchange your lots.<br><br>

See you there!{% endif %}{% endif %}<br><br>

<a href='https://{{domain}}/invoices/{{invoice.no_login_link}}/?src=notification'>Click here to view your invoice</a><br><br>
<br>{{ invoice.auction.invoice_payment_instructions | default:""}}<br>
Best wishes,<br>
{{domain}}<br><br>

<small>Make sure to direct your reply to {{reply_to_email}}</small>""",
        },
        {
            "name": "online_auction_welcome",
            "subject": "Thanks for joining {{tos.auction}}",
            "content": """Hello {{ tos.name }},

Thank you for joining {{tos.auction}}!  This is an online auction with in-person lot exchange.

{{ closer_location_warning }}
Some things to keep in mind:
* Bidding ends on {{ tos.auction_date_as_localized_string }}.  If last-minute bids are placed on a lot, the end time will be extended: https://{{ domain }}/faq#how-exactly-do-dynamic-endings-work
* The winning bid is determined by the second highest bidder, so you should bid what an item is worth to you.  You'll only pay $1 more than the second highest bid.
* If you want to sell lots, you can do so now.
* Any lots you win will need to be picked up{% if tos.auction.multi_location %} at {{ tos.pickup_location }},{% endif %}{% if not tos.pickup_location.users_must_coordinate_pickup%} on {{ tos.pickup_time_as_localized_string }}{% endif %}.  You can get directions here: {{tos.pickup_location.directions_link}}
{% if tos.auction.multi_location and tos.second_pickup_time_as_localized_string %}* This is a multi-location auction; you can bid on lots at any location, and the club will coordinate transport between locations.  If you win lots from other locations, you'll need to return on {{tos.second_pickup_time_as_localized_string}} to pick them up.{% endif %}

Questions?  Just reply and we'll help!

Make sure to direct your reply to {{reply_to_email}}""",
            "html_content": """Hello {{ tos.name }},<br>
<br>
Thank you for joining {{tos.auction}}!  This is an online auction with in-person lot exchange.<br>

{{ closer_location_warning_html }}<br>
Some things to keep in mind:<ul>
<li>Bidding ends on {{ tos.auction_date_as_localized_string }}.  Expect prices to increase -- sometimes dramatically! -- on the last day.  If last-minute bids are placed on a lot, the end time will be extended.  <a href="https://{{ domain }}/faq#how-exactly-do-dynamic-endings-work">More about dynamic endings</a></li>
<li> The winning bid is determined by the second highest bidder, so you should bid what an item is worth to you.  You'll only pay $1 more than the second highest bid.</li>
<li> If you want to sell lots, you can do so now.  <a href="https://{{domain}}{{tos.auction.add_lot_link}}">Click here to get started.</a></li>
<li> Any lots you win will need to be picked up{% if tos.auction.multi_location %} at {{ tos.pickup_location }},{% endif %}{% if not tos.pickup_location.users_must_coordinate_pickup%} on {{ tos.pickup_time_as_localized_string }}{% endif %}.  <a href="{{tos.pickup_location.directions_link}}">Get directions</a></li>
{% if tos.auction.multi_location and tos.second_pickup_time_as_localized_string %}<li> This is a multi-location auction; you can bid on lots at any location, and the club will coordinate transport between locations.  If you win lots from other locations, you'll need to return on {{tos.second_pickup_time_as_localized_string}} to pick them up.</li>{% endif %}</ul>
<br>
<br>
Questions?  Just reply and we'll help!<br><br>

<small>Make sure to direct your reply to {{reply_to_email}}</small>""",
        },
        {
            "name": "in_person_auction_welcome",
            "subject": "Thanks for joining {{tos.auction}}",
            "content": """Hello {{ tos.name }},

Thank you for joining {{tos.auction}}!  This is an in-person auction that starts {{ tos.auction_date_as_localized_string }}.  You can get directions here: {{tos.pickup_location.directions_link}}

See the auction's rules here: https://{{domain}}/auctions/{{tos.auction.slug}}/

See you there!

Questions?  Just reply and we'll help!

Make sure to direct your reply to {{reply_to_email}}""",
            "html_content": """Hello {{ tos.name }},<br>
<br>
Thank you for joining {{tos.auction}}!  This is an in-person auction that starts {{ tos.auction_date_as_localized_string }}.  <a href="{{tos.pickup_location.directions_link}}">Get directions here</a>.<br>
<br>
If you want to sell lots, you can <a href="https://{{domain}}{{tos.auction.add_lot_link}}">add them here</a>.<br>
<br>
Make sure to <a href="https://{{domain}}/auctions/{{tos.auction.slug}}/">read the auction's rules</a>.<br>
<br>
See you there!<br>
<br>
Questions?  Just reply and we'll help!<br><br>

<small>Make sure to direct your reply to {{reply_to_email}}</small>""",
        },
        {
            "name": "auction_print_reminder",
            "subject": "Remember to print your labels for {{ tos.auction }}",
            "content": """Hello {{ tos.name }},

Thanks for adding lots to {{ tos.auction }}!  {% if tos.auction.is_online%}Now that the auction has ended,{% else %}Before you bring your lots to the auction,{%endif%} please print your labels: https://{{domain}}{{tos.auction.label_print_link}}

{% if not tos.auction.is_online%}If you want to add more lots, add them before printing your labels.{%endif%}You can print these on regular paper, cut them out, and use packing tape to attach them to your lots.  Make sure the bags are dry before attaching the labels!

{% if tos.auction.is_online%}If you don't own a printer, write the lot number{% if tos.auction.multi_location %}, winner's location {% endif %} and winner's name clearly on each lot.
{% else %}
Make sure to arrive well before the auction begins so you have time to organize your lots.
{% endif %}

{% if website_focus == "fish" %}If you haven't packed fish before, take a look at these tips: https://{{ domain }}/blog/transporting-fish/{% endif %}

Questions?  Just reply and we'll help!

Make sure to direct your reply to {{reply_to_email}}""",
            "html_content": """Hello {{ tos.name }},<br>
<br>
Thanks for adding lots to {{ tos.auction }}!  {% if tos.auction.is_online%}Now that the auction has ended,{% else %}Before you bring your lots to the auction,{%endif%} please <a href="https://{{domain}}{{tos.auction.label_print_link}}">print your labels from here</a>.<br>{% if not tos.auction.is_online%}If you want to add more lots, add them before printing your labels.{%endif%}
<ol>
<li>Print labels on regular paper</li>
<li>Cut them out</li>
<li>Use packing tape to attach them to your lots</li>
</ol>Make sure the bags are dry before attaching the labels!<br>
<br>
{% if tos.auction.is_online%}If you don't own a printer, <b>write the lot number{% if tos.auction.multi_location %}, winner's location {% endif %} and winner's name clearly on each lot</b>.<br><br>
Remember to bring your lots to {{ tos.pickup_location }}, on {{ tos.pickup_time_as_localized_string }}.{% else %}
Make sure to arrive well before the auction begins, so you have time to organize your lots.  Remember that bidding starts promptly on {{ tos.auction_date_as_localized_string }}.<br>
{% endif %}<a href="{{tos.pickup_location.directions_link}}">Get directions</a>.
<br><br>
{% if website_focus == "fish" %}If you haven't packed fish before, take a look at <a href="https://{{ domain }}/blog/transporting-fish/">these tips</a>.<br><br>{% endif %}
Questions?  Just reply and we'll help!<br><br>

<small>Make sure to direct your reply to {{reply_to_email}}</small>""",
        },
        {
            "name": "reprint_reminder",
            "subject": "Some of your labels for {{ tos.auction }} need to be reprinted",
            "content": """Hello {{ tos.name }},

Some of your lots have sold with buy now!

To help get these to the winner, a new label needs to be printed with the winner's name. If you're able to print a new label before the auction, that would be great.  Click here: https://{{domain}}{{tos.auction.label_print_unprinted_link}} (this will only print labels that need to be printed) and place these labels on your sold lot(s).

If printing isn't possible, please notify the auction staff when you arrive so we can assist.

Thank you, and we'll see you at {{ tos.auction }}

Questions?  Just reply and we'll help!

Make sure to direct your reply to {{reply_to_email}}""",
            "html_content": """Hello {{ tos.name }},<br><br>

Some of your lots have sold with buy now!<br><br>

To help get these to the winner, a new label needs to be printed with the winner's name. If you're able to print a new label before the auction, that would be great.  <a href='https://{{domain}}{{tos.auction.label_print_unprinted_link}}'>Click here to print your labels</a> (this will only print labels that need to be printed) and place these labels on your sold lot(s).<br><br>

If printing isn't possible, please notify the auction staff when you arrive so we can assist.<br><br>

Thank you, and we'll see you at {{ tos.auction }}<br><br>

Questions?  Just reply and we'll help!<br><br>

<small>Make sure to direct your reply to {{reply_to_email}}</small>""",
        },
        {
            "name": "join_auction_reminder",
            "subject": "Don't forget to join {{ auction }}",
            "content": """Hey {{ user.first_name }},

Thanks for checking out {{ auction }}!  Don't forget to join:  Read the rules and click the green button at the bottom of this page:
https://{{ domain }}/auctions/{{auction.slug}}

{% if auction.allow_bidding_on_lots %}Once you join, make sure to bid on any lots you're interested in!{% else %}Don't miss out!{% endif %}

{% for lot in lots %}
* {{ lot.lot_name }}: https://{{lot.full_lot_link}}

{% endfor %}

Got questions?  Having trouble?  Just reply and we'll help you!

Turn these emails off at https://{{ domain }}/preferences/ or unsubscribe from everything: https://{{ domain }}/unsubscribe/{{unsubscribe}}/ Snail mail address: {{ mailing_address }}

Make sure to direct your reply to {{reply_to_email}}""",
            "html_content": """Hey {{ user.first_name }},<br><br>

Thanks for checking out {{ auction }}!  Don't forget to join:  <a href='https://{{ domain }}/auctions/{{auction.slug}}?src={{uuid}}'>Read the rules and click the green button at the bottom of this page</a>
<br><br>

{% if auction.allow_bidding_on_lots %}Once you join, make sure to bid on any lots you're interested in!{% else %}Don't miss out!{% endif %}<br><br>

{% for lot in lots %}
<b>{{ lot.lot_name }}</b><br>
<a href="https://{{lot.full_lot_link}}?src={{uuid}}">{% if lot.thumbnail %}<img src='https://{{ domain }}{{ lot.thumbnail.image.lot_list.url }}'></img><br>{% endif %}View and bid on this lot</a><br><br>

{% endfor %}

Got questions?  Having trouble?  Just reply and we'll help you!<br><br>

<br><small>Turn these emails off under your <a href='https://{{ domain }}/preferences/'>preferences</a>, or <a href="https://{{ domain }}/unsubscribe/{{unsubscribe}}/">unsubscribe</a> from this kind of thing, or snail mail us at {{ mailing_address }}<br><br>Make sure to direct your reply to {{reply_to_email}}</small>""",
        },
    ]

    for template_data in templates_to_update:
        EmailTemplate.objects.update_or_create(
            name=template_data["name"],
            defaults={
                "subject": template_data["subject"],
                "content": template_data["content"],
                "html_content": template_data["html_content"],
            },
        )


def reverse_func(apps, schema_editor):
    # Reversing would require storing the old templates, which is complex
    # For now, we'll just pass - manual rollback would be needed if required
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0210_merge_20251123_1159"),
        ("post_office", "0011_models_help_text"),
    ]

    operations = [
        migrations.RunPython(update_email_templates_with_reply_to, reverse_func),
    ]
