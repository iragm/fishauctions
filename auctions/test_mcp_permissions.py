"""Every tool on ``/mcp/``, pointed at somebody else's club and somebody else's auction.

The catalogue is one registry with one dispatcher, which is what makes an audit like this possible
at all: there is no second code path where a permission could be checked differently. What there is
no substitute for is *running* every tool as somebody who should not be allowed to, because the
gates are per-resolver and a new one is a new chance to forget.

So this is a driver rather than a list of hand-written cases. Two complete, separate tenants are
built, and then **every registered action** is run against tenant A's objects by two people who have
no business there: a plain member of nothing, and a legitimate administrator of tenant B — who is
the interesting one, because they hold real club and auction permissions and the question is only
whether those are correctly scoped to their own club and their own auction.

Two invariants, and both are checked for every action rather than argued about per action:

* **Nothing about tenant A comes back.** Everything private in tenant A carries the word
  ``Zorblatt`` — a participant's name, their email, their memo, a lot's name, the auction's title.
  The tool's whole answer is serialised and searched for those strings, and a string the *caller
  supplied* is never counted: half these tools take a search term, and a tool that answers
  "no page matching “Zorblatt”" has repeated the question, not answered it. So the probe typed into
  every free-text parameter is the bare word and the things looked for are the whole stored
  strings, which only the database knows. The club's *name* deliberately carries nothing: a club's
  existence and name are on the public club finder, so a listing that names one is not a leak and
  this audit should not pretend it is.
* **Nothing about tenant A changes.** Every row of every model that matters is captured before and
  after: a row that belongs to tenant A may not be altered or deleted, and a row created by the
  call may not reference tenant A or carry its sentinel.

An action that legitimately does something for the *caller* — ``set_my_auction`` writing their own
pointer, ``request_a_skill`` filing their own note — passes both, which is the point of writing
the invariants about tenant A rather than about "did anything happen".
"""

from __future__ import annotations

import datetime
import json

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.utils import timezone

from auctions import palette_actions
from auctions.models import (
    Auction,
    AuctionDropdown,
    AuctionTOS,
    Club,
    ClubEvent,
    ClubMember,
    Invoice,
    Lot,
    PickupLocation,
    UserData,
    VolunteerJob,
)

#: The word every private string in tenant A is built out of, and the probe typed into every
#: free-text parameter. Not on the club's name -- see the module docstring.
SENTINEL = "Zorblatt"

#: Tenant A's bidder number. Deliberately **not** one of the secrets below: it is supplied as a
#: parameter by half of these calls, so an answer containing it has repeated the question, and a
#: four-digit string is the one thing here that could turn up in an unrelated answer by accident.
#: What matters is whether the *name* behind it comes back, and that is checked.
THEIR_BIDDER = "9317"


#: The strings only tenant A's database knows. A leak is one of these in an answer, minus anything
#: the caller put in the question.
def secrets() -> tuple[str, ...]:
    return (
        f"{SENTINEL} Member",
        f"{SENTINEL} Bidder",
        f"{SENTINEL} Guppy Trio",
        f"{SENTINEL} Spring Auction",
        f"{SENTINEL} Meeting",
        f"{SENTINEL}MEMO",
        f"{SENTINEL.lower()}-member@example.invalid",
        f"{SENTINEL.lower()}-bidder@example.invalid",
    )


#: The models an audit of "did anything of theirs change" has to watch. Everything an auction or a
#: club is made of, plus the two rows a write could quietly create against somebody else's tenant.
#: The three at the end were added when the auction setup tools were: a pickup location, a dropdown
#: option and a request for volunteers are all rows an outsider could otherwise have created inside
#: somebody else's auction without this driver noticing, because the "nothing of theirs changed"
#: invariant can only watch tables it has been given.
WATCHED = (
    Auction,
    AuctionTOS,
    Lot,
    Invoice,
    Club,
    ClubMember,
    ClubEvent,
    UserData,
    PickupLocation,
    AuctionDropdown,
    VolunteerJob,
)


