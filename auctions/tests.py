"""The shared test fixture, and the helpers every other test module builds on.

This file used to be 34,089 lines -- the suite's fixture *and* 249 test classes. The classes are now
in the ``test_*`` modules beside it and what is left is the part everything imports:
:class:`StandardTestCase` (its 26-row fixture built in ``setUpTestData``, so it costs once per class
rather than once per test), :class:`WritableMediaRoot` for anything that saves a file, and
:func:`patch_views`, which exists because the views package binds an imported name once per module.

``setUp`` stays for one job: emptying the cache. Class-level fixtures mean every test in a class
shares primary keys, so anything cached under one would otherwise carry into the next.
"""

import datetime
import functools
import importlib.util
import tempfile
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from django.contrib.auth.hashers import get_hashers
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
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

# channels.testing's package __init__ eagerly imports ChannelsLiveServerTestCase, which
# pulls in daphne -- a test-only dependency (see requirements-test.in) absent from the
# production image. Probe for daphne WITHOUT importing channels.testing (importing it
# would raise there): the WebSocketConsumerTests below then skip, rather than error,
# when run in the daphne-free image, e.g. `docker exec django manage.py test`. CI runs
# the full suite in the `test` image, which has daphne, so coverage is unchanged.
CHANNELS_TESTING_AVAILABLE = importlib.util.find_spec("daphne") is not None


class patch_views:
    """Patch a name everywhere the ``auctions.views`` package has bound it.

    ``views.py`` used to be one module, so ``patch("auctions.views.send_user_notification")`` reached
    every view that called it. The package binds such a name **once per module that imports it**, and
    a view calls the binding in its own module -- so patching the package attribute now patches
    something nothing calls, and the test passes while asserting nothing. That failure is silent,
    which is why this exists rather than a note telling people to name the right module.

    It patches every module in the package that has the name bound, with one shared mock, so a test
    does not have to know which module the view it is exercising happens to live in today. Works as
    a context manager or a decorator, like ``mock.patch``::

        with patch_views("send_user_notification") as notify:
            ...
        @patch_views("requests.post")
        def test_something(self, mock_post):
            ...

    A dotted target (``"requests.post"``) is matched on its first segment and patched in full.
    """

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
    """Write uploads to a throwaway directory instead of the container's mediafiles volume.

    MEDIA_ROOT is a bind mount of the repo's `mediafiles/`, which is gitignored and untracked.
    A clean checkout doesn't have it, so Docker creates the mount source root-owned and the
    `app` user the tests run as can't write into it -- that is CI, every time, while a dev
    machine that has ever run the site has a writable one and passes. Mix this in to any test
    class that saves a real file (an upload, a generated easy-thumbnail, a photo from an
    importer) so it never depends on the state of that directory.
    """

    @classmethod
    def setUpClass(cls):
        # Enabled before super(), because super() is what runs setUpTestData -- a class fixture
        # that saves a file has to land in the throwaway directory too. tearDownClass never runs
        # if setUpClass raises, hence the unwinding here.
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


