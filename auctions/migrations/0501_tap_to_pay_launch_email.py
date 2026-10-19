"""The ``tap_to_pay_launch_email`` email, sent once by ``manage.py tap_to_pay_launch_announcement``.

Apple's Tap to Pay on iPhone marketing toolkit (US, September 2026) supplies this email as a picture,
not HTML, so it is rebuilt here from the toolkit's own copy deck and its hero visual. Apple forbids
writing your own: every sentence below is the toolkit's, with [partner product] and [partner name]
filled in, and the offer tile left out because there is no offer. The four disclaimers are the ones
the guide requires for what this email shows (the requirements, the card-storage line, Apple Pay, and
the contactless mark printed on the card in the visual).
"""

from django.db import migrations

SUBJECT = "Tap to Pay on iPhone is now available with auction.fish"
TEXT = """{% load email_tags %}Tap to Pay on iPhone
Accept contactless payments right on your iPhone.

Tap to Pay on iPhone is now available with auction.fish. That means you can accept all types of in-person, contactless payments, from physical debit and credit cards to Apple Pay and other digital wallets. Right on your iPhone.

Get started with Tap to Pay on iPhone: https://{{ domain }}{% url 'square_seller' %}

Expand where you do business.
Reach more customers, accept payments on the go, and explore new setups, like line busting. All you need is your iPhone.

Streamline checkout with no additional hardware.
Tap to Pay on iPhone is easy to set up and use. No card readers or terminals required.

Privacy and security of iPhone built in.
Apple doesn't store card numbers on Apple servers, helping protect your business and customer data.*

Get started in a few steps.
1. Download the auction.fish app.
2. Complete sign-up information.
3. Accept in-person, contactless payments, right on iPhone.

Tap to Pay on iPhone requires a supported payment app and the latest version of iOS. Update to the latest version by going to Settings > General > Software Update. Some contactless cards may not be accepted by your payment app. Transaction limits may apply. Tap to Pay on iPhone is not available in all markets. For Tap to Pay on iPhone countries and regions, see https://developer.apple.com/tap-to-pay/regions/.

*Encrypted card numbers are temporarily stored on iPhone only for transactions made in Store and Forward mode.

Apple Pay is a service provided by Apple Payments Services LLC, a subsidiary of Apple Inc. Neither Apple Inc. nor Apple Payments Services LLC is a bank. Any card used in Apple Pay is offered by the card issuer.

The Contactless Indicator mark, consisting of four graduating arcs, is a trademark owned by and used with permission of EMVCo, LLC.

{% email_footer_text %}
"""
HTML = """{% extends "email/base.html" %}{% load email_tags %}
{% block content %}
<div style="text-align:center;">
<div style="font-size:30px;font-weight:bold;line-height:1.2;color:#000000;">Tap to Pay on iPhone</div>
<div style="font-size:22px;line-height:1.3;color:#6e6e73;margin:8px 0 16px;">Accept contactless payments right on your iPhone.</div>
Tap to Pay on iPhone is now available with auction.fish.&#9674; That means you can accept all types of in-person, contactless payments &mdash; from physical debit and credit cards to Apple Pay and other digital wallets.&#9674; Right on your iPhone.
{% email_button %}<a href="https://{{ domain }}{% url 'square_seller' %}">Get started with Tap to Pay on iPhone</a>{% endemail_button %}
<img src="https://{{ domain }}/static/images/tap_to_pay/launch_email_visual.jpg" alt="A contactless card held to an iPhone showing the Tap to Pay screen" width="600" style="display:block;width:100%;max-width:600px;height:auto;border:0;margin:8px 0 24px;">
</div>
<h3>Expand where you do business.</h3>
Reach more customers, accept payments on the go, and explore new setups, like line busting. All you need is your iPhone.
<h3>Streamline checkout with no additional hardware.</h3>
Tap to Pay on iPhone is easy to set up and use. No card readers or terminals required.
<h3>Privacy and security of iPhone built in.</h3>
Apple doesn&rsquo;t store card numbers on Apple servers, helping protect your business and customer data.&#9674;
<h3>Get started in a few steps.</h3>
<ol>
<li>Download the auction.fish app.</li>
<li>Complete sign-up information.</li>
<li>Accept in-person, contactless payments &mdash; right on iPhone.</li>
</ol>
<div style="margin-top:24px;font-size:12px;line-height:1.5;color:#6e6e73;">
&#9674; Legal Disclaimers<br><br>
<b>Tap to Pay on iPhone Requirements:</b> Tap to Pay on iPhone requires a supported payment app and the latest version of iOS. Update to the latest version by going to Settings &gt; General &gt; Software Update. Some contactless cards may not be accepted by your payment app. Transaction limits may apply. Tap to Pay on iPhone is not available in all markets. For Tap to Pay on iPhone countries and regions, see <a href="https://developer.apple.com/tap-to-pay/regions/">https://developer.apple.com/tap-to-pay/regions/</a>.<br><br>
<b>Tap to Pay on iPhone Encryption and Storage:</b> Encrypted card numbers are temporarily stored on iPhone only for transactions made in Store and Forward mode.<br><br>
<b>Apple Pay:</b> Apple Pay is a service provided by Apple Payments Services LLC, a subsidiary of Apple Inc. Neither Apple Inc. nor Apple Payments Services LLC is a bank. Any card used in Apple Pay is offered by the card issuer.<br><br>
<b>Contactless Indicator Mark:</b> The Contactless Indicator mark, consisting of four graduating arcs, is a trademark owned by and used with permission of EMVCo, LLC.
</div>
{% endblock %}
"""


def forwards(apps, schema_editor):
    EmailTemplate = apps.get_model("post_office", "EmailTemplate")
    EmailTemplate.objects.update_or_create(
        name="tap_to_pay_launch_email",
        language="",
        defaults={"subject": SUBJECT, "content": TEXT, "html_content": HTML},
    )


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0500_android_testers_email"),
        ("post_office", "0011_models_help_text"),
    ]

    # Not deleted going back: post_office.Email cascades from its template, taking the sent log with it.
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
