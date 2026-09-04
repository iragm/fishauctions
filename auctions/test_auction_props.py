"""``Auction`` computed properties -- the many questions the rest of the site asks an auction."""

import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionTOS,
    Invoice,
    InvoicePayment,
    Lot,
    PickupLocation,
    add_price_info,
)
from auctions.tests import StandardTestCase


class AuctionPropertyTests(StandardTestCase):
    """Test Auction model properties"""

    def test_auction_type(self):
        """Test the auction_type property returns correct values"""
        # Online auction with one location
        assert self.online_auction.auction_type == "online_one_location"
        assert self.online_auction.auction_type_as_str == "online auction with in-person pickup"

        # In-person auction with one location
        assert self.in_person_auction.auction_type == "inperson_one_location"
        assert self.in_person_auction.auction_type_as_str == "in-person auction"

        # Create a new auction with multiple locations for this test
        multi_location_auction = Auction.objects.create(
            created_by=self.user,
            title="Multi-location auction",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        PickupLocation.objects.create(
            name="first location",
            auction=multi_location_auction,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )
        PickupLocation.objects.create(
            name="second location",
            auction=multi_location_auction,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )
        assert multi_location_auction.auction_type == "online_multi_location"
        assert (
            multi_location_auction.auction_type_as_str == "online auction with in-person pickup at multiple locations"
        )

    def test_pretty_much_over_online_uses_last_pickup_time(self):
        """An online auction is pretty_much_over 24h after its latest pickup time."""
        auction = Auction.objects.create(
            created_by=self.user,
            title="pmo online",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=10),
            date_end=timezone.now() - datetime.timedelta(days=8),
        )
        # Pickup still in the recent past (12h ago) -> not yet pretty_much_over.
        loc = PickupLocation.objects.create(
            name="loc", auction=auction, pickup_time=timezone.now() - datetime.timedelta(hours=12)
        )
        self.assertFalse(auction.pretty_much_over)
        # Move the pickup to 25h ago -> now pretty_much_over.
        loc.pickup_time = timezone.now() - datetime.timedelta(hours=25)
        loc.save()
        self.assertTrue(auction.pretty_much_over)

    def test_pretty_much_over_online_uses_latest_of_multiple_pickups(self):
        """The latest pickup (incl. second_pickup_time) across locations drives wind-down."""
        auction = Auction.objects.create(
            created_by=self.user,
            title="pmo multi",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=10),
            date_end=timezone.now() - datetime.timedelta(days=8),
        )
        PickupLocation.objects.create(
            name="early", auction=auction, pickup_time=timezone.now() - datetime.timedelta(hours=48)
        )
        # A second pickup only 2h ago keeps the auction from being pretty_much_over.
        PickupLocation.objects.create(
            name="late",
            auction=auction,
            pickup_time=timezone.now() - datetime.timedelta(hours=50),
            second_pickup_time=timezone.now() - datetime.timedelta(hours=2),
        )
        self.assertFalse(auction.pretty_much_over)

    def test_pretty_much_over_online_falls_back_to_date_end(self):
        """With no pickup locations, an online auction winds down 24h after date_end."""
        auction = Auction.objects.create(
            created_by=self.user,
            title="pmo no pickup",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=10),
            date_end=timezone.now() - datetime.timedelta(hours=25),
        )
        self.assertTrue(auction.pretty_much_over)
        auction.date_end = timezone.now() - datetime.timedelta(hours=12)
        auction.save()
        self.assertFalse(auction.pretty_much_over)

    def test_pretty_much_over_in_person_uses_date_start(self):
        """In-person auctions are pretty_much_over 24h after date_start, once the online bidding
        and lot submission windows (which default to date_start) are moved along with it."""
        auction = Auction.objects.create(
            created_by=self.user,
            title="pmo in person",
            is_online=False,
            date_start=timezone.now() - datetime.timedelta(hours=12),
        )
        self.assertFalse(auction.pretty_much_over)
        auction.date_start = timezone.now() - datetime.timedelta(hours=25)
        auction.date_online_bidding_ends = auction.date_start
        auction.lot_submission_end_date = auction.date_start
        auction.save()
        self.assertTrue(auction.pretty_much_over)

    def test_pretty_much_over_in_person_waits_for_online_bidding_end(self):
        """An in-person auction with online bidding enabled isn't pretty_much_over until the online
        bidding window closes, even if the in-person event (date_start) was 24h+ ago."""
        auction = Auction.objects.create(
            created_by=self.user,
            title="pmo in person online bidding",
            is_online=False,
            online_bidding="allow",
            date_start=timezone.now() - datetime.timedelta(hours=25),
            date_online_bidding_ends=timezone.now() + datetime.timedelta(hours=1),
        )
        self.assertFalse(auction.pretty_much_over)

    def test_pretty_much_over_in_person_waits_for_lot_submission_end(self):
        """An in-person auction isn't pretty_much_over until lot submission closes, even if the
        event (date_start) was 24h+ ago."""
        auction = Auction.objects.create(
            created_by=self.user,
            title="pmo in person lot submission",
            is_online=False,
            date_start=timezone.now() - datetime.timedelta(hours=25),
            lot_submission_end_date=timezone.now() + datetime.timedelta(hours=1),
        )
        self.assertFalse(auction.pretty_much_over)

    def test_auction_timing_properties(self):
        """Test auction start/end related properties"""
        # Create an auction that has started and is in progress
        in_progress_auction = Auction.objects.create(
            created_by=self.user,
            title="In progress auction",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        assert in_progress_auction.started is True
        assert in_progress_auction.in_progress is True
        assert in_progress_auction.closed is False
        assert in_progress_auction.ending_soon is False

        # Create an auction that hasn't started yet
        future_auction = Auction.objects.create(
            created_by=self.user,
            title="Future auction",
            is_online=True,
            date_start=timezone.now() + datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=2),
        )
        assert future_auction.started is False
        assert future_auction.in_progress is False
        assert future_auction.closed is False

        # Test ending_soon
        ending_soon_auction = Auction.objects.create(
            created_by=self.user,
            title="Ending soon auction",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(minutes=60),
        )
        assert ending_soon_auction.ending_soon is True

    def test_allow_mailing_lots(self):
        """Test the allow_mailing_lots property"""
        # Create a separate auction for this test to avoid test isolation issues
        mail_auction = Auction.objects.create(
            created_by=self.user,
            title="Mail test auction",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        # Initially should be False
        assert mail_auction.allow_mailing_lots is False

        # Add a mail pickup location
        PickupLocation.objects.create(
            name="Mail pickup",
            auction=mail_auction,
            pickup_by_mail=True,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )
        assert mail_auction.allow_mailing_lots is True

    def test_auction_type_as_str_with_mail_only(self):
        """Test that auction_type_as_str returns correct string for mail-only auctions"""
        # Create an online auction with only mail pickup
        mail_only_auction = Auction.objects.create(
            created_by=self.user,
            title="Mail only auction",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        # Add a mail pickup location (pickup_by_mail=True)
        PickupLocation.objects.create(
            name="Mail pickup",
            auction=mail_only_auction,
            pickup_by_mail=True,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )
        # Should return "online auction with lots delivered by mail"
        assert mail_only_auction.auction_type == "online_no_location"
        assert mail_only_auction.auction_type_as_str == "online auction with lots delivered by mail"

        # Create an online auction with no locations at all
        no_location_auction = Auction.objects.create(
            created_by=self.user,
            title="No location auction",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        # Should return "online auction with no specified pickup location"
        assert no_location_auction.auction_type == "online_no_location"
        assert no_location_auction.auction_type_as_str == "online auction with no specified pickup location"

    def test_location_with_location_qs_excludes_zero_coordinates(self):
        """Test that location_with_location_qs excludes locations with 0,0 coordinates"""
        # Create an auction with a location that has 0,0 coordinates
        zero_coord_auction = Auction.objects.create(
            created_by=self.user,
            title="Zero coord auction",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        # Add a location with 0,0 coordinates (should be excluded from distance)
        PickupLocation.objects.create(
            name="Zero location",
            auction=zero_coord_auction,
            latitude=0,
            longitude=0,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )
        # Verify this is treated as having no location with coordinates
        assert zero_coord_auction.location_with_location_qs.count() == 0

        # Add a real location
        PickupLocation.objects.create(
            name="Real location",
            auction=zero_coord_auction,
            latitude=42.0,
            longitude=-72.0,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )
        # Now should have one location with coordinates
        assert zero_coord_auction.location_with_location_qs.count() == 1

    def test_all_auctions_distance_excludes_zero_and_mail_locations(self):
        """Test that AllAuctions view distance calculation excludes 0,0 and mail locations"""
        from django.test import RequestFactory

        from auctions.views import AllAuctions

        # Create auction with only 0,0 location
        zero_auction = Auction.objects.create(
            created_by=self.user,
            title="Zero location auction",
            is_online=True,
            promote_this_auction=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=7),
        )
        PickupLocation.objects.create(
            name="Zero location",
            auction=zero_auction,
            latitude=0,
            longitude=0,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )

        # Create auction with only mail location
        mail_auction = Auction.objects.create(
            created_by=self.user,
            title="Mail only auction",
            is_online=True,
            promote_this_auction=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=7),
        )
        PickupLocation.objects.create(
            name="Mail me my lots",
            auction=mail_auction,
            pickup_by_mail=True,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )

        # Create auction with real location
        real_auction = Auction.objects.create(
            created_by=self.user,
            title="Real location auction",
            is_online=True,
            promote_this_auction=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=7),
        )
        PickupLocation.objects.create(
            name="Real location",
            auction=real_auction,
            latitude=44.0,
            longitude=-72.5,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )

        # Set up request with user location
        factory = RequestFactory()
        request = factory.get("/auctions/")
        request.user = self.user
        # Set user location
        self.user.userdata.latitude = 43.0
        self.user.userdata.longitude = -71.5
        self.user.userdata.save()

        # Get queryset from view
        view = AllAuctions()
        view.request = request
        qs = view.get_queryset()

        # Find our test auctions in the queryset
        zero_result = qs.filter(pk=zero_auction.pk).first()
        mail_result = qs.filter(pk=mail_auction.pk).first()
        real_result = qs.filter(pk=real_auction.pk).first()

        # Zero location auction should have no distance (None or NULL)
        assert zero_result is not None
        assert zero_result.distance is None or zero_result.distance == 0

        # Mail location auction should have no distance (None or NULL)
        assert mail_result is not None
        assert mail_result.distance is None or mail_result.distance == 0

        # Real location auction should have a calculated distance
        assert real_result is not None
        assert real_result.distance is not None
        assert real_result.distance > 0

    def test_nearby_filter_applied_when_user_has_location(self):
        """Nearby filter should hide far auctions and show nearby/joined auctions"""
        from django.test import RequestFactory

        from auctions.views import AllAuctions

        # Create a nearby online auction (within default 100 miles)
        nearby_auction = Auction.objects.create(
            created_by=self.user,
            title="Nearby online auction",
            is_online=True,
            promote_this_auction=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=7),
        )
        PickupLocation.objects.create(
            name="Nearby location",
            auction=nearby_auction,
            latitude=43.1,
            longitude=-71.6,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )

        # Create a far-away online auction (beyond default 100 miles)
        far_auction = Auction.objects.create(
            created_by=self.user,
            title="Far online auction",
            is_online=True,
            promote_this_auction=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=7),
        )
        PickupLocation.objects.create(
            name="Far location",
            auction=far_auction,
            latitude=34.0,
            longitude=-118.0,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )

        # Use user_who_does_not_join so they have no existing TOS records
        test_user = self.user_who_does_not_join
        test_user.userdata.latitude = 43.0
        test_user.userdata.longitude = -71.5
        test_user.userdata.email_me_about_new_auctions_distance = 100
        test_user.userdata.save()

        factory = RequestFactory()
        request = factory.get("/auctions/")
        request.user = test_user

        view = AllAuctions()
        view.request = request
        qs = view.get_queryset()

        # The view should be filtering by nearby
        assert view.nearby_filter_active is True
        # Nearby auction should appear
        assert qs.filter(pk=nearby_auction.pk).exists()
        # Far auction should be excluded
        assert not qs.filter(pk=far_auction.pk).exists()

    def test_nearby_filter_disabled_with_param(self):
        """When nearby=false is in GET params, the nearby filter should be disabled"""
        from django.test import RequestFactory

        from auctions.views import AllAuctions

        # Create a far-away online auction
        far_auction = Auction.objects.create(
            created_by=self.user,
            title="Far online auction no filter",
            is_online=True,
            promote_this_auction=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=7),
        )
        PickupLocation.objects.create(
            name="Far location",
            auction=far_auction,
            latitude=34.0,
            longitude=-118.0,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )

        test_user = self.user_who_does_not_join
        test_user.userdata.latitude = 43.0
        test_user.userdata.longitude = -71.5
        test_user.userdata.email_me_about_new_auctions_distance = 100
        test_user.userdata.save()

        factory = RequestFactory()
        request = factory.get("/auctions/", {"nearby": "false"})
        request.user = test_user

        view = AllAuctions()
        view.request = request
        qs = view.get_queryset()

        # The nearby filter should be inactive
        assert view.nearby_filter_active is False
        # Far auction should now appear (no nearby filter applied)
        assert qs.filter(pk=far_auction.pk).exists()

    def test_nearby_filter_shows_joined_auctions(self):
        """Auctions the user has joined should always appear even if they are far away"""
        from django.test import RequestFactory

        from auctions.views import AllAuctions

        # Create a far-away auction that the user has joined
        far_joined_auction = Auction.objects.create(
            created_by=self.user,
            title="Far joined auction",
            is_online=True,
            promote_this_auction=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=7),
        )
        far_location = PickupLocation.objects.create(
            name="Far location",
            auction=far_joined_auction,
            latitude=34.0,
            longitude=-118.0,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )
        AuctionTOS.objects.create(
            user=self.user_who_does_not_join,
            auction=far_joined_auction,
            pickup_location=far_location,
        )

        test_user = self.user_who_does_not_join
        test_user.userdata.latitude = 43.0
        test_user.userdata.longitude = -71.5
        test_user.userdata.email_me_about_new_auctions_distance = 100
        test_user.userdata.save()

        factory = RequestFactory()
        request = factory.get("/auctions/")
        request.user = test_user

        view = AllAuctions()
        view.request = request
        qs = view.get_queryset()

        # Nearby filter should be active
        assert view.nearby_filter_active is True
        # The far auction the user joined should still appear
        assert qs.filter(pk=far_joined_auction.pk).exists()

    def test_nearby_filter_not_applied_without_location(self):
        """Nearby filter should not apply when user has no location set"""
        from django.test import RequestFactory

        from auctions.views import AllAuctions

        far_auction = Auction.objects.create(
            created_by=self.user,
            title="Far auction no user location",
            is_online=True,
            promote_this_auction=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=7),
        )
        PickupLocation.objects.create(
            name="Far location",
            auction=far_auction,
            latitude=34.0,
            longitude=-118.0,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )

        # User has no location set
        test_user = self.user_who_does_not_join
        test_user.userdata.latitude = 0
        test_user.userdata.longitude = 0
        test_user.userdata.save()

        factory = RequestFactory()
        request = factory.get("/auctions/")
        request.user = test_user

        view = AllAuctions()
        view.request = request
        qs = view.get_queryset()

        # Nearby filter should be inactive (no location)
        assert view.nearby_filter_active is False
        # Far auction should appear (no filter)
        assert qs.filter(pk=far_auction.pk).exists()

    def test_nearby_filter_shows_created_auctions(self):
        """Auctions the user created should always appear even if they are far away"""
        from django.test import RequestFactory

        from auctions.views import AllAuctions

        # Create a far-away auction owned by the test user
        far_created_auction = Auction.objects.create(
            created_by=self.user_who_does_not_join,
            title="Far auction created by user",
            is_online=True,
            promote_this_auction=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=7),
        )
        PickupLocation.objects.create(
            name="Far location",
            auction=far_created_auction,
            latitude=34.0,
            longitude=-118.0,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )

        test_user = self.user_who_does_not_join
        test_user.userdata.latitude = 43.0
        test_user.userdata.longitude = -71.5
        test_user.userdata.email_me_about_new_auctions_distance = 100
        test_user.userdata.show_nearby_auctions = True
        test_user.userdata.save()

        factory = RequestFactory()
        request = factory.get("/auctions/")
        request.user = test_user

        view = AllAuctions()
        view.request = request
        qs = view.get_queryset()

        # Nearby filter should be active
        assert view.nearby_filter_active is True
        # The far auction created by the user should still appear
        assert qs.filter(pk=far_created_auction.pk).exists()

    def test_nearby_filter_disabled_by_preference(self):
        """When show_nearby_auctions preference is False, the nearby filter should not apply"""
        from django.test import RequestFactory

        from auctions.views import AllAuctions

        far_auction = Auction.objects.create(
            created_by=self.user,
            title="Far auction preference off",
            is_online=True,
            promote_this_auction=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=7),
        )
        PickupLocation.objects.create(
            name="Far location",
            auction=far_auction,
            latitude=34.0,
            longitude=-118.0,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )

        test_user = self.user_who_does_not_join
        test_user.userdata.latitude = 43.0
        test_user.userdata.longitude = -71.5
        test_user.userdata.email_me_about_new_auctions_distance = 100
        test_user.userdata.show_nearby_auctions = False
        test_user.userdata.save()

        factory = RequestFactory()
        request = factory.get("/auctions/")
        request.user = test_user

        view = AllAuctions()
        view.request = request
        qs = view.get_queryset()

        # Nearby filter should be inactive (preference disabled)
        assert view.nearby_filter_active is False
        # Far auction should now appear
        assert qs.filter(pk=far_auction.pk).exists()

    def test_nearby_filter_in_person_distance(self):
        """In-person auctions should use email_me_about_new_in_person_auctions_distance, not the online distance"""
        from django.test import RequestFactory

        from auctions.views import AllAuctions

        # Create a nearby in-person auction (within 100 miles)
        nearby_in_person = Auction.objects.create(
            created_by=self.user,
            title="Nearby in-person auction",
            is_online=False,
            promote_this_auction=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=7),
        )
        PickupLocation.objects.create(
            name="Nearby in-person location",
            auction=nearby_in_person,
            latitude=43.1,
            longitude=-71.6,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )

        # Create a far in-person auction (beyond 100 miles)
        far_in_person = Auction.objects.create(
            created_by=self.user,
            title="Far in-person auction",
            is_online=False,
            promote_this_auction=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=7),
        )
        PickupLocation.objects.create(
            name="Far in-person location",
            auction=far_in_person,
            latitude=34.0,
            longitude=-118.0,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )

        test_user = self.user_who_does_not_join
        test_user.userdata.latitude = 43.0
        test_user.userdata.longitude = -71.5
        # Online distance is very small so online auctions wouldn't qualify via distance
        test_user.userdata.email_me_about_new_auctions_distance = 10
        # In-person distance is large enough to include the nearby in-person auction
        test_user.userdata.email_me_about_new_in_person_auctions_distance = 100
        test_user.userdata.save()

        factory = RequestFactory()
        request = factory.get("/auctions/")
        request.user = test_user

        view = AllAuctions()
        view.request = request
        qs = view.get_queryset()

        assert view.nearby_filter_active is True
        # Nearby in-person auction should be visible
        assert qs.filter(pk=nearby_in_person.pk).exists()
        # Far in-person auction should be excluded
        assert not qs.filter(pk=far_in_person.pk).exists()

    def test_permission_check(self):
        """Test the permission_check method"""
        # Creator has permission
        assert self.online_auction.permission_check(self.user) is True

        # Admin has permission
        assert self.online_auction.permission_check(self.admin_user) is True

        # Regular user without admin TOS does not have permission
        assert self.online_auction.permission_check(self.user_with_no_lots) is False

        # Non-authenticated user does not have permission (though this requires a User object)
        assert self.online_auction.permission_check(self.userB) is False

    def test_dynamic_end(self):
        """Test the dynamic_end property for online auctions"""
        # For non-sealed-bid auctions, dynamic end should be 60 minutes after date_end
        expected_dynamic_end = self.online_auction.date_end + datetime.timedelta(minutes=60)
        assert self.online_auction.dynamic_end == expected_dynamic_end

        # For sealed-bid auctions, dynamic end should equal date_end
        sealed_auction = Auction.objects.create(
            created_by=self.user,
            title="Sealed bid auction",
            is_online=True,
            sealed_bid=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        assert sealed_auction.dynamic_end == sealed_auction.date_end

    def test_minutes_to_end(self):
        """Test the minutes_to_end property"""
        # Future auction should have positive minutes
        future_auction = Auction.objects.create(
            created_by=self.user,
            title="Future minutes test",
            is_online=True,
            date_start=timezone.now() + datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=2),
        )
        assert future_auction.minutes_to_end > 0

        # Past auction should return 0
        assert self.online_auction.minutes_to_end == 0

    def test_number_of_locations(self):
        """Test location counting properties"""
        # Default auction has 1 physical location
        assert self.online_auction.number_of_locations == 1
        assert self.online_auction.all_location_count == 1

        # Add a mail location
        PickupLocation.objects.create(
            name="Mail",
            auction=self.online_auction,
            pickup_by_mail=True,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )
        # Physical count stays same, all_location_count increases
        assert self.online_auction.number_of_locations == 1
        assert self.online_auction.all_location_count == 2

    def test_has_non_logical_times(self):
        """Test that has_non_logical_times property detects illogical auction times"""
        # Create an auction with logical times (ending in :00:00)
        logical_auction = Auction.objects.create(
            created_by=self.user,
            title="Logical time auction",
            is_online=True,
            date_start=timezone.now().replace(hour=14, minute=0, second=0, microsecond=0),
            date_end=timezone.now().replace(hour=18, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1),
        )
        assert logical_auction.has_non_logical_times is False

        # Create an auction with logical times (ending in :30:00)
        logical_auction_30 = Auction.objects.create(
            created_by=self.user,
            title="Logical time auction at 30",
            is_online=True,
            date_start=timezone.now().replace(hour=14, minute=30, second=0, microsecond=0),
            date_end=timezone.now().replace(hour=18, minute=30, second=0, microsecond=0) + datetime.timedelta(days=1),
        )
        assert logical_auction_30.has_non_logical_times is False

        # Create an auction with non-logical start time
        illogical_start = Auction.objects.create(
            created_by=self.user,
            title="Illogical start time auction",
            is_online=True,
            date_start=timezone.now().replace(hour=14, minute=23, second=0, microsecond=0),
            date_end=timezone.now().replace(hour=18, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1),
        )
        assert illogical_start.has_non_logical_times is not False

        # Create an auction with non-logical end time
        illogical_end = Auction.objects.create(
            created_by=self.user,
            title="Illogical end time auction",
            is_online=True,
            date_start=timezone.now().replace(hour=14, minute=0, second=0, microsecond=0),
            date_end=timezone.now().replace(hour=18, minute=15, second=0, microsecond=0) + datetime.timedelta(days=1),
        )
        assert illogical_end.has_non_logical_times is not False

        # Create an auction with seconds not zero
        illogical_seconds = Auction.objects.create(
            created_by=self.user,
            title="Illogical seconds auction",
            is_online=True,
            date_start=timezone.now().replace(hour=14, minute=0, second=30, microsecond=0),
            date_end=timezone.now().replace(hour=18, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1),
        )
        assert illogical_seconds.has_non_logical_times is not False

    def test_buyer_seller_participant_stats(self):
        """Test that number_of_buyers, number_of_sellers, number_of_sellers_who_didnt_buy, and number_of_participants are accurate"""
        # In StandardTestCase setup for online_auction:
        # - online_tos (user) sold lots but never won any -> seller only
        # - tosB (userB) won lots but never sold any -> buyer only
        # - admin_online_tos and tosC neither bought nor sold
        assert self.online_auction.number_of_buyers == 1
        assert self.online_auction.number_of_sellers == 1
        assert self.online_auction.number_of_sellers_who_didnt_buy == 1
        assert self.online_auction.number_of_participants == 2

        # Add a user who both sells and buys
        user_both = User.objects.create_user(username="both_buyer_seller", password="testpassword")
        tos_both = AuctionTOS.objects.create(user=user_both, auction=self.online_auction, pickup_location=self.location)
        Lot.objects.create(
            lot_name="Lot sold by both user",
            auction=self.online_auction,
            auctiontos_seller=tos_both,
            quantity=1,
            winning_price=5,
            auctiontos_winner=self.tosB,
            active=False,
        )
        Lot.objects.create(
            lot_name="Lot won by both user",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            winning_price=5,
            auctiontos_winner=tos_both,
            active=False,
        )
        # tos_both both sold and won a lot, so sellers_who_didnt_buy should stay at 1
        assert self.online_auction.number_of_buyers == 2
        assert self.online_auction.number_of_sellers == 2
        assert self.online_auction.number_of_sellers_who_didnt_buy == 1
        # participants = buyers (tosB, tos_both) + sellers who didn't buy (online_tos) = 3
        assert self.online_auction.number_of_participants == 3


