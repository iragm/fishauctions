# Generated manually: correct the account-deletion section of the privacy blog post
from django.db import migrations


def update_privacy_blog_post(apps, schema_editor):
    # Same shape as 0326 and 0353: BlogPostView renders body_rendered, and the historical
    # MarkdownField in a data migration doesn't regenerate it on save, so render the HTML here and
    # write both fields.  privacy_content below is the full, current text.  What changed since 0353
    # is the "Deleting your account" section: which mailing-list contacts are removed (only the ones
    # from a membership you created yourself — a club's own record keeps its place on the club's
    # list), which auction records keep their contents (the ones an admin wrote down at the door),
    # and that your email address is taken out of the auction history that is kept.
    from markdownfield.rendering import render_markdown
    from markdownfield.validators import VALIDATOR_STANDARD

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

### Mobile app and notifications

If you use our mobile app, we store some information about your device so the app can work and notify you:

- The device name, platform (iOS or Android), and the app version

- A push notification token, which we send to Google's Firebase Cloud Messaging service so it can deliver notifications to your device

Push notifications are optional.  You choose whether to receive them, and you can turn them off at any time in the app or in your device's settings.  When you sign out of the app, the push token for that device is cleared, so a signed-out phone never receives your notifications.  We only use these tokens to deliver the notifications you've asked for.

The app can also take card payments in person using Square's Mobile Payments SDK &mdash; for example, when you pay an auction organizer at an in-person event.  As with online payments, the card is processed securely by Square and we never store card numbers or other sensitive payment credentials.

### Emails we send you

We keep a copy of the emails this site sends you &mdash; the message and the address it went to &mdash; for 30 days, so that we can tell you whether an email was sent and look into it if it bounced.  After that the copy is deleted automatically.

### Updating your contact information

When you [update your contact information](/contact_info/), the changes will also be applied to any auctions you've recently joined and to any clubs you're a member of.  This ensures that auction organizers, club admins, sellers, and buyers have your current information.  A record of the change is kept in the auction and club history for the organizers' reference.

### Club memberships

If you join a fish club through this site or are added to a club by a club admin, the club will have access to:

- Your name

- Your email address

- Your phone number

- Your mailing address

This information is visible to club members with the appropriate admin permissions.  You can check and update your contact information at any time through [your contact info page](/contact_info/).

Club admins may export member lists.  If you do not want your contact information shared in these exports, contact the club admin to update your contact preferences.

### Deleting your account

You can delete your account at any time from [your preferences](/preferences/) &mdash; there's a *Delete account* link under *More*, and it works the same way in the app.  The page tells you exactly what is removed before you confirm, and nothing happens for 30 days: signing in again during that time cancels the deletion.

When the deletion goes through, we remove your username, email address, password, any linked Google account, your name, phone number, mailing address, location, preferences, watched lots, search and browsing history, the devices you've used the app on (and their push notification tokens), and any PayPal or Square account you connected.

Some records stay, because they're other people's records too:

- Auctions you took part in keep your bidder number and the amounts on your invoices, so that sellers' payouts and clubs' past auctions still add up.  Your name and contact details are removed from them.  The exception is a record an auction admin wrote down when you turned up in person: that's the auction's own note of who was there, taken from what you told them at the door, so it's kept as it is and only the link to your account is removed.

- Auction and club history keeps a record of what happened, without your account attached.  Where those notes quote your email address, the address is replaced with *[deleted]*.

- Lots you sold stay in the auction they were sold in, without a seller name.  Lots that weren't part of an auction are taken off the site.

- Club membership records that a club admin created or has edited stay with the club, including dues paid, award points, and your place on the club's mailing list if they use Mailchimp or Brevo.  That's the club's own record of a member, collected by the club, so we remove the link to your account and nothing else &mdash; if you want the club's record deleted, or you want them to stop emailing you, ask the club.  A membership you signed yourself up for, that no admin has edited, is deleted with your account, and so is the mailing list contact that came from it.

- Chat messages and comments stay on the lots they belong to, with no name attached.

### Law enforcement and security

We have never had any personal information requests from law enforcement, and we'll remove this message if we receive one.

We take your privacy and security seriously.  If you see something that doesn't seem right, reach out and we'll fix it."""

    BlogPost.objects.update_or_create(
        slug="privacy",
        defaults={
            "title": "Privacy",
            "body": privacy_content,
            "body_rendered": render_markdown(privacy_content, VALIDATOR_STANDARD),
            "extra_js": "",
        },
    )


def reverse_func(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0354_single_club_memberships_are_member_owned"),
    ]

    operations = [
        migrations.RunPython(update_privacy_blog_post, reverse_func),
    ]