class CsvImportTestMixin:
    """Shared helper for driving the two-phase CSV importer in tests (used by StandardTestCase and any
    plain TestCase that exercises an importer)."""

    def run_csv_import(self, url, csv_file, *, decisions=None, file_field="csv_file", follow=True):
        """Upload the file, read the preview token from the redirect, then POST the confirm so the rows
        are actually written. Returns the confirm response.

        `decisions` maps a planned-action row index -> "merge" (default) or "create", controlling what
        happens to possible-duplicate rows. If the upload did not produce a preview (e.g. permission
        denied, or no recognizable columns), the upload response is returned unchanged.
        """
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
    """This is a base class that sets up some common stuff so other tests can be run without needing to write a lot of boilplate code
    Give this class along with your view/model/etc., to ChatGPT and it can write the test subclass
    In general, make sure that AuctionTOS.is_admin=True users can do what they need, users without an AuctionTOS are blocked, no data leaks to non-admins and non-logged in users

    Tests can be run with with docker exec -it django python3 manage.py test

    Tests are also run automatically on commit by github actions
    """

    def endAuction(self):
        self.online_auction.date_end = timezone.now() - datetime.timedelta(days=2)
        self.online_auction.save()

    def setUp(self):
        """Empty the cache between tests.

        The fixture below is built once per class now, so every test in a class sees the same
        primary keys -- and anything cached under one of them (a per-user model budget, a
        throttle, a recommendation) would carry from one test into the next, which is how
        test_rate_limit_stops_asking came to fail on a limit two of its siblings had already
        spent. `isolated_cache` above makes that a local-memory cache belonging to this process
        rather than the Redis every --parallel worker shares, so clearing it is a scoped delete
        and not a FLUSHDB other workers feel; see auctions/test_support.py. One LOCATION covers
        every subclass that does not name its own, which is why this clears in setUpTestData as
        well -- otherwise the last test of one class would be seeding the next class's fixture.
        """
        super().setUp()
        cache.clear()

    @classmethod
    def setUpTestData(cls):
        """Built once per class, not once per test.

        This fixture is 26 rows deep and ~2,700 test methods inherit it, so building it in setUp
        cost 79ms of every one of them -- about 200 seconds a run, the largest single cost left in
        the suite. setUpTestData creates it once inside the class-level atomic block instead;
        Django hands each test a deepcopy of every attribute and rolls the database back between
        tests, so a test that edits or saves a fixture row still cannot reach the next one.
        Subclasses keep their own setUp for per-test work -- ``super().setUp()`` now resolves to
        TestCase's no-op, which is what they want.
        """
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
        # ``promote_this_auction`` is spelled out on both fixture auctions because the model's
        # default is False (an auction is not on the public list until somebody puts it there),
        # and several things this fixture is used to test are scoped to promoted auctions --
        # notably ``models.guess_category``, which excludes lots in unpromoted auctions. Leaving
        # it to the default made those tests depend on a column default rather than on a fixture.
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
        # Every fixture participant gets an explicit bidder number. AuctionTOS.save() auto-assigns
        # with randint(1, 999) when the number is left blank, so a fixture row that generates its own
        # can land on a number a test hard-codes ("88", "70", ...) and fail that test roughly one run
        # in 500: the auction already holds that number under a different name. These are kept out of
        # the range tests pick their own numbers from.
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
        # TODO: stuff to add here:
        # a few more users and a userban or two
        # an online auction that hasn't started yet
        # an in-person auction that hasn't started yet
        # an online auction that's ended
        # an online auction with multiple pickup locations


class SuiteStaysFastTests(StandardTestCase):
    """The two things that hold the suite at ~5 minutes instead of ~55.

    Both fail silently: undo either and every test still passes, ten times slower, which nobody
    notices until CI is slow again and somebody has to find out why.
    """

    def test_passwords_are_hashed_with_the_cheap_hasher(self):
        """fishauctions.test_runner. PBKDF2 here costs ~200ms a call, ~17,000 times a run."""
        self.assertEqual(get_hashers()[0].algorithm, "md5")

    def test_the_shared_fixture_is_built_once_per_class(self):
        """setUpTestData, not setUp: ~2,700 tests inherit these rows."""
        self.assertIn("setUpTestData", StandardTestCase.__dict__)
        # An attribute assigned in setUp would exist on the instance only; reading it off the
        # class is what proves it was built once, for the class.
        self.assertEqual(type(self).online_auction.pk, self.online_auction.pk)

    def test_the_cache_this_clears_every_test_is_not_the_shared_one(self):
        """The clear in setUp and the isolated_cache decorator are one thing, not two.

        Without the decorator that clear is a Redis FLUSHDB, run ~2,700 times, emptying the
        cache out from under every other --parallel worker mid-assertion.
        """
        from django.conf import settings

        self.assertIn("LocMemCache", settings.CACHES["default"]["BACKEND"])
