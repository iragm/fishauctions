"""Shared test fixture and helpers every other test module builds on: StandardTestCase, WritableMediaRoot, patch_views."""

import datetime
import functools
import importlib.util
import tempfile
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from django.contrib.auth.hashers import get_hashers
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionTOS,
    Invoice,
    InvoiceAdjustment,
    Lot,
    PickupLocation,
)
from auctions.test_support import isolated_cache

# Probe for daphne (test-only dep) without importing channels.testing, which would error if daphne is absent.
CHANNELS_TESTING_AVAILABLE = importlib.util.find_spec("daphne") is not None


class patch_views:
    """Patch a name in every auctions.views submodule that binds it."""

    def __init__(self, target, **kwargs):
        self.target = target
        self.kwargs = kwargs

    def _modules(self):
        import importlib
        import pkgutil

        import auctions.views as package

        root = self.target.split(".", 1)[0]
        found = [
            module.__name__
            for module in (
                importlib.import_module(f"auctions.views.{info.name}")
                for info in pkgutil.iter_modules(package.__path__)
            )
            if root in vars(module)
        ]
        if not found:
            msg = f"nothing in auctions.views binds {root!r} -- has it been renamed?"
            raise AssertionError(msg)
        return found

    def __enter__(self):
        self._mock = MagicMock(**self.kwargs)
        self._stack = ExitStack()
        for module in self._modules():
            self._stack.enter_context(patch(f"{module}.{self.target}", self._mock))
        return self._mock

    def __exit__(self, *exc_info):
        return self._stack.__exit__(*exc_info)

    def __call__(self, func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with patch_views(self.target, **self.kwargs) as mock:
                return func(*args, mock, **kwargs)

        return wrapper


class WritableMediaRoot:
    """Redirect MEDIA_ROOT to a throwaway directory for tests that save files."""

    @classmethod
    def setUpClass(cls):
        # Enabled before super() so setUpTestData's file saves land in the throwaway dir too.
        cls._media_tmp = tempfile.TemporaryDirectory()
        cls._media_override = override_settings(MEDIA_ROOT=cls._media_tmp.name)
        cls._media_override.enable()
        try:
            super().setUpClass()
        except Exception:
            cls._media_override.disable()
            cls._media_tmp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._media_override.disable()
        cls._media_tmp.cleanup()
        super().tearDownClass()


def give_contact_info(user, *, phone="555-0100"):
    """Fill in contact info to pass lot/auction gates; pass phone="" to skip lot gate only."""
    from auctions.models import UserData

    user.first_name = user.first_name or "Test"
    user.last_name = user.last_name or "User"
    user.save()
    # Via user.userdata, not the manager, so the cached instance isn't left stale.
    userdata = getattr(user, "userdata", None) or UserData.objects.create(user=user)
    userdata.address = userdata.address or "123 Test St"
    userdata.phone_number = phone
    userdata.save()
    return userdata


class CsvImportTestMixin:
    """Helper for driving the two-phase CSV importer in tests."""

    def run_csv_import(self, url, csv_file, *, decisions=None, file_field="csv_file", follow=True):
        """Upload, then confirm using preview token. `decisions` maps row index to "merge" or "create"."""
        upload = self.client.post(url, {file_field: csv_file})
        location = upload.get("HX-Redirect") or upload.get("Location") or ""
        if "preview=" not in location:
            return upload
        token = location.split("preview=")[1].split("&")[0]
        data = {"_confirm": token}
        for index, decision in (decisions or {}).items():
            data[f"decision_{index}"] = decision
        return self.client.post(url, data, follow=follow)


@isolated_cache("standard")
class StandardTestCase(CsvImportTestMixin, TestCase):
    """Base class with shared fixture: two auctions, users, TOS, lots, invoices."""

    def endAuction(self):
        self.online_auction.date_end = timezone.now() - datetime.timedelta(days=2)
        self.online_auction.save()

    def setUp(self):
        super().setUp()
        cache.clear()

    @classmethod
    def setUpTestData(cls):
        """Built once per class via Django deepcopy+rollback, not per test."""
        super().setUpTestData()
        cache.clear()
        time = timezone.now() - datetime.timedelta(days=2)
        timeStart = timezone.now() - datetime.timedelta(days=3)
        the_future = timezone.now() + datetime.timedelta(days=3)
        cls.admin_user = User.objects.create_user(
            username="admin_user", password="testpassword", email="test@example.com"
        )
        cls.user = User.objects.create_user(username="my_lot", password="testpassword", email="test@example.com")
        cls.user_with_no_lots = User.objects.create_user(
            username="no_lots", password="testpassword", email="asdf@example.com"
        )
        cls.user_who_does_not_join = User.objects.create_user(
            username="no_joins", password="testpassword", email="zxcgv@example.com"
        )
        # promote_this_auction is spelled out: default is False, and guess_category excludes unpromoted lots.
        cls.online_auction = Auction.objects.create(
            created_by=cls.user,
            title="This auction is online",
            is_online=True,
            date_end=time,
            date_start=timeStart,
            winning_bid_percent_to_club=25,
            lot_entry_fee=2,
            unsold_lot_fee=10,
            tax=25,
            promote_this_auction=True,
        )
        cls.in_person_auction = Auction.objects.create(
            created_by=cls.user,
            title="This auction is in-person",
            is_online=False,
            date_end=time,
            date_start=timeStart,
            winning_bid_percent_to_club=25,
            lot_entry_fee=2,
            unsold_lot_fee=10,
            tax=25,
            buy_now="allow",
            reserve_price="allow",
            use_seller_dash_lot_numbering=True,
            promote_this_auction=True,
        )
        cls.location = PickupLocation.objects.create(
            name="location", auction=cls.online_auction, pickup_time=the_future
        )
        cls.in_person_location = PickupLocation.objects.create(
            name="location", auction=cls.in_person_auction, pickup_time=the_future
        )
        # Every participant gets an explicit bidder number, since AuctionTOS.save() auto-assigns randint(1, 999)
        # and could collide with a number a test hard-codes.
        cls.in_person_buyer = AuctionTOS.objects.create(
            user=cls.user_with_no_lots,
            auction=cls.in_person_auction,
            pickup_location=cls.in_person_location,
            bidder_number="555",
        )
        cls.userB = User.objects.create_user(username="no_tos", password="testpassword")
        cls.admin_online_tos = AuctionTOS.objects.create(
            user=cls.admin_user,
            auction=cls.online_auction,
            pickup_location=cls.location,
            is_admin=True,
            bidder_number="501",
        )
        cls.admin_in_person_tos = AuctionTOS.objects.create(
            user=cls.admin_user,
            auction=cls.in_person_auction,
            pickup_location=cls.in_person_location,
            is_admin=True,
            bidder_number="502",
        )
        cls.online_tos = AuctionTOS.objects.create(
            user=cls.user, auction=cls.online_auction, pickup_location=cls.location, bidder_number="503"
        )
        cls.in_person_tos = AuctionTOS.objects.create(
            user=cls.user, auction=cls.in_person_auction, pickup_location=cls.location, bidder_number="504"
        )
        cls.tosB = AuctionTOS.objects.create(
            user=cls.userB, auction=cls.online_auction, pickup_location=cls.location, bidder_number="505"
        )
        cls.tosC = AuctionTOS.objects.create(
            user=cls.user_with_no_lots, auction=cls.online_auction, pickup_location=cls.location, bidder_number="506"
        )
        cls.lot = Lot.objects.create(
            lot_name="A test lot",
            auction=cls.online_auction,
            auctiontos_seller=cls.online_tos,
            quantity=1,
            winning_price=10,
            auctiontos_winner=cls.tosB,
            active=False,
        )
        # no permission to save images by default, so this is a no-go
        # png_bytes = base64.b64decode(
        #     b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAH0KzMgAAAABJRU5ErkJggg=="
        # )
        # cls.lot_image = LotImage.objects.create(
        #     lot_number=cls.lot,
        #     image=SimpleUploadedFile("test.png", png_bytes, content_type="image/png"),
        #     is_primary=True,
        # )
        cls.lotB = Lot.objects.create(
            lot_name="B test lot",
            auction=cls.online_auction,
            auctiontos_seller=cls.online_tos,
            quantity=1,
            winning_price=10,
            auctiontos_winner=cls.tosB,
            active=False,
        )
        cls.lotC = Lot.objects.create(
            lot_name="C test lot",
            auction=cls.online_auction,
            auctiontos_seller=cls.online_tos,
            quantity=1,
            winning_price=10,
            auctiontos_winner=cls.tosB,
            active=False,
        )
        cls.unsoldLot = Lot.objects.create(
            lot_name="Unsold lot",
            reserve_price=10,
            auction=cls.online_auction,
            quantity=1,
            auctiontos_seller=cls.online_tos,
            active=False,
        )
        cls.invoice, c = Invoice.objects.get_or_create(auctiontos_user=cls.online_tos)
        cls.invoiceB, c = Invoice.objects.get_or_create(auctiontos_user=cls.tosB)
        cls.adjustment_add = InvoiceAdjustment.objects.create(
            adjustment_type="ADD", amount=10, notes="test", invoice=cls.invoiceB
        )
        cls.adjustment_discount = InvoiceAdjustment.objects.create(
            adjustment_type="DISCOUNT", amount=10, notes="test", invoice=cls.invoiceB
        )
        cls.adjustment_add_percent = InvoiceAdjustment.objects.create(
            adjustment_type="ADD_PERCENT",
            amount=10,
            notes="test",
            invoice=cls.invoiceB,
        )
        cls.adjustment_discount_percent = InvoiceAdjustment.objects.create(
            adjustment_type="DISCOUNT_PERCENT",
            amount=10,
            notes="test",
            invoice=cls.invoiceB,
        )
        cls.in_person_lot = Lot.objects.create(
            lot_name="another test lot",
            auction=cls.in_person_auction,
            auctiontos_seller=cls.admin_in_person_tos,
            quantity=1,
            custom_lot_number="101-1",
        )
        # TODO: more users/userbans, not-yet-started auctions, an ended auction, multiple pickup locations.


class SuiteStaysFastTests(StandardTestCase):
    """The two things that hold the suite at ~5 minutes instead of ~55; both fail silently if undone."""

    def test_passwords_are_hashed_with_the_cheap_hasher(self):
        """fishauctions.test_runner swaps in md5; PBKDF2 costs ~200ms/call, ~17,000 calls a run."""
        self.assertEqual(get_hashers()[0].algorithm, "md5")

    def test_the_shared_fixture_is_built_once_per_class(self):
        """setUpTestData, not setUp: ~2,700 tests inherit these rows."""
        self.assertIn("setUpTestData", StandardTestCase.__dict__)
        # Reading the pk off the class (not the instance) proves it was built once.
        self.assertEqual(type(self).online_auction.pk, self.online_auction.pk)

    def test_the_cache_this_clears_every_test_is_not_the_shared_one(self):
        """Without isolated_cache, setUp's clear is a Redis FLUSHDB felt by every --parallel worker."""
        from django.conf import settings

        self.assertIn("LocMemCache", settings.CACHES["default"]["BACKEND"])


class EveryTestStartsInTheSiteTimezoneTests(SimpleTestCase):
    """Four forms call timezone.activate() in __init__ and never deactivate; the runner resets it,
    or which test ran before this one in the same --parallel worker would decide the result."""

    def test_a_leaked_timezone_does_not_reach_the_next_test(self):
        """SimpleTestCase: on a TestCase, _pre_setup would open a second atomic block nothing exits."""
        from django.conf import settings

        timezone.activate("Pacific/Kiritimati")
        self._pre_setup()
        self.assertEqual(timezone.get_current_timezone_name(), settings.TIME_ZONE)
