"""The "paid" invoice email said "You owe a total of $12.00", and only after that, and only when the
invoice had a pickup location, that it was paid. People who had paid cash at the door read it as a bill.

A paid invoice now says it's paid and is a receipt, first, with or without a location, and leaves off the
auction's payment instructions. A template edited in the admin keeps whatever part of this no longer
matches it.
"""

from django.db import migrations

AMOUNT = (
    "{% if invoice.user_should_be_paid %}You will be paid{% else %}You owe a total of{% endif %} "
    "${{ invoice.absolute_amount|floatformat:2}}{% if location %}"
)
PAID_AMOUNT = (
    '{% if invoice.status == "PAID" %}Your invoice is paid and nothing more is owed; this is your receipt. '
    "{% if invoice.user_should_be_paid %}You were paid{% else %}You paid{% endif %}"
    "{% elif invoice.user_should_be_paid %}You will be paid{% else %}You owe a total of{% endif %} "
    "${{ invoice.absolute_amount|floatformat:2}}{% if location %}"
)
INSTRUCTIONS = '{{ invoice.auction.invoice_payment_instructions | default:""}}'
UNPAID_INSTRUCTIONS = '{% if invoice.status != "PAID" %}' + INSTRUCTIONS + "{% endif %}"
RECEIPT = '{% if invoice.status == "PAID" %}RECEIPT_BREAKYour invoice has been paid in full, this is just a receipt for your records.{% endif %}'

#: ``[(field, old, new)]`` for the invoice_ready template.
CHANGES = [
    (
        "html_content",
        AMOUNT + "\n" + RECEIPT.replace("RECEIPT_BREAK", "<br><br>") + "\n",
        PAID_AMOUNT + "\n",
    ),
    (
        "content",
        AMOUNT + "\n\n" + RECEIPT.replace("RECEIPT_BREAK", "") + "\n\n",
        PAID_AMOUNT + "\n\n",
    ),
    ("html_content", "{% endemail_button %}" + INSTRUCTIONS, "{% endemail_button %}" + UNPAID_INSTRUCTIONS),
    ("content", "\n\n" + INSTRUCTIONS + "\n", "\n\n" + UNPAID_INSTRUCTIONS + "\n"),
]


def _apply(apps, forward):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    for template in EmailTemplate.objects.filter(name="invoice_ready"):
        changed = set()
        for field, old, new in CHANGES:
            if not forward:
                old, new = new, old
            body = getattr(template, field) or ""
            if body.count(old) == 1:
                setattr(template, field, body.replace(old, new, 1))
                changed.add(field)
        if changed:
            template.save(update_fields=sorted(changed))


def forwards(apps, schema_editor):
    _apply(apps, forward=True)


def backwards(apps, schema_editor):
    _apply(apps, forward=False)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0471_watched_items_ending_link"),
        ("post_office", "0011_models_help_text"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