class LotPropertyTests(StandardTestCase):
    """Test Lot model properties"""

    def test_lot_ended_property(self):
        """Test that lot.ended works correctly"""
        # Create a lot that has ended
        ended_lot = Lot.objects.create(
            lot_name="Ended lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            # inherited from auction, this value won't be used
            date_end=timezone.now() - datetime.timedelta(days=1),
        )
        assert ended_lot.ended is True

        # Create a lot that is still active
        active_lot = Lot.objects.create(
            lot_name="Active lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
        )
        # simulated dynamic ending
        active_lot.date_end = timezone.now() + datetime.timedelta(days=1)
        active_lot.save()
        assert active_lot.ended is False

    def test_lot_with_auction_inherits_end_date(self):
        """Test that lots in an auction inherit the auction's end date"""
        # Create a lot with a future end date but in an ended auction
        lot = Lot.objects.create(
            lot_name="Inherit end date lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            date_end=timezone.now() + datetime.timedelta(days=30),
        )
        # The auction has ended, so the lot should be ended too
        assert lot.ended is True


class LotInvoicePropertyTests(StandardTestCase):
    """Regression tests for Lot.winner_invoice / Lot.sellers_invoice.

    These properties previously queried Invoice with a non-existent `user` field
    (`Q(user=..., auction=...)`). The resulting FieldError was swallowed by a blanket
    `except Exception: return None`, so any lot with `winner` or `user` (seller) set --
    which is the normal case for online sales and every user-submitted lot -- silently
    resolved to None. That broke the Square auto-refund path and the invoice links in the
    lot table/detail templates. The correct traversal is `auctiontos_user__user=...`.
    """

    def test_winner_invoice_resolves_when_winner_user_and_auctiontos_both_set(self):
        """Realistic online-sale case: both winner (User) and auctiontos_winner are set.

        The old code raised a swallowed FieldError as soon as `winner` was truthy (even though
        the auctiontos branch was valid), so this returned None. It must now return the invoice.
        """
        self.lot.winner = self.userB
        self.lot.save()
        assert self.lot.winner is not None
        assert self.lot.auctiontos_winner == self.tosB
        assert self.lot.winner_invoice == self.invoiceB

    def test_winner_invoice_resolves_from_winner_user_without_auctiontos(self):
        """Only the legacy winner (User FK) is set: resolve via auctiontos_user__user for this auction."""
        invoice = Invoice.objects.create(auctiontos_user=self.tosB, auction=self.online_auction)
        lot = Lot.objects.create(
            lot_name="winner-user only lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            winning_price=Decimal("10.00"),
            winner=self.userB,
            active=False,
        )
        assert lot.auctiontos_winner is None
        assert lot.winner_invoice == invoice

    def test_sellers_invoice_resolves_when_seller_user_and_auctiontos_both_set(self):
        """Realistic case: both user (seller User FK) and auctiontos_seller are set."""
        self.lot.user = self.user
        self.lot.save()
        assert self.lot.user is not None
        assert self.lot.auctiontos_seller == self.online_tos
        assert self.lot.sellers_invoice == self.invoice

    def test_sellers_invoice_resolves_from_seller_user_without_auctiontos(self):
        """Only the legacy user (seller User FK) is set: resolve via auctiontos_user__user for this auction."""
        invoice = Invoice.objects.create(auctiontos_user=self.online_tos, auction=self.online_auction)
        lot = Lot.objects.create(
            lot_name="seller-user only lot",
            auction=self.online_auction,
            user=self.user,
            quantity=1,
            active=False,
        )
        assert lot.auctiontos_seller is None
        assert lot.sellers_invoice == invoice

    def test_winner_invoice_none_when_winner_has_no_invoice(self):
        """A winner who never joined the auction has no invoice: return None, not an error."""
        lot = Lot.objects.create(
            lot_name="no-invoice winner lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            winning_price=Decimal("10.00"),
            winner=self.user_who_does_not_join,
            active=False,
        )
        assert lot.winner_invoice is None

    def test_invoice_properties_none_when_no_winner_or_seller(self):
        """A lot with neither winner nor seller set returns None from both properties."""
        bare_lot = Lot.objects.create(
            lot_name="bare lot",
            auction=self.online_auction,
            quantity=1,
            active=False,
        )
        assert bare_lot.winner_invoice is None
        assert bare_lot.sellers_invoice is None

    def test_square_refund_possible_true_for_winner_user_lot_with_square_payment(self):
        """With the invoice now resolvable, a Square payment large enough makes a refund possible."""
        invoice = Invoice.objects.create(auctiontos_user=self.tosB, auction=self.online_auction)
        InvoicePayment.objects.create(
            invoice=invoice,
            payment_method="square",
            amount=Decimal("20.00"),
            amount_available_to_refund=Decimal("20.00"),
        )
        lot = Lot.objects.create(
            lot_name="square refundable lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            winning_price=Decimal("10.00"),
            winner=self.userB,
            active=False,
        )
        assert lot.winner_invoice == invoice
        assert lot.square_refund_possible is True

    def test_square_refund_possible_false_without_square_payment(self):
        """The invoice resolves, but with no Square payment a refund is not possible."""
        Invoice.objects.create(auctiontos_user=self.tosB, auction=self.online_auction)
        lot = Lot.objects.create(
            lot_name="no square payment lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            winning_price=Decimal("10.00"),
            winner=self.userB,
            active=False,
        )
        assert lot.winner_invoice is not None
        assert lot.square_refund_possible is False


