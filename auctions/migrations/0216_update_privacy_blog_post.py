# Generated manually to update the privacy blog post with payment and contact info details
from django.db import migrations


def update_privacy_blog_post(apps, schema_editor):
    BlogPost = apps.get_model("auctions", "BlogPost")

    privacy_content = """It's pretty important to know who you're sharing your personal information with.  This site keeps track of the following pieces of information:

- Your name

- Your email address

- Your phone number

- Your mailing address

- Your location

We don't collect or store any credit card information.  For the most part, your contact information isn't shared with anyone else on the site.  Here are the *only* times your information is shared with other users:

- Your email address is visible to all users on [your contact page](/account/), unless you hide it in [preferences](/preferences/).  Only signed in users can see any of your info.

- Your username is visible whenever you place a bid.  You can uncheck *Username Visible* in [preferences](/preferences/), which will hide your username when you bid.  Even if this is unchecked, your username will always be shown when you sell a lot or make a chat message.  (It's worth mentioning that when your username is hidden, you're still not completely anonymous.  Auction admins can still see your username, and, behind the scenes, a unique identifier is used and visible in the page source to other bidders.)

- If your username is an email address, that *will* be visible to non-logged-in users, and you'll probably get spam.  You'll get an email recommending that you [change your username](/username/), which is likely the reason you're reading this page.

- When you confirm your pickup location for an auction, all of your contact information is made available to the organizer of the auction.  This is visible to them even if you don't sell or buy any lots in the auction.

- When you sell a lot that is part of an auction, your real name is given to the winner of that lot.

- When you win a lot that is part of an auction, your real name is given to the seller of that lot.

- When you sell a lot that is *not* part of an auction, all your contact information is given to the winner of that lot.

- When you win a lot that is *not* part of an auction, all your contact information is given to the seller of that lot.

### On-site payments

When you pay for an invoice using PayPal or Square on this site, we store a record of your payment including:

- Your name

- Your email address

- Your mailing address (as provided by the payment processor)

- The amount paid and currency

- A transaction ID from the payment processor

We do *not* store any credit card numbers, bank account details, or other sensitive payment credentials.  All payment processing is handled securely by PayPal or Square.

When auction organizers connect their PayPal or Square accounts to receive payments, we store their merchant IDs and authentication tokens (encrypted at rest for Square).  This allows us to process payments on their behalf without storing any buyer payment credentials.

### Updating your contact information

When you [update your contact information](/contact_info/), the changes will also be applied to any auctions you've recently joined.  This ensures that auction organizers and sellers/buyers have your current information for lot exchanges.  A record of the change is kept in the auction's history for the organizer's reference.

### Law enforcement and security

We have never had any personal information requests from law enforcement, and we'll remove this message if we receive one.

We take your privacy and security seriously.  If you see something that doesn't seem right, reach out and we'll fix it."""

    BlogPost.objects.update_or_create(
        slug="privacy",
        defaults={
            "title": "Privacy",
            "body": privacy_content,
            "extra_js": "",
        },
    )


def reverse_func(apps, schema_editor):
    # Reversing would require storing the old content, which is complex
    # For now, we'll just pass - manual rollback would be needed if required
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0215_alter_auction_email_defaults"),
    ]

    operations = [
        migrations.RunPython(update_privacy_blog_post, reverse_func),
    ]
