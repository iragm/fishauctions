"""Make the account OpenAI's reviewer signs in with, and the auction its test cases name.

``chatgpt_submission``'s five test cases say "the spring auction" and "lot 14", so this builds
exactly that: an unlisted in-person auction run by a separate account, with lot submission open, a
lot 14, and two lots the reviewer already entered so "what do I owe" has an invoice to read. The
reviewer is a participant, not an admin, which is also what the third negative case relies on.

Safe to run again: it finds what it made by username and title and fills in only what is missing.
A new password is printed each run unless ``--password`` is given::

    docker exec django python3 manage.py chatgpt_reviewer --email reviewer@example.com
"""

from __future__ import annotations

import datetime
import secrets

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from auctions.models import Auction, AuctionTOS, Invoice, Lot, PickupLocation

User = get_user_model()

REVIEWER = "chatgpt-review"
ORGANIZER = "chatgpt-review-club"
TITLE = "Spring Auction"

RULES = (
    "<p>Bring your lots bagged and labelled by 6pm. Lots are sold in number order. The club keeps "
    "20% of each sale and $1 per lot entered. Pay at the front desk before you leave.</p>"
)

#: Lot 14 is the one the test case asks about, so it gets the full description.
LOTS = [
    ("Java fern on driftwood", 1, 8, ""),
    ("Cherry shrimp", 10, 10, ""),
    ("Bristlenose pleco, juvenile", 3, 12, ""),
    ("Amazon sword", 1, 5, ""),
    ("Guppies, Endler hybrid", 6, 8, ""),
    ("Corydoras sterbai", 4, 20, ""),
    ("Anubias nana", 2, 6, ""),
    ("Kribensis pair", 2, 15, ""),
    ("Hornwort, large bag", 1, 3, ""),
    ("Zebra otocinclus", 3, 18, ""),
    ("Cryptocoryne wendtii", 3, 6, ""),
    ("Neon tetras", 10, 12, ""),
    ("Ramshorn snails", 10, 3, ""),
    (
        "Apistogramma cacatuoides pair",
        2,
        25,
        "<p>Double red, tank raised. About an inch and a half. Bagged with water from their tank.</p>",
    ),
    ("Vallisneria", 5, 5, ""),
    ("White cloud minnows", 6, 6, ""),
    ("Duckweed", 1, 2, ""),
    ("Betta, male halfmoon", 1, 15, ""),
]

#: Entered by the reviewer, so their invoice carries the lot fees.
OWN_LOTS = [("Moss balls", 3, 5), ("Rummynose tetras", 8, 14)]


class Command(BaseCommand):
    help = "Create or refresh the ChatGPT reviewer's account and its demo auction."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="The address the reviewer signs in with")
        parser.add_argument("--password", help="Defaults to a new random one, printed")

    @transaction.atomic
    def handle(self, *args, **options):
        password = options["password"] or secrets.token_urlsafe(12)
        reviewer = _account(REVIEWER, options["email"], password)
        organizer = _account(ORGANIZER, None, None)

        start = timezone.now() + datetime.timedelta(days=60)
        auction = Auction.objects.filter(created_by=organizer, title=TITLE).first()
        if not auction:
            auction = Auction.objects.create(
                created_by=organizer,
                title=TITLE,
                is_online=False,
                date_start=start,
                lot_submission_start_date=timezone.now(),
                lot_submission_end_date=start,
                summernote_description=RULES,
                winning_bid_percent_to_club=20,
                lot_entry_fee=1,
                promote_this_auction=False,
            )
        elif not auction.can_submit_lots or auction.date_start < timezone.now() + datetime.timedelta(days=14):
            # A review can take weeks; keep the auction ahead of it.
            auction.date_start = start
            auction.lot_submission_start_date = timezone.now()
            auction.lot_submission_end_date = start
            auction.save()

        location = PickupLocation.objects.filter(auction=auction).first() or PickupLocation.objects.create(
            auction=auction,
            name="Club meeting hall",
            description="The community center's back room",
            pickup_time=auction.date_start,
        )
        AuctionTOS.objects.get_or_create(
            auction=auction, user=organizer, defaults={"pickup_location": location, "is_admin": True}
        )
        seller, _ = AuctionTOS.objects.get_or_create(
            auction=auction,
            user=None,
            name="Pat Example",
            defaults={"pickup_location": location, "manually_added": True},
        )
        mine, _ = AuctionTOS.objects.get_or_create(
            auction=auction, user=reviewer, defaults={"pickup_location": location}
        )

        if not Lot.objects.filter(auction=auction).exists():
            for name, quantity, price, description in LOTS:
                _lot(auction, seller, name, quantity, price, description)
            for name, quantity, price in OWN_LOTS:
                _lot(auction, mine, name, quantity, price, "")
        Invoice.objects.get_or_create(auctiontos_user=mine, defaults={"auction": auction})

        self.stdout.write(f"Auction: {auction.get_absolute_url()}")
        self.stdout.write(f"Username: {REVIEWER}  Email: {reviewer.email}  Password: {password}")


def _account(username: str, email: str | None, password: str | None):
    user = User.objects.filter(username=username).first() or User(username=username)
    if email:
        user.email = email
    if password:
        user.set_password(password)
    elif not user.pk:
        user.set_unusable_password()
    user.save()
    if email:
        EmailAddress.objects.filter(user=user).exclude(email__iexact=email).delete()
        EmailAddress.objects.update_or_create(user=user, email=email, defaults={"verified": True, "primary": True})
    return user


def _lot(auction, tos, name, quantity, price, description):
    return Lot.objects.create(
        auction=auction,
        auctiontos_seller=tos,
        user=tos.user,
        lot_name=name,
        quantity=quantity,
        reserve_price=price,
        summernote_description=description,
    )
