"""Three guards that hold whether or not anyone remembered.

Each of these was a real bug, and each was only correct because every author had remembered the rule
in every place it applies. A test says it once instead.

- :class:`RouteAuthorizationTests` walks the whole URL conf as a stranger.
- :class:`TenancyInvariantTests` checks that no row points at two different tenants.
- :class:`BidderNumberTests` covers the club/auction rules for a bidder number, which has no database
  constraint behind it (see :func:`auctions.services.bidder_number_conflict`).
"""

import re

from django.contrib.auth.models import User
from django.db.models import F
from django.test import Client
from django.urls import get_resolver

from auctions.forms import ClubMemberAdminForm, CreateEditAuctionTOS, QuickAddTOS
from auctions.models import AuctionTOS, BapAward, ClubMember, Invoice, Lot
from auctions.services import bidder_number_conflict, free_bidder_number_for, set_member_bidder_number
from auctions.tests import StandardTestCase

#: Routes a signed-out or uninvolved visitor may legitimately open. Everything else must refuse.
#: Anything added here is a decision that the page is public, so it wants a reason.
PUBLIC_ROUTE_NAMES = frozenset(
    {
        # The site's own front door and public listings.
        "home", "allLots", "auctions", "all_auctions", "auction_main", "clubs", "leaderboard",
        "lot_by_pk", "lot_by_pk_and_slug", "lot_by_pk_qr", "user_lots", "userpage", "promo", "faq",
        "blog_post", "privacy_policy", "tos", "support", "dmca", "dmca_notice", "report_lot", "help",
        "help_guide",
        "club_detail", "speakers", "speaker_detail",
        # Account pages: their content is the caller's own, or a sign-in form.
        "account_login", "account_signup", "account_logout", "account_inactive", "account_email",
        "account_email_verification_sent", "account_reset_password", "account_reset_password_done",
        "account_reset_password_from_key", "account_reset_password_from_key_done", "account_set_password",
        "account_change_password", "account_delete", "account_deleted", "socialaccount_connections",
        "socialaccount_login_cancelled", "socialaccount_signup", "mobile_socialaccount_connections",
        "preferences", "notification_preferences", "contact_info", "change_username", "printing",
        "ignore_categories", "feedback", "messages", "my_bids", "my_invoices", "selling", "buying", "watched",
        "won_lots", "my_lot_report", "my_won_lot_csv", "my_lots_page_view_history", "user_api_keys",
        "chat_subscriptions", "auction_confirm", "all_my_users", "paypal_seller", "square_seller",
        "account_data_export", "library", "library_answer",
        "command_palette", "unsubscribe", "auction_join",
        # Beacons, health and static documents.
        "pageview", "form_abandoned", "check_username", "get_ad", "click_ad", "service_worker",
        "site_webmanifest", "javascript-catalog", "assetlinks", "apple_app_site_association",
        # Barcodes render bars for a number; they hold no membership lookup (see ClubBarcodeView).
        "club_barcode", "club_barcode_png",
        # Autocompletes scope their own querysets to what the caller may see.
        "auction-autocomplete", "auctiontos-autocomplete", "category-autocomplete", "lot-autocomplete",
        "species-autocomplete", "club-member-merge-autocomplete", "auction_custom_dropdown_options",
        "auction_custom_random_options", "htmx_lot", "save_lot_ajax", "guess_category", "get_auction_info",
        "club-member-autocomplete",
        # Aliases of lot_by_pk; the lot page is public.
        "lot_in_auction", "lot_in_auction_with_slug",
        # Third-party and account flows: allauth's re-auth prompt, the social round trip, the
        # Summernote editor iframe and Django admin's own sign-in form.
        "account_reauthenticate", "mobile-auth-social-done", "django_summernote-editor", "login",
        # Public club embeds, and the OAuth/MCP discovery documents.
        "club_events_embed", "club_past_events_embed", "club_auction_embed", "club_announcement_embed",
        "club_bap_embed", "club_calendar", "oauth-resource-metadata", "oauth-resource-metadata-path",
        "oauth-server-metadata", "oauth-server-metadata-issuer", "authorized-token-list", "device",
        "mobile-config", "square_success", "paypal_success",
        # The OIDC signing keys, once OIDC_RSA_KEYFILE is set: a JWKS is public keys, and a client
        # has to read it before it holds anything at all. 404s on a deployment with no key.
        "jwks-info", "oidc-connect-discovery-info",
    }
)  # fmt: skip