def _columns(model) -> list[str]:
    """Every concrete column on a model, so a snapshot compares values and not just which rows exist."""
    return [field.attname for field in model._meta.concrete_fields]


class CrossTenantTestCase(TestCase):
    """Two tenants that share nothing, and two people with no business in the first one."""

    def setUp(self):
        soon = timezone.now() + datetime.timedelta(days=10)
        started = timezone.now() - datetime.timedelta(days=1)

        # --- tenant A: everything the audit says must not be reachable --------------------
        self.their_owner = User.objects.create_user(
            username="their_owner", password="x", email="their-owner@example.invalid"
        )
        self.their_club = Club.objects.create(
            name="Northside Aquarists", abbreviation="NA", enable_breeder_award_program=True
        )
        ClubMember.objects.create(
            club=self.their_club,
            user=self.their_owner,
            name="Northside Officer",
            permission_admin=True,
            permission_add_edit=True,
            permission_view=True,
            permission_manage_bap=True,
            permission_manage_auctions=True,
        )
        self.their_member = ClubMember.objects.create(
            club=self.their_club,
            name=f"{SENTINEL} Member",
            email=f"{SENTINEL.lower()}-member@example.invalid",
        )
        self.their_auction = Auction.objects.create(
            created_by=self.their_owner,
            club=self.their_club,
            title=f"{SENTINEL} Spring Auction",
            is_online=False,
            date_start=started,
            date_end=soon,
            promote_this_auction=False,
        )
        self.their_location = PickupLocation.objects.create(
            name="Northside hall", auction=self.their_auction, pickup_time=soon
        )
        self.their_tos = AuctionTOS.objects.create(
            auction=self.their_auction,
            pickup_location=self.their_location,
            name=f"{SENTINEL} Bidder",
            email=f"{SENTINEL.lower()}-bidder@example.invalid",
            bidder_number=THEIR_BIDDER,
            memo=f"{SENTINEL}MEMO",
        )
        # A custom lot number, because that is what ``_resolve_lot`` matches on: without one the
        # probe below never reaches a lot at all, and half the audit would be asserting that a
        # permission check refused a lot it had failed to find.
        self.their_lot = Lot.objects.create(
            lot_name=f"{SENTINEL} Guppy Trio",
            auction=self.their_auction,
            auctiontos_seller=self.their_tos,
            quantity=1,
            lot_number_int=41,
            custom_lot_number="Z41",
        )
        self.their_event = ClubEvent.objects.create(
            club=self.their_club,
            title=f"{SENTINEL} Meeting",
            date_start=soon,
            date_end=soon + datetime.timedelta(hours=2),
        )
        self.their_invoice = Invoice.objects.get_or_create(auctiontos_user=self.their_tos)[0]
        # One row each for the three setup tables, so the driver has something of theirs to try to
        # change rather than only something to try to add to.
        self.their_dropdown_option = AuctionDropdown.objects.create(
            auction=self.their_auction, user=self.their_owner, value=f"{SENTINEL}Fish"
        )
        self.their_volunteer_job = VolunteerJob.objects.create(
            auction=self.their_auction,
            created_by=self.their_owner,
            description=f"{SENTINEL} table duty",
            people_needed=2,
        )

        # --- tenant B: a real administrator, of somewhere else ---------------------------
        self.our_owner = User.objects.create_user(username="our_owner", password="x", email="ours@example.invalid")
        self.our_club = Club.objects.create(
            name="Southside Bettas", abbreviation="SB", enable_breeder_award_program=True
        )
        ClubMember.objects.create(
            club=self.our_club,
            user=self.our_owner,
            name="Southside Officer",
            permission_admin=True,
            permission_add_edit=True,
            permission_view=True,
            permission_manage_bap=True,
            permission_manage_auctions=True,
        )
        self.our_auction = Auction.objects.create(
            created_by=self.our_owner,
            club=self.our_club,
            title="Southside Autumn Sale",
            is_online=False,
            date_start=started,
            date_end=soon,
            promote_this_auction=False,
        )
        self.our_location = PickupLocation.objects.create(
            name="Southside hall", auction=self.our_auction, pickup_time=soon
        )
        AuctionTOS.objects.create(
            auction=self.our_auction,
            pickup_location=self.our_location,
            user=self.our_owner,
            name="Southside Officer",
            email="ours@example.invalid",
            bidder_number="12",
            is_admin=True,
        )

        # --- and somebody in nothing at all ----------------------------------------------
        self.outsider = User.objects.create_user(username="outsider", password="x", email="outsider@example.invalid")

        # --- and an ordinary bidder *inside* tenant A -------------------------------------
        # The persona the two above cannot cover. A participant is allowed in: they see the
        # auction, they see the lots, they may add their own. What they may not do is read another
        # participant's email, memo or invoice, or change anything that is not theirs.
        self.their_bidder_user = User.objects.create_user(
            username="their_bidder", password="x", email="bidder@example.invalid"
        )
        self.bidders_own_tos = AuctionTOS.objects.create(
            auction=self.their_auction,
            pickup_location=self.their_location,
            user=self.their_bidder_user,
            name="Ordinary Bidder",
            email="bidder@example.invalid",
            bidder_number="88",
        )

        for user in (self.their_owner, self.our_owner, self.outsider, self.their_bidder_user):
            UserData.objects.get_or_create(user=user)

    # -- the driver ---------------------------------------------------------------------

    def _run(self, user, name, params):
        request = RequestFactory().post("/")
        request.user = user
        # What ``mcp.tools.call_tool`` sets: an agent is not looking at a page, so nothing can be
        # inferred from one. Passing a page here would be handing the caller context they never had.
        request.palette_page = {}
        return palette_actions.run_action(request, name, params)

    def _their_values(self):
        """A value for every parameter in the registry, pointing at tenant A wherever it names one."""
        return {
            # who and what, all of it theirs
            "auction": self.their_auction.slug,
            "club": self.their_club.slug,
            "lot": "Z41",
            "lot_id": self.their_lot.pk,
            "person": THEIR_BIDDER,
            "bidder": THEIR_BIDDER,
            "bidder_number": THEIR_BIDDER,
            "winner": THEIR_BIDDER,
            "event": SENTINEL,
            "copy_from": self.their_auction.slug,
            "name": SENTINEL,
            "query": SENTINEL,
            "search": SENTINEL,
            # values that make an action get as far as its permission check
            "email": "audit@example.invalid",
            "new_name": "Audit Rename",
            "new_title": "Audit Retitle",
            "memo": "AUDITMEMO",
            "phone_number": "555-0100",
            "address": "1 Audit Street",
            "amount": 5,
            "label": "Audit adjustment",
            "points": 3,
            "notes": "audit",
            "price": 7,
            "status": "paid",
            "decision": "approve",
            "message": "audit message",
            "text": "audit announcement",
            "title": "Audit Auction",
            "date_start": (timezone.now() + datetime.timedelta(days=60)).strftime("%Y-%m-%dT%H:%M"),
            "starts": (timezone.now() + datetime.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M"),
            "ends": (timezone.now() + datetime.timedelta(days=30, hours=2)).strftime("%Y-%m-%dT%H:%M"),
            "description": "audit event",
            "location": "audit hall",
            "setting": "use_check_in_mode",
            "value": "true",
            "species": "Poecilia reticulata",
            "scientific_name": "Poecilia reticulata",
            "names": "audit name",
            "common_name": "audit name",
            "variety": "Audit Strain",
            "strain_of": "Poecilia reticulata",
            "url": "https://example.invalid/fish.jpg",
            "image_id": 1,
            "caption": "audit",
            "skill": "audit skill",
            "reason": "audit reason",
            "agree_to_rules": True,
            "pickup_location": "Northside hall",
            "quantity": 1,
            "lots": "one audit lot",
            # The pricing and refund tools. ``item`` is the probe for price_history, so the leak
            # audit really drives it; ``percent``/``paid_by`` take refund_lot past its own argument
            # parsing and up to the permission check, which is the line being tested.
            "item": SENTINEL,
            "years": 3,
            # The page-only writes (``mcp_only``). Each of these takes its action past argument
            # parsing and up to the permission check, which is the line this audit tests -- an
            # action that bails on a missing argument proves nothing about whether it leaks.
            "restore": False,
            "permanently": False,
            "active": False,
            "renewing": True,
            "hide": True,
            "rating": "positive",
            "as": "buyer",
            "angle": 90,
            "primary": False,
            "genus": "Poecilia",
            "category": "Cichlids",
            "date": timezone.now().strftime("%Y-%m-%d"),
            "count": 2,
            "all_lots": True,
            "percent": 50,
            "paid_by": "club",
            "page": "auction_main",
            # The auction and account setup tools. Each of these is what a real call would carry, so
            # a missing gate shows up as a row that moved rather than as a question coming back.
            "location_coordinates": "42.36,-71.06",
            "coordinates": "42.36,-71.06",
            "by_mail": False,
            "users_must_coordinate_pickup": False,
            "pickup_time": (timezone.now() + datetime.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M"),
            "option": f"{SENTINEL}Audit",
            "field": "Lot name",
            "people_needed": 2,
            "bounty": 5,
            "job": f"{SENTINEL} table duty",
            "show": "unused",
            "username": "audit-rename",
            "first_name": "Audit",
            "last_name": "Person",
            "custom_checkbox": True,
            "reference_link": "https://example.invalid/fish",
        }

    def _params_for(self, action):
        """Only what this action documents, because ``run_action`` refuses anything else."""
        values = self._their_values()
        return {key: value for key, value in values.items() if key in action.params}

    def _snapshot(self):
        # ``pk`` rather than "id": not every model here names its primary key that way, and a
        # snapshot that raises is a snapshot that never catches anything.
        return {
            model.__name__: {row["pk"]: dict(row) for row in model.objects.all().values("pk", *_columns(model))}
            for model in WATCHED
        }

    def _their_pks(self):
        """Which rows in the snapshot belong to tenant A."""
        return {
            "Auction": {self.their_auction.pk},
            "AuctionTOS": {self.their_tos.pk},
            "Lot": {self.their_lot.pk},
            "Invoice": {self.their_invoice.pk},
            "Club": {self.their_club.pk},
            "ClubMember": set(ClubMember.objects.filter(club=self.their_club).values_list("pk", flat=True)),
            "ClubEvent": {self.their_event.pk},
            "UserData": set(UserData.objects.filter(user=self.their_owner).values_list("pk", flat=True)),
            "PickupLocation": {self.their_location.pk},
            "AuctionDropdown": {self.their_dropdown_option.pk},
            "VolunteerJob": {self.their_volunteer_job.pk},
        }

    def _assert_nothing_of_theirs_moved(self, before, after, where, *, may_create_inside=False):
        """Nothing of tenant A's may change or vanish, and nothing new may land inside it.

        ``may_create_inside`` is for the participant persona: somebody who has joined an auction is
        *supposed* to be able to add a lot to it, so for them the invariant is only about rows that
        already existed and were not theirs.
        """
        theirs = self._their_pks()
        for model, rows in after.items():
            was = before[model]
            for pk in theirs[model]:
                self.assertIn(pk, rows, f"{where} deleted {model} {pk}")
                self.assertEqual(was.get(pk), rows[pk], f"{where} changed {model} {pk}")
            if may_create_inside:
                continue
            for pk, row in rows.items():
                if pk in was:
                    continue
                text = json.dumps(row, default=str)
                for secret in secrets():
                    self.assertNotIn(secret, text, f"{where} created a {model} carrying their data: {row}")
                for column, value in (
                    ("auction_id", self.their_auction.pk),
                    ("club_id", self.their_club.pk),
                    ("auctiontos_seller_id", self.their_tos.pk),
                    ("auctiontos_winner_id", self.their_tos.pk),
                    ("auctiontos_user_id", self.their_tos.pk),
                ):
                    self.assertNotEqual(row.get(column), value, f"{where} created a {model} inside their tenant")


class NobodyElsesDataTests(CrossTenantTestCase):
    """Reads: no tool may say anything private about a tenant the caller is not in."""

    def _assert_no_leak(self, user, who):
        leaked = []
        for name, action in sorted(palette_actions.ACTIONS.items()):
            params = self._params_for(action)
            result = self._run(user, name, params)
            answer = json.dumps(result, default=str)
            asked = json.dumps(params, default=str)
            for secret in secrets():
                # Supplied, then echoed, is the question coming back -- not an answer to it.
                if secret in answer and secret not in asked:
                    leaked.append(f"{name} leaked “{secret}”: {answer[:300]}")
        self.assertEqual(leaked, [], f"{who} was told about somebody else's auction")

    def test_a_member_of_nothing_is_told_nothing(self):
        self._assert_no_leak(self.outsider, "an outsider")

    def test_an_administrator_of_another_club_is_told_nothing(self):
        """The interesting one: real club and auction permissions, held somewhere else."""
        self._assert_no_leak(self.our_owner, "another club's admin")

    def test_a_bidder_is_not_told_about_the_other_bidders(self):
        """A participant sees the auction and its lots. They do not see each other's contact details.

        So this checks a narrower set than the two above: the auction's title and a lot's name are
        things they are entitled to, and an address, a memo and somebody else's name on an invoice
        are not.
        """
        private = (
            f"{SENTINEL.lower()}-bidder@example.invalid",
            f"{SENTINEL.lower()}-member@example.invalid",
            f"{SENTINEL}MEMO",
            f"{SENTINEL} Member",
        )
        leaked = []
        for name, action in sorted(palette_actions.ACTIONS.items()):
            params = self._params_for(action)
            answer = json.dumps(self._run(self.their_bidder_user, name, params), default=str)
            asked = json.dumps(params, default=str)
            for secret in private:
                if secret in answer and secret not in asked:
                    leaked.append(f"{name} leaked “{secret}”: {answer[:300]}")
        self.assertEqual(leaked, [], "a bidder was told about the other bidders")


class NobodyElsesRowsTests(CrossTenantTestCase):
    """Writes: no tool may change a row in a tenant the caller is not in."""

    def _assert_nothing_written(self, user, who, *, may_create_inside=False):
        for name, action in sorted(palette_actions.ACTIONS.items()):
            if action.danger != palette_actions.DANGER_CONFIRM:
                continue
            before = self._snapshot()
            self._run(user, name, self._params_for(action))
            self._assert_nothing_of_theirs_moved(
                before, self._snapshot(), f"{who} calling {name}", may_create_inside=may_create_inside
            )

    def test_a_member_of_nothing_changes_nothing(self):
        self._assert_nothing_written(self.outsider, "an outsider")

    def test_an_administrator_of_another_club_changes_nothing(self):
        self._assert_nothing_written(self.our_owner, "another club's admin")

    def test_a_bidder_in_the_auction_changes_nothing_that_is_not_theirs(self):
        """Being let in is not being given the keys: they may add their own lot and nothing else."""
        self._assert_nothing_written(self.their_bidder_user, "a bidder", may_create_inside=True)


class NothingCrashesInsteadOfRefusingTests(CrossTenantTestCase):
    """A refusal has to be a refusal, not a traceback that happens to leave the data alone.

    ``run_action`` turns any unhandled exception into "Something went wrong ... reference", which
    looks like a refusal from the outside and is a bug on the inside -- and an audit that only
    asserted "nothing changed" would pass on every one of them.
    """

    def test_no_action_blows_up_on_somebody_elses_tenant(self):
        crashed = []
        personas = (
            (self.outsider, "outsider"),
            (self.our_owner, "other admin"),
            (self.their_bidder_user, "a bidder"),
        )
        for user, who in personas:
            for name, action in sorted(palette_actions.ACTIONS.items()):
                result = self._run(user, name, self._params_for(action))
                if "reference" in str(result.get("error", "")):
                    crashed.append(f"{who} calling {name}: {result['error']}")
        self.assertEqual(crashed, [])


class PrintLabelsByPrimaryKeyTests(CrossTenantTestCase):
    """The one the driver above found, written out so it cannot come back quietly.

    ``print_labels`` took a ``lot_id`` and looked it up by primary key with no check at all, then
    answered "Opening the label for <lot name>". A primary key is a guessable number, so that was
    an enumeration oracle over every lot on the site -- including lots in auctions nobody has
    promoted -- and the link it handed back went to a page that would then turn the caller away.
    ``SingleLotLabelView.dispatch`` was always right; the resolver had simply never been given the
    same rule.
    """

    def _print(self, user):
        return self._run(user, "print_labels", {"lot_id": self.their_lot.pk})

    def test_a_stranger_is_not_told_what_the_lot_is_called(self):
        result = self._print(self.outsider)
        self.assertIn("error", result)
        self.assertNotIn(SENTINEL, json.dumps(result, default=str))

    def test_neither_is_an_administrator_of_another_auction(self):
        self.assertIn("error", self._print(self.our_owner))

    def test_nor_a_bidder_in_the_same_auction_who_does_not_own_it(self):
        """Being in the auction is not owning the lot -- and labels carry the seller's details."""
        self.assertIn("error", self._print(self.their_bidder_user))

    def test_the_seller_still_gets_their_own_label(self):
        self.their_tos.user = self.their_bidder_user
        self.their_tos.save()
        result = self._print(self.their_bidder_user)
        self.assertTrue(result.get("ok"), result)
        self.assertIn(f"/lots/print/{self.their_lot.pk}/", result["url"])

    def test_and_so_does_an_admin_of_that_auction(self):
        result = self._print(self.their_owner)
        self.assertTrue(result.get("ok"), result)

    def test_the_answer_still_says_which_lot_and_links_to_it(self):
        """``url`` is the label page; the lot's own address rides alongside as ``lot_url``."""
        result = self._print(self.their_owner)
        self.assertIn("/lots/print/", result["url"])
        self.assertIn("lot_url", result)
        self.assertNotEqual(result["lot_url"], result["url"])


class AuctionSetupBelongsToTheAuctionTests(CrossTenantTestCase):
    """The setup tools, named one at a time.

    The driver above proves nothing of tenant A's moved. These prove the gate is what stopped it,
    which is a different claim: an action that refuses everybody because it can't find the auction
    passes the driver and is still broken. Each of these checks that the auction's own admin CAN do
    the thing, so a refusal is about who is asking rather than about the call being malformed.
    """

    def _their(self, action, **params):
        """Tenant B's legitimate administrator, aimed at tenant A's auction."""
        return self._run(self.our_owner, action, {"auction": self.their_auction.slug, **params})

    def _theirs_own(self, action, **params):
        """The auction's own admin, so a refusal above is about who asked and not about the call."""
        return self._run(self.their_owner, action, {"auction": self.their_auction.slug, **params})

    def test_a_stranger_cannot_add_a_pickup_location(self):
        before = PickupLocation.objects.filter(auction=self.their_auction).count()
        result = self._their("add_pickup_location", name="Ours", location_coordinates="42.36,-71.06")
        self.assertNotIn("ok", result)
        self.assertEqual(PickupLocation.objects.filter(auction=self.their_auction).count(), before)

    def test_their_own_admin_can_add_a_pickup_location(self):
        result = self._theirs_own("add_pickup_location", name="Village hall", location_coordinates="42.36,-71.06")
        self.assertTrue(result.get("ok"), result)

    def test_a_stranger_cannot_move_their_pickup_location(self):
        result = self._their(
            "update_pickup_location", location=self.their_location.name, setting="address", value="Somewhere else"
        )
        self.assertNotIn("ok", result)
        self.their_location.refresh_from_db()
        self.assertNotEqual(self.their_location.address, "Somewhere else")

    def test_a_stranger_cannot_add_or_remove_their_dropdown_options(self):
        added = self._their("add_dropdown_option", option="Ours")
        self.assertNotIn("ok", added)
        removed = self._their("remove_dropdown_option", option=self.their_dropdown_option.value)
        self.assertNotIn("ok", removed)
        self.assertTrue(AuctionDropdown.objects.filter(pk=self.their_dropdown_option.pk).exists())

    def test_their_own_admin_can_add_a_dropdown_option(self):
        result = self._theirs_own("add_dropdown_option", option="Cichlid")
        self.assertTrue(result.get("ok"), result)

    def test_a_stranger_cannot_change_what_their_labels_print(self):
        was = self.their_auction.label_print_fields
        result = self._their("update_label_fields", field="Lot name", value=False)
        self.assertNotIn("ok", result)
        self.their_auction.refresh_from_db()
        self.assertEqual(self.their_auction.label_print_fields, was)

    def test_their_own_admin_can_change_what_the_labels_print(self):
        result = self._theirs_own("update_label_fields", field="Quantity", value=True)
        self.assertTrue(result.get("ok"), result)

    def test_a_stranger_cannot_cancel_their_request_for_help(self):
        result = self._their("cancel_volunteer_request", job=self.their_volunteer_job.description)
        self.assertNotIn("ok", result)
        self.their_volunteer_job.refresh_from_db()
        self.assertFalse(self.their_volunteer_job.canceled)

    def test_a_stranger_cannot_ask_their_room_for_help(self):
        before = VolunteerJob.objects.filter(auction=self.their_auction).count()
        result = self._their("request_volunteers", description="carry our boxes")
        self.assertNotIn("ok", result)
        self.assertEqual(VolunteerJob.objects.filter(auction=self.their_auction).count(), before)


class ClubSetupBelongsToTheClubTests(CrossTenantTestCase):
    """The widened club settings tool, and the survey beside it."""

    def test_a_stranger_cannot_read_what_their_club_is_using(self):
        result = self._run(self.our_owner, "club_setup", {"club": self.their_club.slug})
        self.assertNotIn("found", result)

    def test_a_stranger_cannot_change_their_breeder_award_settings(self):
        was = self.their_club.points_per_lot
        result = self._run(
            self.our_owner,
            "update_club_setting",
            {"club": self.their_club.slug, "setting": "points_per_lot", "value": 99},
        )
        self.assertNotIn("ok", result)
        self.their_club.refresh_from_db()
        self.assertEqual(self.their_club.points_per_lot, was)

    def test_a_stranger_cannot_change_their_membership_settings(self):
        result = self._run(
            self.our_owner,
            "update_club_setting",
            {"club": self.their_club.slug, "setting": "membership_annual_fee", "value": 999},
        )
        self.assertNotIn("ok", result)

    def test_a_stranger_cannot_rewrite_their_welcome_email(self):
        result = self._run(
            self.our_owner,
            "update_club_setting",
            {"club": self.their_club.slug, "setting": "welcome_opening", "value": "Zorblatt"},
        )
        self.assertNotIn("ok", result)

    def test_the_breeder_award_permission_is_not_the_edit_club_permission(self):
        """The four settings pages have four different gates, and this is the one that differs most.

        Somebody who may edit the club's details still may not change how its points are awarded,
        because that is a different job held by a different officer.
        """
        officer = ClubMember.objects.create(
            club=self.their_club,
            user=self.our_owner,
            name="An officer",
            email="officer@example.invalid",
            permission_edit_club=True,
        )
        allowed = self._run(
            self.our_owner,
            "update_club_setting",
            {"club": self.their_club.slug, "setting": "allow_joining", "value": True},
        )
        self.assertTrue(allowed.get("ok"), allowed)
        refused = self._run(
            self.our_owner,
            "update_club_setting",
            {"club": self.their_club.slug, "setting": "points_per_lot", "value": 99},
        )
        self.assertIn("error", refused)
        officer.permission_manage_bap = True
        officer.save()
        now_allowed = self._run(
            self.our_owner,
            "update_club_setting",
            {"club": self.their_club.slug, "setting": "points_per_lot", "value": 99},
        )
        self.assertTrue(now_allowed.get("ok"), now_allowed)