class SellerInvoiceRemovedLotTests(StandardTestCase):
    """Regression tests for Item 12: a removed (banned) lot must still appear on its seller's
    invoice, but it must not be charged.

    ``Lot.banned`` is labeled "Removed" and its help text documents "Removed lots are not
    charged in invoices." The original ``payout`` property honored that with an
    ``if self.banned: return payout`` short-circuit (a $0 payout). When the cut math was
    refactored into the ``add_price_info`` queryset annotation, that guard was dropped, so a
    removed lot was silently charged its normal seller cut / unsold-lot fee -- money the
    invoice's own line items displayed but that the club never intended to collect, and (on
    the buyer side) money billed for a lot that was pulled. These tests lock in:

      * removed lots stay visible in ``sold_lots_queryset`` (they are not filtered out),
      * their ``your_cut`` / ``club_cut`` are $0, so the displayed seller line items reconcile
        with ``total_sold`` / ``net`` / ``calculated_total``,
      * a buyer is never billed for a removed lot they "won".
    """

    def _isolated_auction(self):
        """Build a clean online auction with a seller, a buyer, and both invoices, so the
        assertions aren't muddied by the lots StandardTestCase attaches to ``self.invoice``."""
        now = timezone.now()
        auction = Auction.objects.create(
            created_by=self.user,
            title="removed-lot auction",
            is_online=True,
            date_end=now - datetime.timedelta(days=2),
            date_start=now - datetime.timedelta(days=3),
            winning_bid_percent_to_club=25,
            lot_entry_fee=2,
            unsold_lot_fee=10,
            tax=0,
        )
        location = PickupLocation.objects.create(
            name="loc", auction=auction, pickup_time=now + datetime.timedelta(days=3)
        )
        seller_tos = AuctionTOS.objects.create(user=self.user_with_no_lots, auction=auction, pickup_location=location)
        buyer_tos = AuctionTOS.objects.create(user=self.userB, auction=auction, pickup_location=location)
        seller_invoice, _ = Invoice.objects.get_or_create(auctiontos_user=seller_tos, auction=auction)
        buyer_invoice, _ = Invoice.objects.get_or_create(auctiontos_user=buyer_tos, auction=auction)
        return auction, seller_tos, buyer_tos, seller_invoice, buyer_invoice

    def _sold_lot(self, auction, seller_tos, buyer_tos, name, price=100):
        return Lot.objects.create(
            lot_name=name,
            auction=auction,
            auctiontos_seller=seller_tos,
            auctiontos_winner=buyer_tos,
            winning_price=Decimal(price),
            quantity=1,
            active=False,
        )

    def _unsold_lot(self, auction, seller_tos, name):
        return Lot.objects.create(
            lot_name=name,
            auction=auction,
            auctiontos_seller=seller_tos,
            quantity=1,
            active=False,
        )

    def test_removed_lot_still_listed_on_seller_invoice(self):
        """A removed lot (sold or unsold) is not filtered out of the seller's lot listing."""
        auction, seller_tos, buyer_tos, seller_invoice, _ = self._isolated_auction()
        kept = self._sold_lot(auction, seller_tos, buyer_tos, "kept sold lot", price=100)
        removed_sold = self._sold_lot(auction, seller_tos, buyer_tos, "sold then removed", price=100)
        removed_sold.remove(True, self.user)
        removed_unsold = self._unsold_lot(auction, seller_tos, "unsold then removed")
        removed_unsold.remove(True, self.user)

        listed_pks = {lot.pk for lot in seller_invoice.sold_lots_queryset}
        assert kept.pk in listed_pks
        assert removed_sold.pk in listed_pks, "removed sold lot must still show on the seller invoice"
        assert removed_unsold.pk in listed_pks, "removed unsold lot must still show on the seller invoice"

    def test_removed_lots_are_not_charged(self):
        """Removed lots contribute $0 to both the seller's cut and the club's cut."""
        auction, seller_tos, buyer_tos, seller_invoice, _ = self._isolated_auction()
        removed_sold = self._sold_lot(auction, seller_tos, buyer_tos, "sold then removed", price=100)
        removed_sold.remove(True, self.user)
        removed_unsold = self._unsold_lot(auction, seller_tos, "unsold then removed")
        removed_unsold.remove(True, self.user)

        by_pk = {lot.pk: lot for lot in seller_invoice.sold_lots_queryset}
        assert by_pk[removed_sold.pk].your_cut == Decimal(0)
        assert by_pk[removed_sold.pk].club_cut == Decimal(0)
        # Without the fix this would be -unsold_lot_fee (-10), charging the seller for a pulled lot.
        assert by_pk[removed_unsold.pk].your_cut == Decimal(0)
        assert by_pk[removed_unsold.pk].club_cut == Decimal(0)

    def test_seller_invoice_lines_reconcile_with_total(self):
        """The sum of the displayed per-lot cuts equals total_sold / net / calculated_total."""
        auction, seller_tos, buyer_tos, seller_invoice, _ = self._isolated_auction()
        # one lot that is genuinely sold (charged) plus two removed lots (not charged)
        self._sold_lot(auction, seller_tos, buyer_tos, "kept sold lot", price=100)
        removed_sold = self._sold_lot(auction, seller_tos, buyer_tos, "sold then removed", price=100)
        removed_sold.remove(True, self.user)
        removed_unsold = self._unsold_lot(auction, seller_tos, "unsold then removed")
        removed_unsold.remove(True, self.user)

        listed = list(seller_invoice.sold_lots_queryset)
        displayed_seller_total = sum((lot.your_cut for lot in listed), Decimal(0))
        # kept lot: 100 * (100-25)/100 - 2 = 73; removed lots: 0 each
        assert displayed_seller_total == Decimal(73)
        assert seller_invoice.total_sold == Decimal(73)
        assert displayed_seller_total == seller_invoice.total_sold

        seller_invoice.recalculate()
        seller_invoice.refresh_from_db()
        assert seller_invoice.net == Decimal(73)
        assert seller_invoice.calculated_total == Decimal(73)

    def test_buyer_not_charged_for_removed_lot(self):
        """A buyer is never billed for a lot they "won" that was later removed."""
        auction, seller_tos, buyer_tos, _, buyer_invoice = self._isolated_auction()
        kept = self._sold_lot(auction, seller_tos, buyer_tos, "kept sold lot", price=100)
        removed = self._sold_lot(auction, seller_tos, buyer_tos, "won then removed", price=100)
        removed.remove(True, self.user)

        bought_pks = {lot.pk for lot in buyer_invoice.bought_lots_queryset}
        assert kept.pk in bought_pks
        assert removed.pk not in bought_pks, "buyer must not be billed for a removed lot"

        # buyer display (bought_lots_queryset) reconciles with total_bought, and neither counts the removed lot
        displayed_buyer_total = sum((lot.final_price for lot in buyer_invoice.bought_lots_queryset), Decimal(0))
        assert displayed_buyer_total == Decimal(100)
        assert buyer_invoice.total_bought == Decimal(100)

    def test_non_removed_lots_unaffected(self):
        """Guard: the fix must not change what non-removed lots are charged."""
        auction, seller_tos, buyer_tos, seller_invoice, _ = self._isolated_auction()
        sold = self._sold_lot(auction, seller_tos, buyer_tos, "normal sold", price=100)
        unsold = self._unsold_lot(auction, seller_tos, "normal unsold")

        by_pk = {lot.pk: lot for lot in seller_invoice.sold_lots_queryset}
        assert by_pk[sold.pk].your_cut == Decimal(73)
        assert by_pk[sold.pk].club_cut == Decimal(27)
        # a plain unsold lot is still charged the unsold-lot fee -- only *removed* lots are waived
        assert by_pk[unsold.pk].your_cut == Decimal(-10)