class RouteAuthorizationTests(StandardTestCase):
    """Every routed page refuses a stranger, unless it is named public above.

    The authorization model leans on base-class ordering: ``class X(APIView, AuctionViewMixin)`` and
    ``class X(AuctionViewMixin, APIView)`` look the same and behave differently, because APIView's
    dispatch never calls super(). Reading MROs catches that once; this catches it every time.
    """

    #: Fillers for the capture groups in a URL pattern, so a route can be requested at all.
    def _samples(self):
        tos = AuctionTOS.objects.filter(auction=self.online_auction).first()
        return {
            "slug": self.online_auction.slug,
            "pk": str(self.lot.pk),
            "lot": str(self.lot.pk),
            "lot_number": str(self.lot.pk),
            "uuid": "00000000-0000-0000-0000-000000000000",
            "bidder_number": (tos.bidder_number if tos else "1"),
            "tos": (tos.bidder_number if tos else "1"),
            "user_pk": str(self.user.pk),
            "auction_pk": str(self.online_auction.pk),
            "leave_as": "buyer",
            "action": "delete",
            "program": "bap",
        }

    def _routes(self):
        rows = []

        def walk(patterns, prefix=""):
            for pattern in patterns:
                if hasattr(pattern, "url_patterns"):
                    walk(pattern.url_patterns, prefix + str(pattern.pattern))
                else:
                    rows.append((prefix + str(pattern.pattern), getattr(pattern, "name", None)))

        walk(get_resolver().url_patterns)
        return rows

    def _fill(self, pattern, samples):
        url = pattern.lstrip("^").rstrip("$")
        url = re.sub(r"<(?:[^:>]+:)?([^>]+)>", lambda m: samples.get(m.group(1), "1"), url)
        url = re.sub(r"\(\?P<([^>]+)>[^)]*\)", lambda m: samples.get(m.group(1), "1"), url)
        return url if url.startswith("/") else "/" + url

    def test_no_route_serves_an_uninvolved_user(self):
        """A signed-in user who is in no auction and no club gets 200 only from a public page."""
        outsider = User.objects.create_user("route_outsider", "route_outsider@example.com", "x")
        client = Client()
        client.force_login(outsider)
        samples = self._samples()
        leaked = []
        for pattern, name in self._routes():
            if not name or name in PUBLIC_ROUTE_NAMES:
                continue
            url = self._fill(pattern, samples)
            if any(character in url for character in "(<[\\"):
                continue
            try:
                response = client.get(url, follow=False)
            except Exception:
                # A view that raises on a made-up pk is not serving anybody.
                continue
            if response.status_code == 200:
                leaked.append(f"{name} ({url})")
        self.assertEqual(
            leaked,
            [],
            "These routes answered 200 for a user with no auction and no club. Either gate them, or "
            "add them to PUBLIC_ROUTE_NAMES with a reason:\n  " + "\n  ".join(leaked),
        )