class BuyNowSellerCreditTests(StandardTestCase):
    """Regression tests for Item 13: a lot bought via "buy now" must credit the seller (and
    charge the buyer) immediately -- before the endauctions cron runs.

    A completed buy-now sale sets ``winning_price`` + ``buy_now_used`` but deliberately leaves
    ``active=True`` (the sold lot stays visible in the browse view until the cron flips it
    inactive; see the comment in ``bidding.bid_on_lot``). Two bugs kept the money wrong until
    the cron caught up:

      * ``add_price_info``'s ``your_cut`` annotation only produced the real seller cut for lots
        with ``active=False``. A live buy-now lot fell through to ``$0``, so ``club_cut``
        (``winning_price - your_cut``) booked the *entire* sale price to the club and credited
        the seller nothing until ``endauctions`` set ``active=False``.
      * ``bidding.bid_on_lot`` called ``create_update_invoices`` *before* saving the sale
        fields, so both invoices recalculated from stale (unsold) DB state -- even the buyer's
        invoice showed ``$0`` until the next recalculation.

    These tests lock in that the seller cut, club cut, and both invoices are correct the moment
    buy now completes, that a later ``endauctions`` run does not double-apply anything, and that
    normal (non-buy-now) sales are unchanged.
    """

    def _buy_now_auction(self, **kwargs):
        now = timezone.now()
        defaults = {
            "winning_bid_percent_to_club": 25,
            "lot_entry_fee": 2,
            "unsold_lot_fee": 10,
            "tax": 0,
            "buy_now": "allow",
        }
        defaults.update(kwargs)
        auction = Auction.objects.create(
            created_by=self.user,
            title="buy-now auction",
            is_online=True,
            date_end=now + datetime.timedelta(days=2),
            date_start=now - datetime.timedelta(days=1),
            **defaults,
        )
        location = PickupLocation.objects.create(
            name="loc", auction=auction, pickup_time=now + datetime.timedelta(days=3)
        )
        seller_tos = AuctionTOS.objects.create(user=self.user_with_no_lots, auction=auction, pickup_location=location)
        buyer_tos = AuctionTOS.objects.create(user=self.userB, auction=auction, pickup_location=location)
        return auction, location, seller_tos, buyer_tos

    def _biddable_lot(self, auction, seller_tos, buy_now_price=100, **kwargs):
        now = timezone.now()
        lot = Lot.objects.create(
            lot_name="buy-now lot",
            auction=auction,
            auctiontos_seller=seller_tos,
            quantity=1,
            buy_now_price=Decimal(buy_now_price),
            date_end=now + datetime.timedelta(days=2),
            **kwargs,
        )
        # bidding (and therefore buy now) is blocked on very new lots; backdate date_posted so it is allowed
        lot.date_posted = now - datetime.timedelta(hours=1)
        lot.save()
        return lot

    def _seller_invoice(self, auction, seller_tos):
        return Invoice.objects.filter(auctiontos_user=seller_tos, auction=auction).first()

    def _buyer_invoice(self, auction, buyer_tos):
        return Invoice.objects.filter(auctiontos_user=buyer_tos, auction=auction).first()

    def test_seller_credited_immediately_after_buy_now(self):
        """The seller cut / club cut are correct the moment buy now completes, before any cron run."""
        from auctions.bidding import bid_on_lot

        auction, _location, seller_tos, buyer_tos = self._buy_now_auction()
        lot = self._biddable_lot(auction, seller_tos, buy_now_price=100)

        result = bid_on_lot(lot, self.userB, Decimal(100))
        assert result["type"] == "LOT_END_WINNER", result

        lot.refresh_from_db()
        # buy now completed the sale, but deliberately left the lot active until the cron
        assert lot.winning_price == Decimal(100)
        assert lot.buy_now_used is True
        assert lot.auctiontos_winner == buyer_tos
        assert lot.active is True, "buy now must not deactivate the lot (it stays visible until endauctions)"

        # the seller-cut annotation credits the live buy-now lot NOW, not $0-until-the-cron
        priced = add_price_info(Lot.objects.filter(pk=lot.pk)).first()
        assert priced.your_cut == Decimal(73)  # 100 * (100 - 25)/100 - 2 (lot entry fee)
        assert priced.club_cut == Decimal(27)  # 100 - 73

        seller_invoice = self._seller_invoice(auction, seller_tos)
        assert seller_invoice is not None, "buy now must create the seller invoice"
        assert seller_invoice.total_sold == Decimal(73)
        assert seller_invoice.calculated_total == Decimal(73)

    def test_buyer_charged_immediately_after_buy_now(self):
        """The buyer's invoice reflects the purchase the moment buy now completes."""
        from auctions.bidding import bid_on_lot

        auction, _location, seller_tos, buyer_tos = self._buy_now_auction()
        lot = self._biddable_lot(auction, seller_tos, buy_now_price=100)

        bid_on_lot(lot, self.userB, Decimal(100))
        lot.refresh_from_db()

        buyer_invoice = self._buyer_invoice(auction, buyer_tos)
        assert buyer_invoice is not None, "buy now must create the buyer invoice"
        bought_pks = {b.pk for b in buyer_invoice.bought_lots_queryset}
        assert lot.pk in bought_pks, "the bought lot must appear on the buyer invoice immediately"
        assert buyer_invoice.total_bought == Decimal(100)
        # tax=0, so the buyer simply owes the 100 sale price -> net is -100
        assert buyer_invoice.calculated_total == Decimal(-100)

    def test_endauctions_does_not_double_apply_after_buy_now(self):
        """Running the endauctions logic after buy now must not change either invoice total.

        The buy-now lot is still ``active=True`` so ``declare_winners_on_lots`` picks it up,
        sets ``active=False`` and recalculates. Because ``recalculate()`` re-derives the total
        (it is not additive), the numbers are identical before and after -- no double credit."""
        from auctions.bidding import bid_on_lot
        from auctions.management.commands.endauctions import declare_winners_on_lots

        auction, _location, seller_tos, buyer_tos = self._buy_now_auction()
        lot = self._biddable_lot(auction, seller_tos, buy_now_price=100)

        bid_on_lot(lot, self.userB, Decimal(100))

        seller_invoice = self._seller_invoice(auction, seller_tos)
        buyer_invoice = self._buyer_invoice(auction, buyer_tos)
        assert seller_invoice.calculated_total == Decimal(73)
        assert buyer_invoice.calculated_total == Decimal(-100)

        # run the cron logic; the buy-now lot is ended (sold) and still active, so it is processed
        lot.refresh_from_db()
        declare_winners_on_lots([lot])

        lot.refresh_from_db()
        assert lot.active is False, "endauctions should finalize (deactivate) the buy-now lot"
        seller_invoice.refresh_from_db()
        buyer_invoice.refresh_from_db()
        # totals unchanged: the credit was already applied at buy-now time and recalculate is idempotent
        assert seller_invoice.calculated_total == Decimal(73)
        assert buyer_invoice.calculated_total == Decimal(-100)
        # and the seller cut is still correct once the lot is inactive (active=False branch agrees)
        priced = add_price_info(Lot.objects.filter(pk=lot.pk)).first()
        assert priced.your_cut == Decimal(73)
        assert priced.club_cut == Decimal(27)

    def test_normal_auction_ending_lot_still_credits_correctly(self):
        """Guard: a normally-ended (non-buy-now) sold lot -- active=False, buy_now_used=False --
        still credits the seller exactly as before the fix."""
        auction, _location, seller_tos, buyer_tos = self._buy_now_auction()
        lot = Lot.objects.create(
            lot_name="normal sold lot",
            auction=auction,
            auctiontos_seller=seller_tos,
            auctiontos_winner=buyer_tos,
            winning_price=Decimal(100),
            quantity=1,
            active=False,
        )
        priced = add_price_info(Lot.objects.filter(pk=lot.pk)).first()
        assert priced.buy_now_used is False
        assert priced.your_cut == Decimal(73)
        assert priced.club_cut == Decimal(27)

    def test_active_lot_without_buy_now_is_not_credited(self):
        """Guard: the fix must be precise -- a still-active lot that hasn't sold (no winning_price,
        not buy_now_used) is credited $0. Only *completed* buy-now sales bypass the active=False gate."""
        auction, _location, seller_tos, _buyer_tos = self._buy_now_auction()
        lot = self._biddable_lot(auction, seller_tos, buy_now_price=100)  # no bid placed: active, unsold

        priced = add_price_info(Lot.objects.filter(pk=lot.pk)).first()
        assert priced.your_cut == Decimal(0)
        assert priced.club_cut == Decimal(0)