class TenancyInvariantTests(StandardTestCase):
    """No row may point at two tenants at once.

    ``EditLot`` accepted an ``auctiontos_winner`` from another auction, which put one auction's lot
    charge onto a stranger's invoice in theirs. The form is fixed; this says the shape is wrong
    however it got written.
    """

    def test_no_row_spans_two_tenants(self):
        checks = {
            "Lot.auction != Lot.auctiontos_seller.auction": Lot.objects.filter(
                auctiontos_seller__isnull=False, auction__isnull=False
            ).exclude(auctiontos_seller__auction=F("auction")),
            "Lot.auction != Lot.auctiontos_winner.auction": Lot.objects.filter(
                auctiontos_winner__isnull=False, auction__isnull=False
            ).exclude(auctiontos_winner__auction=F("auction")),
            "Invoice.auction != Invoice.auctiontos_user.auction": Invoice.objects.filter(
                auctiontos_user__isnull=False, auction__isnull=False
            ).exclude(auctiontos_user__auction=F("auction")),
            "Invoice.club != Invoice.club_member.club": Invoice.objects.filter(
                club__isnull=False, club_member__isnull=False
            ).exclude(club_member__club=F("club")),
            "AuctionTOS.auction != AuctionTOS.pickup_location.auction": AuctionTOS.objects.filter(
                pickup_location__isnull=False
            ).exclude(pickup_location__auction=F("auction")),
            "AuctionTOS.clubmember.club != AuctionTOS.auction.club": AuctionTOS.objects.filter(
                clubmember__isnull=False, auction__club__isnull=False
            ).exclude(clubmember__club=F("auction__club")),
            "BapAward.club_member.club != BapAward.lot.auction.club": BapAward.objects.filter(
                lot__isnull=False, lot__auction__club__isnull=False
            ).exclude(club_member__club=F("lot__auction__club")),
        }
        for label, queryset in checks.items():
            with self.subTest(invariant=label):
                self.assertEqual(queryset.count(), 0, f"{label}: {list(queryset[:3])}")

    def test_a_winner_from_another_auction_is_refused(self):
        """The form behind /api/lot/<pk>/, which is how the cross-tenant charge got written."""
        from auctions.forms import EditLot

        foreign = AuctionTOS.objects.exclude(auction=self.online_auction).first()
        self.assertIsNotNone(foreign, "fixture needs an AuctionTOS in another auction")
        form = EditLot(
            user=self.user,
            lot=self.lot,
            auction=self.online_auction,
            instance=self.lot,
            data={
                "lot_name": self.lot.lot_name,
                "auction": self.online_auction.pk,
                "quantity": 1,
                "auctiontos_winner": foreign.pk,
                "winning_price": "5",
                "reserve_price": "1",
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("auctiontos_winner", form.errors)


class BidderNumberTests(StandardTestCase):
    """The rules for a bidder number, which no database constraint enforces.

    A conditional ``UniqueConstraint`` creates no index on MariaDB (W036), so ``(club,
    bidder_number)`` was declared and absent for as long as it existed. Uniqueness now lives in the
    forms and in generation, which is what these cover.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from auctions.models import Club

        cls.club = Club.objects.create(name="Bidder number test club", abbreviation="BNTC")

    def _club_auction(self):
        """The fixture's auction, switched to club-managed so the ClubMember owns the numbers."""
        auction = self.online_auction
        auction.club = self.club
        auction.manage_users_through_club = "all"
        auction.save()
        return auction

    def test_a_number_used_in_the_auction_is_refused(self):
        taken = AuctionTOS.objects.filter(auction=self.online_auction).exclude(bidder_number="").first()
        self.assertIsNotNone(taken)
        self.assertIsNotNone(bidder_number_conflict(taken.bidder_number, auction=self.online_auction))

    def test_a_row_does_not_collide_with_itself(self):
        taken = AuctionTOS.objects.filter(auction=self.online_auction).exclude(bidder_number="").first()
        self.assertIsNone(bidder_number_conflict(taken.bidder_number, auction=self.online_auction, exclude_tos=taken))

    def test_an_unmanaged_auction_ignores_the_club(self):
        """Numbers in a plain auction are its own, even when the auction belongs to a club."""
        auction = self.online_auction
        auction.club = self.club
        auction.manage_users_through_club = ""
        auction.save()
        member = ClubMember.objects.create(club=self.club, name="Club only", bidder_number="777")
        self.assertIsNone(bidder_number_conflict("777", auction=auction))
        member.delete()

    def test_a_club_managed_auction_sees_the_club(self):
        """The same number is refused there, because the member's row will be copied in."""
        auction = self._club_auction()
        member = ClubMember.objects.create(club=self.club, name="Club only", bidder_number="778")
        holder = bidder_number_conflict("778", auction=auction)
        self.assertEqual(holder, member)

    def test_a_members_own_shadow_never_blocks_them(self):
        auction = self._club_auction()
        member = ClubMember.objects.create(club=self.club, name="Shadowed", bidder_number="779")
        shadow = AuctionTOS.objects.filter(auction=auction, clubmember=member).first()
        if shadow is None:
            shadow = AuctionTOS.objects.create(
                auction=auction,
                clubmember=member,
                pickup_location=auction.location_qs.first(),
                bidder_number="779",
                name="Shadowed",
            )
        self.assertIsNone(bidder_number_conflict("779", auction=auction, exclude_member=member))
        self.assertIsNone(bidder_number_conflict("779", auction=auction, exclude_tos=shadow))

    def test_quick_add_refuses_a_club_members_number(self):
        """Bulk-adding people to a club-managed auction must not take a number the club has spoken for.

        ``QuickAddTOS`` leaves the field editable, and the person holding it may have no row in this
        auction yet -- so checking only the auction's own rows would find it free and displace them
        the next time they sync.
        """
        auction = self._club_auction()
        ClubMember.objects.create(club=self.club, name="Has 780", bidder_number="780")
        form = QuickAddTOS(
            auction=auction,
            bidder_numbers_on_this_form=[],
            data={
                "name": "Somebody new",
                "bidder_number": "780",
                "email": "new780@example.com",
                "pickup_location": auction.location_qs.first().pk,
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("bidder_number", form.errors)

    def test_quick_add_allows_a_free_number(self):
        auction = self._club_auction()
        form = QuickAddTOS(
            auction=auction,
            bidder_numbers_on_this_form=[],
            data={
                "name": "Somebody new",
                "bidder_number": "791",
                "email": "new791@example.com",
                "pickup_location": auction.location_qs.first().pk,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_the_auction_tos_form_does_not_take_a_number_in_club_managed_mode(self):
        """In a club-managed auction the number belongs to the ClubMember, so this form can't set it.

        The field is disabled, which means Django ignores whatever is posted. Pinned here because
        the uniqueness rules only make sense once it is clear which form owns the number.
        """
        auction = self._club_auction()
        tos = AuctionTOS.objects.filter(auction=auction).exclude(bidder_number="").first()
        original = tos.bidder_number
        form = CreateEditAuctionTOS(
            is_edit_form=True,
            auctiontos=tos,
            auction=auction,
            instance=tos,
            data={
                "name": tos.name or "x",
                "bidder_number": "792",
                "pickup_location": tos.pickup_location_id,
                "email": tos.email or "x@example.com",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["bidder_number"], original)

    def test_the_auction_tos_form_refuses_a_duplicate_in_a_plain_auction(self):
        """With no club managing it, the auction owns its numbers and the form enforces that."""
        auction = self.online_auction
        auction.manage_users_through_club = ""
        auction.save()
        rows = list(AuctionTOS.objects.filter(auction=auction).exclude(bidder_number="")[:2])
        self.assertEqual(len(rows), 2, "fixture needs two numbered people in one auction")
        form = CreateEditAuctionTOS(
            is_edit_form=True,
            auctiontos=rows[0],
            auction=auction,
            instance=rows[0],
            data={
                "name": rows[0].name or "x",
                "bidder_number": rows[1].bidder_number,
                "pickup_location": rows[0].pickup_location_id,
                "email": rows[0].email or "x@example.com",
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("bidder_number", form.errors)

    def test_the_member_form_refuses_another_members_number(self):
        ClubMember.objects.create(club=self.club, name="Has 781", bidder_number="781")
        other = ClubMember.objects.create(club=self.club, name="Wants 781", bidder_number="782")
        form = ClubMemberAdminForm(
            club=self.club,
            instance=other,
            data={
                "name": "Wants 781",
                "email": "w@example.com",
                "bidder_number": "781",
                "contact_status": "contact",
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("bidder_number", form.errors)

    def test_a_member_keeps_their_own_number(self):
        member = ClubMember.objects.create(club=self.club, name="Keeps 783", bidder_number="783")
        form = ClubMemberAdminForm(
            club=self.club,
            instance=member,
            data={
                "name": "Keeps 783",
                "email": "k@example.com",
                "bidder_number": "783",
                "contact_status": "contact",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_generation_avoids_both_scopes(self):
        """free_bidder_number_for must not hand back a number the club or an auction already uses."""
        auction = self._club_auction()
        member = ClubMember.objects.create(club=self.club, name="Needs one")
        taken_in_club = set(
            ClubMember.objects.filter(club=self.club).exclude(pk=member.pk).values_list("bidder_number", flat=True)
        )
        taken_in_auction = set(
            AuctionTOS.objects.filter(auction=auction)
            .exclude(clubmember=member)
            .values_list("bidder_number", flat=True)
        )
        number = free_bidder_number_for(member)
        self.assertNotIn(number, taken_in_club)
        self.assertNotIn(number, taken_in_auction)

    def test_setting_a_number_displaces_the_holder(self):
        """Explicitly setting a number still moves whoever had it; only the forms refuse."""
        first = ClubMember.objects.create(club=self.club, name="First", bidder_number="784")
        second = ClubMember.objects.create(club=self.club, name="Second", bidder_number="785")
        set_member_bidder_number(second, "784")
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(second.bidder_number, "784")
        self.assertNotEqual(first.bidder_number, "784")
