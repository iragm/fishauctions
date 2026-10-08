"""The lot pages an auction is actually run from: labels, push, set-winner and the queue."""

from datetime import timedelta
from unittest.mock import MagicMock

from django.urls import resolve, reverse
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionHistory,
    Bid,
    Invoice,
    Lot,
    LotQueueEntry,
    UserData,
    UserLabelPrefs,
    Watch,
)
from auctions.tests import StandardTestCase, patch_views
from auctions.views import UnprintedLotLabelsView


class LotLabelViewTestCase(StandardTestCase):
    """Tests for the LotLabelView"""

    def setUp(self):
        super().setUp()
        self.url = reverse(
            "my_labels_by_username", kwargs={"slug": self.online_auction.slug, "username": self.user.username}
        )

    def assert_message_contains(self, response, expected_text, should_exist=True):
        """Helper method to check if a message contains expected text."""
        messages_list = list(response.wsgi_request._messages)
        found = any(expected_text in str(message) for message in messages_list)
        if should_exist:
            assert found, f"Expected message containing '{expected_text}', got: {[str(m) for m in messages_list]}"
        else:
            assert not found, (
                f"Should not have message containing '{expected_text}', got: {[str(m) for m in messages_list]}"
            )

    def test_user_can_print_own_labels(self):
        """Test that a regular user can print their own labels."""
        self.client.login(username=self.user, password="testpassword")
        self.endAuction()
        response = self.client.get(self.url)
        # messages = list(response.wsgi_request._messages)
        assert response.status_code == 200
        # weasyprint currently requires pydyf==0.8.0.
        assert "attachment;filename=" in response.headers["Content-Disposition"]

    def test_small_labels(self):
        user_label_prefs, created = UserLabelPrefs.objects.get_or_create(user=self.user)
        user_label_prefs.preset = "sm"
        user_label_prefs.save()
        self.client.login(username=self.user, password="testpassword")
        response = self.client.get(self.url)
        assert response.status_code == 200
        assert "attachment;filename=" in response.headers["Content-Disposition"]

    def test_thermal_labels(self):
        """Test that a regular user can print their own labels."""
        user_label_prefs, created = UserLabelPrefs.objects.get_or_create(user=self.user)
        user_label_prefs.preset = "thermal_sm"
        user_label_prefs.save()
        self.client.login(username=self.user, password="testpassword")
        response = self.client.get(self.url)
        assert response.status_code == 200
        assert "attachment;filename=" in response.headers["Content-Disposition"]

    def test_thermal_labels_capped_at_100(self):
        """Test that thermal labels are capped at 100 per PDF."""
        # Create 150 lots for testing the cap
        for i in range(150):
            Lot.objects.create(
                lot_name=f"Test lot {i}",
                auction=self.online_auction,
                auctiontos_seller=self.online_tos,
                quantity=1,
                winning_price=10,
                auctiontos_winner=self.tosB,
                active=False,
            )

        user_label_prefs, created = UserLabelPrefs.objects.get_or_create(user=self.user)
        user_label_prefs.preset = "thermal_sm"
        user_label_prefs.save()
        self.client.login(username=self.user, password="testpassword")
        self.endAuction()
        response = self.client.get(self.url)

        assert response.status_code == 200
        assert "attachment;filename=" in response.headers["Content-Disposition"]

        # Check that a warning message was added about the 100 label cap
        self.assert_message_contains(response, "100 labels")
        self.assert_message_contains(response, "Print unprinted labels")

    def test_thermal_very_sm_labels_capped_at_100(self):
        """Test that thermal_very_sm labels are also capped at 100 per PDF."""
        # Create 120 lots for testing the cap
        for i in range(120):
            Lot.objects.create(
                lot_name=f"Test lot {i}",
                auction=self.online_auction,
                auctiontos_seller=self.online_tos,
                quantity=1,
                winning_price=10,
                auctiontos_winner=self.tosB,
                active=False,
            )

        user_label_prefs, created = UserLabelPrefs.objects.get_or_create(user=self.user)
        user_label_prefs.preset = "thermal_very_sm"
        user_label_prefs.save()
        self.client.login(username=self.user, password="testpassword")
        self.endAuction()
        response = self.client.get(self.url)

        assert response.status_code == 200
        assert "attachment;filename=" in response.headers["Content-Disposition"]

        # Check that a warning message was added about the 100 label cap
        self.assert_message_contains(response, "100 labels")
        self.assert_message_contains(response, "Print unprinted labels")

    def test_non_thermal_labels_not_capped(self):
        """Test that non-thermal labels are NOT capped at 100."""
        # Create 150 lots for testing
        for i in range(150):
            Lot.objects.create(
                lot_name=f"Test lot {i}",
                auction=self.online_auction,
                auctiontos_seller=self.online_tos,
                quantity=1,
                winning_price=10,
                auctiontos_winner=self.tosB,
                active=False,
            )

        user_label_prefs, created = UserLabelPrefs.objects.get_or_create(user=self.user)
        user_label_prefs.preset = "lg"  # Non-thermal preset
        user_label_prefs.save()
        self.client.login(username=self.user, password="testpassword")
        self.endAuction()
        response = self.client.get(self.url)

        assert response.status_code == 200
        assert "attachment;filename=" in response.headers["Content-Disposition"]

        # Check that NO warning message was added
        self.assert_message_contains(response, "100 labels", should_exist=False)

    def test_non_admin_cannot_print_others_labels(self):
        """Test that a non-admin user cannot print labels for other users."""
        self.client.login(username="no_tos", password="testpassword")
        response = self.client.get(self.url)
        assert response.status_code == 302
        messages = list(response.wsgi_request._messages)
        assert str(messages[0]) == "Your account doesn't have permission to view this page."

    def test_cannot_print_if_not_joined_auction(self):
        """Test that a user cannot print labels if they haven't joined the auction."""
        self.client.login(username=self.user_who_does_not_join.username, password="testpassword")
        url = reverse("print_my_labels", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 302
        self.assertRedirects(response, self.online_auction.get_absolute_url())
        messages = list(response.wsgi_request._messages)
        assert (
            str(messages[0])
            == "You haven't joined this auction yet.  You need to join this auction and add lots before you can print labels."
        )

    def test_unprinted_labels_by_bidder_number_reaches_the_unprinted_view(self):
        """The Users tab's "Print only N unprinted labels" link was swallowed by the <path:> route."""
        url = reverse("print_unprinted_labels_by_bidder_number", kwargs={"slug": "x", "bidder_number": "14"})
        match = resolve(url)
        self.assertIs(match.func.view_class, UnprintedLotLabelsView)
        self.assertEqual(match.kwargs["bidder_number"], "14")

    def test_no_printable_lots(self):
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        response = self.client.get(self.url)
        assert response.status_code == 302

    def test_get_seller_email_font_size_for_supported_presets(self):
        from auctions.views import LotLabelView

        short_sm_email_size = LotLabelView.get_seller_email_font_size("short@example.com", "sm")
        long_sm_email_size = LotLabelView.get_seller_email_font_size("admin13423523452@example.com", "sm")
        short_email_size = LotLabelView.get_seller_email_font_size("short@example.com", "thermal_sm")
        long_thermal_email_size = LotLabelView.get_seller_email_font_size("seller1234567890@example.com", "thermal_sm")
        thermal_very_sm_size = LotLabelView.get_seller_email_font_size(
            "seller1234567890@example.com", "thermal_very_sm"
        )
        long_lg_email_size = LotLabelView.get_seller_email_font_size("admin13423523452@example.com", "lg")
        custom_size = LotLabelView.get_seller_email_font_size("really.long.seller.email.address@example.com", "custom")

        self.assertIsNone(short_sm_email_size)
        self.assertIsNotNone(long_sm_email_size)
        self.assertRegex(long_sm_email_size, r"^\d+\.\d{2}em$")
        self.assertIsNone(short_email_size)
        self.assertIsNotNone(long_thermal_email_size)
        self.assertRegex(long_thermal_email_size, r"^\d+\.\d{2}em$")
        self.assertIsNotNone(thermal_very_sm_size)
        self.assertRegex(thermal_very_sm_size, r"^\d+\.\d{2}em$")
        self.assertIsNotNone(long_lg_email_size)
        self.assertRegex(long_lg_email_size, r"^\d+\.\d{2}em$")
        self.assertLess(float(long_sm_email_size[:-2]), 1)
        self.assertLess(float(long_thermal_email_size[:-2]), 1)
        self.assertLess(float(thermal_very_sm_size[:-2]), float(long_thermal_email_size[:-2]))
        self.assertLess(float(long_lg_email_size[:-2]), 1)
        self.assertIsNone(custom_size)

    def test_get_lot_number_font_size_for_supported_presets(self):
        from auctions.views import LotLabelView

        seven_digit_sm_size = LotLabelView.get_lot_number_font_size("123-456", "sm")
        short_sm_size = LotLabelView.get_lot_number_font_size("123456", "sm")
        six_digit_thermal_size = LotLabelView.get_lot_number_font_size("123456", "thermal_sm")
        seven_digit_thermal_size = LotLabelView.get_lot_number_font_size("1234567", "thermal_sm")
        short_thermal_size = LotLabelView.get_lot_number_font_size("12345", "thermal_sm")
        long_lg_size = LotLabelView.get_lot_number_font_size("123-456", "lg")
        short_lg_size = LotLabelView.get_lot_number_font_size("123456", "lg")
        custom_size = LotLabelView.get_lot_number_font_size("123456789", "custom")

        self.assertIsNotNone(seven_digit_sm_size)
        self.assertRegex(seven_digit_sm_size, r"^\d+\.\d{2}em$")
        self.assertIsNone(short_sm_size)
        self.assertIsNotNone(six_digit_thermal_size)
        self.assertRegex(six_digit_thermal_size, r"^\d+\.\d{2}em$")
        self.assertIsNotNone(seven_digit_thermal_size)
        self.assertRegex(seven_digit_thermal_size, r"^\d+\.\d{2}em$")
        self.assertIsNotNone(long_lg_size)
        self.assertRegex(long_lg_size, r"^\d+\.\d{2}em$")
        self.assertIsNone(short_lg_size)
        self.assertLess(float(seven_digit_thermal_size[:-2]), float(six_digit_thermal_size[:-2]))
        self.assertIsNone(short_thermal_size)
        self.assertIsNone(custom_size)

    def test_seller_email_scales_sooner_for_medium_length(self):
        """Moderately long emails are scaled now, between the old and new thresholds."""
        from auctions.views import LotLabelView

        # 20 characters: above the new sm threshold of 18.
        medium_sm_size = LotLabelView.get_seller_email_font_size("john.doe@example.com", "sm")
        # 23 characters: above the new lg threshold of 20.
        medium_lg_size = LotLabelView.get_seller_email_font_size("user@longertesthost.com", "lg")
        # 13 characters: below every threshold.
        very_short_sm_size = LotLabelView.get_seller_email_font_size("user@test.com", "sm")

        self.assertIsNotNone(medium_sm_size)
        self.assertRegex(medium_sm_size, r"^\d+\.\d{2}em$")
        self.assertLess(float(medium_sm_size[:-2]), 1)
        self.assertIsNotNone(medium_lg_size)
        self.assertLess(float(medium_lg_size[:-2]), 1)
        self.assertIsNone(very_short_sm_size)

    def test_bulk_print_pdf_with_default_label_fields(self):
        """An admin can print labels for all users with the default, custom-field-heavy config."""
        self.client.login(username="admin_user", password="testpassword")
        url = reverse("auction_printing_pdf", kwargs={"slug": self.in_person_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200
        assert "attachment;filename=" in response.headers["Content-Disposition"]

    def test_bulk_print_form_uses_fetch(self):
        """The bulk print form uses fetch(), so downloading the PDF doesn't navigate."""
        self.client.login(username="admin_user", password="testpassword")
        url = reverse("auction_printing", kwargs={"slug": self.in_person_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200
        self.assertContains(response, "fetch(")
        self.assertContains(response, "application/pdf")


class UpdateLotPushNotificationsViewTestCase(StandardTestCase):
    def get_url(self):
        return reverse("enable_notifications")

    def test_anonymous_user(self):
        response = self.client.get(self.get_url())
        assert response.status_code == 401
        response = self.client.post(self.get_url())
        assert response.status_code == 401

    def test_logged_in_user(self):
        self.client.login(username=self.user_who_does_not_join.username, password="testpassword")
        response = self.client.get(self.get_url())
        assert response.status_code == 405
        response = self.client.post(self.get_url())
        assert response.status_code == 200
        assert response.json()["result"] == "success"
        userdata = UserData.objects.get(user=self.user_who_does_not_join)
        assert userdata.push_notifications_when_lots_sell is True


class LotPushTestNotificationViewTestCase(StandardTestCase):
    def get_url(self):
        return reverse("lot_push_test", kwargs={"pk": self.in_person_lot.pk})

    def _setup_watcher_with_push(self):
        from webpush.models import PushInformation, SubscriptionInfo

        watcher_userdata = UserData.objects.get(user=self.user_with_no_lots)
        watcher_userdata.push_notifications_when_lots_sell = True
        watcher_userdata.save()
        Watch.objects.create(lot_number=self.in_person_lot, user=self.user_with_no_lots)
        sub = SubscriptionInfo.objects.create(
            browser="Chrome",
            endpoint="https://fcm.googleapis.com/push/example_token",
            auth="auth_secret",
            p256dh="p256dh_key",
        )
        return PushInformation.objects.create(user=self.user_with_no_lots, subscription=sub)

    def test_test_button_visible_for_watched_user_with_push_info(self):
        self._setup_watcher_with_push()
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        response = self.client.get(reverse("lot_by_pk", kwargs={"pk": self.in_person_lot.pk}))
        assert response.status_code == 200
        self.assertContains(response, 'id="test-notification"')
        self.assertNotContains(response, 'if (Notification.permission !== "granted")')

    def test_test_button_hidden_without_push_info(self):
        Watch.objects.create(lot_number=self.in_person_lot, user=self.user_with_no_lots)
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        response = self.client.get(reverse("lot_by_pk", kwargs={"pk": self.in_person_lot.pk}))
        assert response.status_code == 200
        self.assertNotContains(response, 'id="test-notification"')
        self.assertNotContains(response, 'if (Notification.permission !== "granted")')
        self.assertContains(response, "$('#subscribe_success').addClass(\"d-none\")")
        self.assertContains(response, "Get a notification on this device when bidding starts on this lot")

    def test_watch_notification_message_still_shows_without_push_info(self):
        watcher_userdata = UserData.objects.get(user=self.user_with_no_lots)
        watcher_userdata.push_notifications_when_lots_sell = True
        watcher_userdata.save()
        Watch.objects.create(lot_number=self.in_person_lot, user=self.user_with_no_lots)
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        response = self.client.get(reverse("lot_by_pk", kwargs={"pk": self.in_person_lot.pk}))
        assert response.status_code == 200
        self.assertContains(response, "You'll get a notification when bidding starts on this lot")
        # "How it works" is the help, not a pop-up of its own.
        self.assertContains(response, f"/help/in-person-auctions/?auction={self.in_person_auction.slug}#coming-up")
        self.assertNotContains(response, "notification-help")
        self.assertNotContains(response, 'id="test-notification"')

    def test_anonymous_user_does_not_see_test_notification_controls(self):
        response = self.client.get(reverse("lot_by_pk", kwargs={"pk": self.in_person_lot.pk}))
        assert response.status_code == 200
        self.assertNotContains(response, 'id="test-notification"')
        self.assertNotContains(response, "You'll get a notification when bidding starts on this lot")

    def test_watched_user_with_push_can_send_test_notification(self):
        self._setup_watcher_with_push()
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        with patch_views("send_user_notification") as mock_notify:
            response = self.client.post(self.get_url())
        assert response.status_code == 200
        mock_notify.assert_called_once()
        assert mock_notify.call_args.kwargs["user"] == self.user_with_no_lots
        payload = mock_notify.call_args.kwargs["payload"]
        assert payload["head"] == f"{self.in_person_lot.lot_name} test notification"
        assert payload["body"] == f"Lot {self.in_person_lot.lot_number_display} test notification for this watched lot."
        assert payload["url"] == f"https://{self.in_person_lot.full_lot_link}"

    def test_test_notification_requires_watch(self):
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        with patch_views("send_user_notification") as mock_notify:
            response = self.client.post(self.get_url())
        assert response.status_code == 403
        mock_notify.assert_not_called()

    def test_watched_user_without_push_subscription_gets_400(self):
        Watch.objects.create(lot_number=self.in_person_lot, user=self.user_with_no_lots)
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        with patch_views("send_user_notification") as mock_notify:
            response = self.client.post(self.get_url())
        assert response.status_code == 400
        mock_notify.assert_not_called()

    def test_the_help_page_test_needs_no_lot(self):
        self._setup_watcher_with_push()
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        with patch_views("send_user_notification") as mock_notify:
            response = self.client.post(reverse("push_test"))
        assert response.status_code == 200
        assert mock_notify.call_args.kwargs["payload"]["url"].endswith("/help/in-person-auctions/#coming-up")

    def test_the_help_page_test_without_push_gets_400(self):
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        with patch_views("send_user_notification") as mock_notify:
            response = self.client.post(reverse("push_test"))
        assert response.status_code == 400
        mock_notify.assert_not_called()


class ViewLotSimpleTestCase(StandardTestCase):
    """ViewLotSimple, the htmx_lot endpoint auction admins use to project lot images."""

    def get_url(self):
        return reverse(
            "htmx_lot",
            kwargs={"slug": self.in_person_auction.slug, "custom_lot_number": self.in_person_lot.custom_lot_number},
        )

    def _setup_watcher_with_push(self):
        """Give user_with_no_lots a watch on in_person_lot and a push subscription."""
        from webpush.models import PushInformation, SubscriptionInfo

        watcher_userdata = UserData.objects.get(user=self.user_with_no_lots)
        watcher_userdata.push_notifications_when_lots_sell = True
        watcher_userdata.save()
        Watch.objects.create(lot_number=self.in_person_lot, user=self.user_with_no_lots)
        sub = SubscriptionInfo.objects.create(
            browser="Chrome",
            endpoint="https://fcm.googleapis.com/push/example_token",
            auth="auth_secret",
            p256dh="p256dh_key",
        )
        return PushInformation.objects.create(user=self.user_with_no_lots, subscription=sub)

    def test_anonymous_user(self):
        """Anonymous users are denied access to htmx lot view for auctioned lots"""
        response = self.client.get(self.get_url())
        self.assertEqual(response.status_code, 403)

    def test_non_admin_user(self):
        """Non-admin users are denied access to htmx lot view for auctioned lots"""
        self.client.login(username=self.user_who_does_not_join.username, password="testpassword")
        response = self.client.get(self.get_url())
        self.assertEqual(response.status_code, 403)

    def test_admin_no_watchers(self):
        """An admin can view a lot, and no push is sent when nobody is watching."""
        self.client.login(username=self.admin_user.username, password="testpassword")
        with patch_views("send_user_notification") as mock_notify:
            response = self.client.get(self.get_url())
        self.assertEqual(response.status_code, 200)
        mock_notify.assert_not_called()

    def test_admin_push_notification_success(self):
        """An admin viewing an unsold lot pushes to a watching user."""
        self._setup_watcher_with_push()
        self.client.login(username=self.admin_user.username, password="testpassword")
        with patch_views("send_user_notification") as mock_notify:
            response = self.client.get(self.get_url())
        self.assertEqual(response.status_code, 200)
        mock_notify.assert_called_once()

    def test_admin_push_failure_deletes_push_info_and_creates_history(self):
        """A failed push deletes the stale PushInformation and records AuctionHistory."""
        import requests
        from webpush.models import PushInformation

        self._setup_watcher_with_push()
        self.client.login(username=self.admin_user.username, password="testpassword")
        with patch_views(
            "send_user_notification",
            side_effect=requests.exceptions.ConnectionError("push endpoint permanently removed"),
        ):
            response = self.client.get(self.get_url())
        self.assertEqual(response.status_code, 200)
        # Deleted, so the endpoint is never retried.
        self.assertFalse(PushInformation.objects.filter(user=self.user_with_no_lots).exists())
        # And the failure is recorded with the expected message.
        history = AuctionHistory.objects.filter(auction=self.in_person_auction, user=None).first()
        self.assertIsNotNone(history)
        self.assertEqual(
            history.action,
            f"push notification error occurred for {self.user_with_no_lots.username}",
        )

    def test_admin_push_timeout_also_cleans_up(self):
        """Other RequestException subclasses, such as Timeout, are handled too."""
        import requests
        from webpush.models import PushInformation

        self._setup_watcher_with_push()
        self.client.login(username=self.admin_user.username, password="testpassword")
        with patch_views(
            "send_user_notification",
            side_effect=requests.exceptions.Timeout("push endpoint timed out"),
        ):
            response = self.client.get(self.get_url())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PushInformation.objects.filter(user=self.user_with_no_lots).exists())

    def test_admin_push_webpush_exception_cleans_up(self):
        """A WebPushException (FCM's 404 for an expired token) is handled too."""
        from pywebpush import WebPushException
        from webpush.models import PushInformation

        self._setup_watcher_with_push()
        self.client.login(username=self.admin_user.username, password="testpassword")
        # FCM uses 404, not 410, for expired tokens.
        mock_response = type("Response", (), {"status_code": 404, "reason": "Not Found", "text": ""})()
        with patch_views(
            "send_user_notification",
            side_effect=WebPushException("Push failed: 404 Not Found", response=mock_response),
        ):
            response = self.client.get(self.get_url())
        self.assertEqual(response.status_code, 200)
        # Stale PushInformation must be cleaned up
        self.assertFalse(PushInformation.objects.filter(user=self.user_with_no_lots).exists())
        # AuctionHistory must be created
        history = AuctionHistory.objects.filter(auction=self.in_person_auction, user=None).first()
        self.assertIsNotNone(history)
        self.assertEqual(
            history.action,
            f"push notification error occurred for {self.user_with_no_lots.username}",
        )

    def test_sold_lot_no_push_notification(self):
        """No push notification sent when the lot is already sold"""
        from webpush.models import PushInformation

        self._setup_watcher_with_push()
        # Mark lot as sold
        self.in_person_lot.auctiontos_winner = self.in_person_buyer
        self.in_person_lot.winning_price = 10
        self.in_person_lot.save()
        self.client.login(username=self.admin_user.username, password="testpassword")
        with patch_views("send_user_notification") as mock_notify:
            response = self.client.get(self.get_url())
        self.assertEqual(response.status_code, 200)
        mock_notify.assert_not_called()
        # PushInformation must be untouched
        self.assertTrue(PushInformation.objects.filter(user=self.user_with_no_lots).exists())

    def test_message_users_disabled_no_push_notification(self):
        """No push is sent when auction.message_users_when_lots_sell is False."""
        from webpush.models import PushInformation

        self.in_person_auction.message_users_when_lots_sell = False
        self.in_person_auction.save()
        self._setup_watcher_with_push()
        self.client.login(username=self.admin_user.username, password="testpassword")
        with patch_views("send_user_notification") as mock_notify:
            response = self.client.get(self.get_url())
        self.assertEqual(response.status_code, 200)
        mock_notify.assert_not_called()
        # PushInformation must be untouched
        self.assertTrue(PushInformation.objects.filter(user=self.user_with_no_lots).exists())


class DynamicSetLotWinnerViewTestCase(StandardTestCase):
    def get_url(self):
        return reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug})

    def test_anonymous_user(self):
        response = self.client.get(self.get_url())
        assert response.status_code == 302  # Redirect to login
        response = self.client.post(self.get_url())
        assert response.status_code == 302  # Redirect to login

    def test_non_admin_user(self):
        self.client.login(username=self.user_who_does_not_join.username, password="testpassword")
        response = self.client.get(self.get_url())
        assert response.status_code == 403
        response = self.client.post(self.get_url())
        assert response.status_code == 403

    def test_admin_user(self):
        self.client.login(username=self.admin_user.username, password="testpassword")
        response = self.client.get(self.get_url())
        assert response.status_code == 200
        response = self.client.post(
            self.get_url(), data={"lot": "101-1", "price": "5", "winner": "555", "action": "validate"}
        )
        data = response.json()
        assert data.get("price") == "valid"
        assert data.get("winner") == "valid"
        assert data.get("lot") == "valid"

        self.in_person_lot.reserve_price = 10
        self.in_person_lot.save()
        response = self.client.post(
            self.get_url(), data={"lot": "101-1", "price": "5", "winner": "556", "action": "validate"}
        )
        data = response.json()
        assert data.get("price") != "valid"
        assert data.get("winner") != "valid"
        assert data.get("lot") == "valid"

        response = self.client.post(self.get_url(), data={"lot": "102-1", "action": "validate"})
        data = response.json()
        assert data.get("lot") != "valid"

        response = self.client.post(
            self.get_url(), data={"lot": "101-1", "price": "10", "winner": "555", "action": "save"}
        )
        data = response.json()
        assert data.get("price") == "valid"
        assert data.get("winner") == "valid"
        assert data.get("lot") == "valid"
        assert data.get("last_sold_lot_number") == "101-1"
        assert data.get("success_message") is not None

        lot = Lot.objects.filter(pk=self.in_person_lot.pk).first()
        assert lot.winning_price == 10
        assert lot.auctiontos_winner is not None

        response = self.client.post(
            self.get_url(), data={"lot": "101-1", "price": "10", "winner": "555", "action": "validate"}
        )
        data = response.json()
        assert data.get("lot") != "valid"

        invoice, created = Invoice.objects.get_or_create(auctiontos_user=self.in_person_lot.auctiontos_seller)
        invoice.status = "UNPAID"
        invoice.save()

        self.in_person_lot.auctiontos_winner = None
        self.in_person_lot.winning_price = None

        response = self.client.post(
            self.get_url(), data={"lot": "101-1", "price": "10", "winner": "555", "action": "save"}
        )
        data = response.json()
        assert data.get("lot") != "valid"
        assert self.in_person_lot.auctiontos_winner is None
        assert self.in_person_lot.winning_price is None

        response = self.client.post(
            self.get_url(), data={"lot": "101-1", "price": "7", "winner": "555", "action": "force_save"}
        )
        data = response.json()
        assert data.get("lot") == "valid"

        lot = Lot.objects.filter(pk=self.in_person_lot.pk).first()
        assert lot.winning_price == 7
        assert lot.auctiontos_winner is not None

        Bid.objects.create(user=self.admin_user, lot_number=self.in_person_lot, amount=100)
        self.in_person_auction.online_bidding = "allow"
        self.in_person_auction.save()
        invoice.status = "OPEN"
        invoice.save()

        lot = Lot.objects.filter(pk=self.in_person_lot.pk).first()
        lot.winning_price = None
        lot.auctiontos_winner = None
        lot.winner = None
        lot.save()

        response = self.client.post(
            self.get_url(), data={"lot": "101-1", "price": "10", "winner": "555", "action": "validate"}
        )
        data = response.json()
        assert data.get("price") != "valid"
        assert data.get("winner") != "valid"

        # A lot created with the same custom_lot_number as in_person_lot.
        new_lot = Lot.objects.create(
            lot_name="dupe",
            auction=self.in_person_auction,
            auctiontos_seller=self.admin_in_person_tos,
            quantity=1,
            custom_lot_number="101-1",
        )
        # Duplicate detection changes the new lot's number, leaving one lot on "101-1".
        new_lot.refresh_from_db()  # Refresh to get the updated custom_lot_number
        lots_with_101_1 = Lot.objects.filter(auction=self.in_person_auction, custom_lot_number="101-1")
        assert lots_with_101_1.count() == 1, (
            f"Duplicate detection should have changed the duplicate lot's number. New lot number: {new_lot.custom_lot_number}"
        )
        # Verify the new lot got a different number
        assert new_lot.custom_lot_number != "101-1", (
            f"New lot should have been assigned a different number, got: {new_lot.custom_lot_number}"
        )

    def test_htmx_lot_preview_sends_push_notification_to_watcher(self):
        from webpush.models import PushInformation, SubscriptionInfo

        watcher_userdata = UserData.objects.get(user=self.user_with_no_lots)
        watcher_userdata.push_notifications_when_lots_sell = True
        watcher_userdata.save()
        Watch.objects.create(lot_number=self.in_person_lot, user=self.user_with_no_lots)
        sub = SubscriptionInfo.objects.create(
            browser="Chrome",
            endpoint="https://fcm.googleapis.com/push/example_token",
            auth="auth_secret",
            p256dh="p256dh_key",
        )
        PushInformation.objects.create(user=self.user_with_no_lots, subscription=sub)

        self.client.login(username=self.admin_user.username, password="testpassword")
        response = self.client.get(self.get_url())
        assert response.status_code == 200

        lot_preview_url = reverse(
            "htmx_lot",
            kwargs={"slug": self.in_person_auction.slug, "custom_lot_number": self.in_person_lot.custom_lot_number},
        )
        with patch_views("send_user_notification") as mock_notify:
            response = self.client.get(lot_preview_url)
        assert response.status_code == 200
        mock_notify.assert_called_once()
        assert mock_notify.call_args.kwargs["user"] == self.user_with_no_lots
        assert mock_notify.call_args.kwargs["ttl"] == 10000
        payload = mock_notify.call_args.kwargs["payload"]
        assert payload["head"] == f"{self.in_person_lot.lot_name} is about to be sold"
        assert payload["body"] == (
            f"Lot {self.in_person_lot.lot_number_display}  Don't miss out, bid now!  "
            "You're getting this notification because you watched this lot."
        )
        assert payload["url"] == f"https://{self.in_person_lot.full_lot_link}"
        assert payload["tag"] == f"lot_sell_notification_{self.in_person_lot.pk}"


class LotQueueViewTestCase(StandardTestCase):
    """The in-person lot queue (LotQueueView, the fullscreen queue and current lot), set winners and watcher pushes."""

    def get_url(self):
        return reverse("auction_lot_queue", kwargs={"slug": self.in_person_auction.slug})

    def fullscreen_url(self):
        return reverse("auction_lot_queue_fullscreen", kwargs={"slug": self.in_person_auction.slug})

    def current_lot_url(self):
        return reverse("auction_lot_queue_current_lot", kwargs={"slug": self.in_person_auction.slug})

    def _make_in_person_lot(self, name):
        return Lot.objects.create(
            lot_name=name,
            auction=self.in_person_auction,
            auctiontos_seller=self.admin_in_person_tos,
            quantity=1,
        )

    def _watch_with_push(self, lot, user):
        from webpush.models import PushInformation, SubscriptionInfo

        ud = UserData.objects.get(user=user)
        ud.push_notifications_when_lots_sell = True
        ud.save()
        Watch.objects.create(lot_number=lot, user=user)
        sub = SubscriptionInfo.objects.create(
            browser="Chrome",
            endpoint=f"https://fcm.googleapis.com/push/{user.pk}_{lot.pk}",
            auth="auth_secret",
            p256dh="p256dh_key",
        )
        return PushInformation.objects.create(user=user, subscription=sub)

    def _login_admin(self):
        self.client.login(username=self.admin_user.username, password="testpassword")

    # --- permissions ---------------------------------------------------------
    def test_anonymous_user_redirected(self):
        response = self.client.get(self.get_url())
        assert response.status_code == 302
        response = self.client.post(self.get_url(), data={"action": "add", "value": "101-1"})
        assert response.status_code == 302

    def test_non_admin_user_denied(self):
        self.client.login(username=self.user_who_does_not_join.username, password="testpassword")
        assert self.client.get(self.get_url()).status_code == 403
        assert self.client.post(self.get_url(), data={"action": "add", "value": "101-1"}).status_code == 403
        assert self.client.get(self.fullscreen_url()).status_code == 403
        assert self.client.get(self.current_lot_url()).status_code == 403

    def test_online_auction_has_no_queue(self):
        """The queue is in-person only; the online auction 404s."""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("auction_lot_queue", kwargs={"slug": self.online_auction.slug})
        assert self.client.get(url).status_code == 404

    def test_admin_can_view_queue_page(self):
        self._login_admin()
        response = self.client.get(self.get_url())
        assert response.status_code == 200
        self.assertContains(response, "Lot queue")
        # Multi-line {# #} comments leak onto the page, so the template uses {% comment %}.
        self.assertNotContains(response, "Reuse the shared barcode pipeline")
        self.assertNotContains(response, "Fullscreen queue: hidden until fullscreen")
        self.assertContains(response, self.current_lot_url())

    # --- adding --------------------------------------------------------------
    def test_add_by_qr_value(self):
        self._login_admin()
        response = self.client.post(self.get_url(), data={"action": "add", "value": self.in_person_lot.qr_code})
        assert response.status_code == 200
        assert LotQueueEntry.objects.filter(auction=self.in_person_auction, lot=self.in_person_lot).exists()

    def test_add_by_partial_qr_value(self):
        self._login_admin()
        response = self.client.post(self.get_url(), data={"action": "add", "value": f"/qr/{self.in_person_lot.pk}/"})
        assert response.status_code == 200
        assert LotQueueEntry.objects.filter(auction=self.in_person_auction, lot=self.in_person_lot).exists()

    def test_add_by_typed_lot_number(self):
        self._login_admin()
        response = self.client.post(self.get_url(), data={"action": "add", "value": "101-1"})
        assert response.status_code == 200
        assert LotQueueEntry.objects.filter(auction=self.in_person_auction, lot=self.in_person_lot).exists()

    def test_add_by_scanner_lot_pk_returns_json(self):
        self._login_admin()
        response = self.client.post(self.get_url(), data={"lot_pk": self.in_person_lot.pk})
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert LotQueueEntry.objects.filter(auction=self.in_person_auction, lot=self.in_person_lot).exists()

    def test_add_unknown_number_shows_error(self):
        self._login_admin()
        response = self.client.post(self.get_url(), data={"action": "add", "value": "does-not-exist"})
        assert response.status_code == 200
        self.assertContains(response, "No lot found")
        assert LotQueueEntry.objects.filter(auction=self.in_person_auction).count() == 0

    def test_duplicate_add_rejected(self):
        self._login_admin()
        self.client.post(self.get_url(), data={"action": "add", "value": self.in_person_lot.qr_code})
        response = self.client.post(self.get_url(), data={"action": "add", "value": self.in_person_lot.qr_code})
        assert response.status_code == 200
        self.assertContains(response, "already in the queue")
        assert LotQueueEntry.objects.filter(auction=self.in_person_auction, lot=self.in_person_lot).count() == 1

    def test_duplicate_add_via_scanner_returns_error_json(self):
        self._login_admin()
        self.client.post(self.get_url(), data={"lot_pk": self.in_person_lot.pk})
        response = self.client.post(self.get_url(), data={"lot_pk": self.in_person_lot.pk})
        data = response.json()
        assert data["ok"] is False
        assert "already in the queue" in data["message"]

    def test_sold_lot_rejected(self):
        self._login_admin()
        self.in_person_lot.auctiontos_winner = self.in_person_buyer
        self.in_person_lot.winning_price = 10
        self.in_person_lot.save()
        response = self.client.post(self.get_url(), data={"action": "add", "value": self.in_person_lot.qr_code})
        assert response.status_code == 200
        self.assertContains(response, "already been sold")
        assert LotQueueEntry.objects.filter(auction=self.in_person_auction).count() == 0

    def test_lot_from_other_auction_rejected(self):
        """A lot QR from a different auction is refused."""
        self._login_admin()
        response = self.client.post(self.get_url(), data={"lot_pk": self.lot.pk})
        data = response.json()
        assert data["ok"] is False
        assert "not part of this auction" in data["message"]

    # --- reorder / remove ----------------------------------------------------
    def test_reorder(self):
        self._login_admin()
        lot_a = self._make_in_person_lot("A")
        lot_b = self._make_in_person_lot("B")
        lot_c = self._make_in_person_lot("C")
        e_a = LotQueueEntry.objects.create(auction=self.in_person_auction, lot=lot_a, order=1)
        e_b = LotQueueEntry.objects.create(auction=self.in_person_auction, lot=lot_b, order=2)
        e_c = LotQueueEntry.objects.create(auction=self.in_person_auction, lot=lot_c, order=3)
        response = self.client.post(self.get_url(), data={"action": "reorder", "order[]": [e_c.pk, e_a.pk, e_b.pk]})
        assert response.status_code == 200
        e_a.refresh_from_db()
        e_b.refresh_from_db()
        e_c.refresh_from_db()
        assert (e_c.order, e_a.order, e_b.order) == (1, 2, 3)

    def test_remove(self):
        self._login_admin()
        entry = LotQueueEntry.objects.create(auction=self.in_person_auction, lot=self.in_person_lot, order=1)
        response = self.client.post(self.get_url(), data={"action": "remove", "entry_id": entry.pk})
        assert response.status_code == 200
        assert not LotQueueEntry.objects.filter(pk=entry.pk).exists()

    # --- fullscreen queue / current lot ---------------------------------------
    def test_fullscreen_queue_shows_head_lot(self):
        self._login_admin()
        LotQueueEntry.objects.create(auction=self.in_person_auction, lot=self.in_person_lot, order=1)
        response = self.client.get(self.fullscreen_url())
        assert response.status_code == 200
        self.assertContains(response, self.in_person_lot.lot_name)

    def test_current_lot_shows_only_the_head_lot(self):
        self._login_admin()
        self.in_person_auction.online_bidding = "allow"
        self.in_person_auction.save()
        next_lot = self._make_in_person_lot("Next up")
        LotQueueEntry.objects.create(auction=self.in_person_auction, lot=self.in_person_lot, order=1)
        LotQueueEntry.objects.create(auction=self.in_person_auction, lot=next_lot, order=2)
        for url in (self.current_lot_url(), self.current_lot_url() + "?partial=lot"):
            response = self.client.get(url)
            assert response.status_code == 200
            self.assertContains(response, self.in_person_lot.lot_name)
            self.assertNotContains(response, "Next up")
            self.assertNotContains(response, "Online high bid")

    def test_current_lot_stays_on_a_lot_sold_elsewhere_until_next(self):
        self._login_admin()
        next_lot = self._make_in_person_lot("Next up")
        entry = LotQueueEntry.objects.create(auction=self.in_person_auction, lot=self.in_person_lot, order=1)
        LotQueueEntry.objects.create(auction=self.in_person_auction, lot=next_lot, order=2)
        Lot.objects.filter(pk=self.in_person_lot.pk).update(auctiontos_winner=self.in_person_buyer, winning_price=10)
        response = self.client.get(self.current_lot_url() + "?partial=lot")
        # A GET moves nothing: the screen shows what the room is on until someone says otherwise.
        self.assertContains(response, self.in_person_lot.lot_name)
        self.assertContains(response, "Sold")
        self.client.post(self.get_url(), data={"action": "next", "from": entry.pk})
        response = self.client.get(self.current_lot_url() + "?partial=lot")
        self.assertContains(response, "Next up")

    # --- set-winner integration ----------------------------------------------
    def test_set_winner_pops_queue_and_returns_next(self):
        self._login_admin()
        next_lot = self._make_in_person_lot("Next up")
        LotQueueEntry.objects.create(auction=self.in_person_auction, lot=self.in_person_lot, order=1)
        LotQueueEntry.objects.create(auction=self.in_person_auction, lot=next_lot, order=2)
        winners_url = reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug})
        response = self.client.post(
            winners_url, data={"lot": "101-1", "price": "10", "winner": "555", "action": "save"}
        )
        data = response.json()
        assert data["success_message"] is not None
        # The sold lot's entry is kept, passed, and the new lot on the block is reported back.
        assert LotQueueEntry.objects.get(lot=self.in_person_lot).passed_at is not None
        assert LotQueueEntry.objects.get(lot=next_lot).passed_at is None
        assert data["next_queued_lot_number"] == next_lot.lot_number_display

    def test_set_winner_last_lot_returns_null_next(self):
        self._login_admin()
        LotQueueEntry.objects.create(auction=self.in_person_auction, lot=self.in_person_lot, order=1)
        winners_url = reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug})
        response = self.client.post(
            winners_url, data={"lot": "101-1", "price": "10", "winner": "555", "action": "save"}
        )
        data = response.json()
        assert data["next_queued_lot_number"] is None

    def test_set_winners_page_prefills_head_lot(self):
        self._login_admin()
        LotQueueEntry.objects.create(auction=self.in_person_auction, lot=self.in_person_lot, order=1)
        winners_url = reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug})
        response = self.client.get(winners_url)
        assert response.status_code == 200
        # escapejs escapes the hyphen, so assert on the context value.
        assert response.context["queue_head_lot_number"] == "101-1"

    # --- notifications -------------------------------------------------------
    def test_queue_add_notifies_watcher_once(self):
        self._login_admin()
        self._watch_with_push(self.in_person_lot, self.user_with_no_lots)
        other = self._make_in_person_lot("Other")
        with patch_views("send_user_notification") as mock_notify:
            self.client.post(self.get_url(), data={"lot_pk": self.in_person_lot.pk})
            # Adding another lot re-runs the top-10 pass; the watched lot must not notify twice.
            self.client.post(self.get_url(), data={"lot_pk": other.pk})
        assert mock_notify.call_count == 1
        assert mock_notify.call_args.kwargs["user"] == self.user_with_no_lots

    def test_notification_only_for_top_ten(self):
        self._login_admin()
        # Fill positions 1-10 with unwatched lots already flagged notified.
        for i in range(10):
            filler = self._make_in_person_lot(f"filler {i}")
            filler.coming_up_push_sent = True
            filler.selling_push_sent_at = timezone.now()
            filler.save()
            LotQueueEntry.objects.create(auction=self.in_person_auction, lot=filler, order=i + 1)
        watched = self._make_in_person_lot("watched")
        self._watch_with_push(watched, self.user_with_no_lots)
        # Adding it at position 11 must NOT notify yet.
        with patch_views("send_user_notification") as mock_notify:
            self.client.post(self.get_url(), data={"lot_pk": watched.pk})
        assert mock_notify.call_count == 0
        watched.refresh_from_db()
        assert watched.coming_up_push_sent is False
        # Removing the head moves the watched lot to position 10, which notifies once.
        head = LotQueueEntry.objects.filter(auction=self.in_person_auction).order_by("order").first()
        with patch_views("send_user_notification") as mock_notify:
            self.client.post(self.get_url(), data={"action": "remove", "entry_id": head.pk})
        assert mock_notify.call_count == 1
        watched.refresh_from_db()
        assert watched.coming_up_push_sent is True

    def test_notification_deduped_across_queue_then_view(self):
        """A lot that notified from the queue doesn't notify again in ViewLotSimple."""
        self._login_admin()
        self._watch_with_push(self.in_person_lot, self.user_with_no_lots)
        with patch_views("send_user_notification") as mock_notify:
            self.client.post(self.get_url(), data={"lot_pk": self.in_person_lot.pk})
        assert mock_notify.call_count == 1
        view_url = reverse(
            "htmx_lot",
            kwargs={"slug": self.in_person_auction.slug, "custom_lot_number": self.in_person_lot.custom_lot_number},
        )
        with patch_views("send_user_notification") as mock_notify:
            self.client.get(view_url)
        assert mock_notify.call_count == 0

    def test_notification_deduped_across_view_then_queue(self):
        """And vice versa."""
        self._login_admin()
        self._watch_with_push(self.in_person_lot, self.user_with_no_lots)
        view_url = reverse(
            "htmx_lot",
            kwargs={"slug": self.in_person_auction.slug, "custom_lot_number": self.in_person_lot.custom_lot_number},
        )
        with patch_views("send_user_notification") as mock_notify:
            self.client.get(view_url)
        assert mock_notify.call_count == 1
        with patch_views("send_user_notification") as mock_notify:
            self.client.post(self.get_url(), data={"lot_pk": self.in_person_lot.pk})
        assert mock_notify.call_count == 0
        # The pull-up started the cooldown, so reaching the block moments later doesn't push again.
        self.in_person_lot.refresh_from_db()
        assert self.in_person_lot.selling_push_sent_at is not None

    def test_coming_up_then_about_to_be_sold_overwrites(self):
        """A lot 10 away gets "coming up soon"; reaching the head fires "about to be sold" with the same tag,
        so the device overwrites the first.
        """
        self._login_admin()
        watched = self._make_in_person_lot("watched")
        self._watch_with_push(watched, self.user_with_no_lots)
        # A filler at the head puts `watched` at position 2.
        filler = self._make_in_person_lot("filler")
        with patch_views("send_user_notification") as mock_notify:
            self.client.post(self.get_url(), data={"lot_pk": filler.pk})
            self.client.post(self.get_url(), data={"lot_pk": watched.pk})
        # Exactly one push so far -- the "coming up soon" for the watcher.
        coming_up_calls = [c for c in mock_notify.call_args_list if c.kwargs["user"] == self.user_with_no_lots]
        assert len(coming_up_calls) == 1
        assert "coming up soon" in coming_up_calls[0].kwargs["payload"]["body"]
        coming_up_tag = coming_up_calls[0].kwargs["payload"]["tag"]
        watched.refresh_from_db()
        assert watched.coming_up_push_sent is True
        assert watched.selling_push_sent_at is None
        # Removing the filler makes it the head.
        head_entry = LotQueueEntry.objects.get(auction=self.in_person_auction, lot=filler)
        with patch_views("send_user_notification") as mock_notify:
            self.client.post(self.get_url(), data={"action": "remove", "entry_id": head_entry.pk})
        sold_calls = [c for c in mock_notify.call_args_list if c.kwargs["user"] == self.user_with_no_lots]
        assert len(sold_calls) == 1
        assert "about to be sold" in sold_calls[0].kwargs["payload"]["head"]
        # The same tag replaces rather than stacks.
        assert sold_calls[0].kwargs["payload"]["tag"] == coming_up_tag
        watched.refresh_from_db()
        assert watched.selling_push_sent_at is not None

    # --- added_to_queue stat -------------------------------------------------
    def test_adding_lot_sets_sticky_added_to_queue(self):
        self._login_admin()
        assert self.in_person_lot.added_to_queue is False
        self.client.post(self.get_url(), data={"lot_pk": self.in_person_lot.pk})
        self.in_person_lot.refresh_from_db()
        assert self.in_person_lot.added_to_queue is True
        assert self.in_person_auction.number_of_lots_added_to_queue == 1
        # Removing the entry leaves the sticky flag intact.
        entry = LotQueueEntry.objects.get(auction=self.in_person_auction, lot=self.in_person_lot)
        self.client.post(self.get_url(), data={"action": "remove", "entry_id": entry.pk})
        self.in_person_lot.refresh_from_db()
        assert self.in_person_lot.added_to_queue is True
        assert self.in_person_auction.number_of_lots_added_to_queue == 1

    # --- websocket real-time refresh -----------------------------------------
    def test_queue_mutation_broadcasts_websocket(self):
        """Every queue mutation pokes the admin auction group so open kiosk screens re-fetch."""
        self._login_admin()
        fake_layer = MagicMock()
        with patch_views("channels.layers.get_channel_layer", return_value=fake_layer):
            with patch_views("async_to_sync", side_effect=lambda f: f):
                self.client.post(self.get_url(), data={"lot_pk": self.in_person_lot.pk})
        assert any(
            call.args[0] == f"auctions_{self.in_person_auction.pk}" and call.args[1].get("type") == "queue_updated"
            for call in fake_layer.group_send.call_args_list
        )

    # --- moving the queue: Next, Back, and recording the lot on the block ---------
    def _queue(self, *names):
        lots = [self._make_in_person_lot(name) for name in names]
        entries = [
            LotQueueEntry.objects.create(auction=self.in_person_auction, lot=lot, order=i + 1)
            for i, lot in enumerate(lots)
        ]
        return lots, entries

    def _move(self, action, showing):
        return self.client.post(self.get_url(), data={"action": action, "from": showing.pk if showing else 0})

    def _on_the_block(self):
        from auctions.views.selling import queue_entries, queue_split

        entry = queue_split(queue_entries(self.in_person_auction))[1]
        return entry.lot if entry else None

    def _sell(self, lot, price="10", winner="555"):
        url = reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug})
        data = {"lot": lot.lot_number_display, "price": price, "winner": winner, "action": "save"}
        return self.client.post(url, data=data).json()

    def _pull_up(self, lot, preview=False):
        url = reverse(
            "htmx_lot", kwargs={"slug": self.in_person_auction.slug, "custom_lot_number": lot.custom_lot_number}
        )
        return self.client.get(url + ("?preview=1" if preview else ""))

    @staticmethod
    def _announced(mock, lot):
        return [c for c in mock.call_args_list if c.kwargs["payload"]["head"] == f"{lot.lot_name} is about to be sold"]

    def test_next_and_back_move_the_lot_on_the_block(self):
        self._login_admin()
        (a, b), (entry_a, entry_b) = self._queue("A", "B")
        self._move("next", entry_a)
        assert self._on_the_block() == b
        entry_a.refresh_from_db()
        assert entry_a.passed_at is not None
        self._move("back", entry_b)
        assert self._on_the_block() == a

    def test_a_press_from_a_screen_that_is_behind_does_nothing(self):
        """Two presses of Next before the screen refreshes, or Next as a sale moves the queue: one lot, not two."""
        self._login_admin()
        (_a, b, _c), (entry_a, _entry_b, _entry_c) = self._queue("A", "B", "C")
        self._move("next", entry_a)
        response = self._move("next", entry_a)
        assert self._on_the_block() == b
        self.assertNotContains(response, "last lot")

    def test_the_ends_of_the_queue_say_so(self):
        self._login_admin()
        (a,), (entry_a,) = self._queue("A")
        self.assertContains(self._move("back", entry_a), "first lot")
        self._move("next", entry_a)
        assert self._on_the_block() is None
        self.assertContains(self._move("next", None), "last lot")
        self._move("back", None)
        assert self._on_the_block() == a

    def test_recording_the_lot_on_the_block_moves_the_queue_and_announces_the_next(self):
        self._login_admin()
        (a, b), _entries = self._queue("A", "B")
        self._watch_with_push(b, self.user_with_no_lots)
        with patch_views("send_user_notification") as mock_notify:
            data = self._sell(a)
        assert self._on_the_block() == b
        assert data["next_queued_lot_number"] == b.lot_number_display
        assert len(self._announced(mock_notify, b)) == 1

    def test_winners_recorded_later_follow_the_room_not_the_recorder(self):
        """The queue run with Next, winners entered afterwards: watchers hear it when the room gets there,
        once, and set winners walks the recorder through the queue behind it.
        """
        self._login_admin()
        (a, b, c), (entry_a, entry_b, _entry_c) = self._queue("A", "B", "C")
        self._watch_with_push(b, self.user_with_no_lots)
        with patch_views("send_user_notification") as mock_notify:
            self._move("next", entry_a)
            self._move("next", entry_b)
            assert self._on_the_block() == c
            data = self._sell(a)
            assert data["next_queued_lot_number"] == b.lot_number_display
            self._pull_up(b)
            data = self._sell(b)
            assert data["next_queued_lot_number"] == c.lot_number_display
        assert self._on_the_block() == c
        assert len(self._announced(mock_notify, b)) == 1

    def test_set_winners_pulls_up_the_first_lot_still_to_record(self):
        self._login_admin()
        (a, _b), (entry_a, _entry_b) = self._queue("A", "B")
        self._move("next", entry_a)
        winners_url = reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug})
        # The lot on the block first...
        assert self.client.get(winners_url).context["queue_head_lot_number"] == _b.lot_number_display
        self._move("next", LotQueueEntry.objects.get(lot=_b))
        # ...then, once the room has finished, the first lot nobody recorded.
        assert self.client.get(winners_url).context["queue_head_lot_number"] == a.lot_number_display

    def test_a_double_check_pulls_up_the_next_lot_even_though_it_is_recorded(self):
        self._login_admin()
        (a, b), _entries = self._queue("A", "B")
        self._sell(a)
        self._sell(b)
        data = self._sell(a)
        assert data["success_message"] == "This lot has been double checked"
        assert data["next_queued_lot_number"] == b.lot_number_display

    def test_a_double_check_that_does_not_match_still_pulls_up_the_next_lot(self):
        self._login_admin()
        (a, b), _entries = self._queue("A", "B")
        self._sell(a, price="10")
        data = self._sell(a, price="12")
        assert data["banner"] == "error"
        assert data["last_sold_lot_number"] == a.lot_number_display
        assert data["next_queued_lot_number"] == b.lot_number_display
        # The first sale stands until someone undoes it, and the queue stays where the room is.
        a.refresh_from_db()
        assert a.winning_price == 10
        assert self._on_the_block() == b

    def test_a_lot_sold_out_of_turn_is_skipped(self):
        """Two people recording different lots: each is pulled up the next lot nobody has recorded."""
        self._login_admin()
        (a, b, c), _entries = self._queue("A", "B", "C")
        self._sell(b)
        assert self._on_the_block() == a
        data = self._sell(a)
        assert data["next_queued_lot_number"] == c.lot_number_display
        assert self._on_the_block() == c

    def test_next_skips_recorded_lots_but_back_does_not(self):
        self._login_admin()
        (_a, b, c), (entry_a, _entry_b, entry_c) = self._queue("A", "B", "C")
        Lot.objects.filter(pk=b.pk).update(auctiontos_winner=self.in_person_buyer, winning_price=5)
        self._move("next", entry_a)
        assert self._on_the_block() == c
        self._move("back", entry_c)
        assert self._on_the_block() == b

    def test_a_lot_lingering_on_the_block_is_not_announced_again(self):
        self._login_admin()
        a = self._make_in_person_lot("A")
        self._watch_with_push(a, self.user_with_no_lots)
        with patch_views("send_user_notification") as mock_notify:
            self.client.post(self.get_url(), data={"lot_pk": a.pk})
            Lot.objects.filter(pk=a.pk).update(selling_push_sent_at=timezone.now() - timedelta(minutes=30))
            # Every queue change reruns the pass; the lot on the block has had its turn announced.
            self.client.post(self.get_url(), data={"lot_pk": self._make_in_person_lot("B").pk})
        assert len(self._announced(mock_notify, a)) == 1

    def test_going_back_to_a_lot_announces_it_again_only_after_the_cooldown(self):
        self._login_admin()
        (_a, b), (entry_a, entry_b) = self._queue("A", "B")
        self._watch_with_push(b, self.user_with_no_lots)
        with patch_views("send_user_notification") as mock_notify:
            self._move("next", entry_a)
            # Next once too often and straight back: nothing new.
            self._move("back", entry_b)
            self._move("next", entry_a)
        assert len(self._announced(mock_notify, b)) == 1
        Lot.objects.filter(pk=b.pk).update(selling_push_sent_at=timezone.now() - timedelta(minutes=3))
        with patch_views("send_user_notification") as mock_notify:
            # The room came back to it later: it's really being sold now.
            self._move("back", entry_b)
            self._move("next", entry_a)
        assert len(self._announced(mock_notify, b)) == 1

    def test_a_mistyped_lot_number_is_announced_again_when_it_really_comes_up(self):
        self._login_admin()
        lot = self._make_in_person_lot("Typo")
        self._watch_with_push(lot, self.user_with_no_lots)
        with patch_views("send_user_notification") as mock_notify:
            # Still being typed: set winners previews it without announcing.
            self._pull_up(lot, preview=True)
            assert not self._announced(mock_notify, lot)
            # Tabbed away from with the wrong number, then pulled up again at once.
            self._pull_up(lot)
            self._pull_up(lot)
            assert len(self._announced(mock_notify, lot)) == 1
            Lot.objects.filter(pk=lot.pk).update(selling_push_sent_at=timezone.now() - timedelta(minutes=40))
            self._pull_up(lot)
        assert len(self._announced(mock_notify, lot)) == 2

    def test_re_adding_a_passed_lot_puts_it_back_on_the_end(self):
        self._login_admin()
        (a, b), (entry_a, _entry_b) = self._queue("A", "B")
        self._move("next", entry_a)
        self.client.post(self.get_url(), data={"lot_pk": a.pk})
        entry_a.refresh_from_db()
        assert entry_a.passed_at is None
        assert entry_a.order == 3
        assert self._on_the_block() == b

    def test_reorder_keeps_passed_lots_in_front(self):
        self._login_admin()
        (_a, b, c), (entry_a, entry_b, entry_c) = self._queue("A", "B", "C")
        self._move("next", entry_a)
        self.client.post(self.get_url(), data={"action": "reorder", "order[]": [entry_c.pk, entry_b.pk]})
        for entry in (entry_a, entry_b, entry_c):
            entry.refresh_from_db()
        assert (entry_a.order, entry_c.order, entry_b.order) == (1, 2, 3)
        assert self._on_the_block() == c

    def test_a_lot_that_did_not_sell_can_be_queued_for_another_go(self):
        self._login_admin()
        (a, b), (_entry_a, entry_b) = self._queue("A", "B")
        self._watch_with_push(a, self.user_with_no_lots)
        with patch_views("send_user_notification"):
            self.client.post(reverse("lot_end_unsold", kwargs={"pk": a.pk}))
            self.client.post(self.get_url(), data={"lot_pk": a.pk})
            data = self._sell(b)
        assert data["next_queued_lot_number"] == a.lot_number_display
        assert self._on_the_block() == a
        Lot.objects.filter(pk=a.pk).update(selling_push_sent_at=timezone.now() - timedelta(minutes=10))
        with patch_views("send_user_notification") as mock_notify:
            self._move("back", LotQueueEntry.objects.get(lot=a))
            self._move("next", entry_b)
        assert len(self._announced(mock_notify, a)) == 1

    def test_ending_the_lot_on_the_block_unsold_moves_the_queue(self):
        self._login_admin()
        (a, b), _entries = self._queue("A", "B")
        self.client.post(reverse("lot_end_unsold", kwargs={"pk": a.pk}))
        assert self._on_the_block() == b


class AlternativeSplitLabelTests(StandardTestCase):
    """Test the alternative_split_label field"""

    def test_custom_label(self):
        """Test that a custom label can be set"""
        self.online_auction.alternative_split_label = "supporter"
        self.online_auction.save()
        auction = Auction.objects.get(pk=self.online_auction.pk)
        assert auction.alternative_split_label == "supporter"

    def test_label_in_csv_export_header(self):
        """Test that the custom label appears in CSV export header"""
        self.online_auction.alternative_split_label = "patron"
        self.online_auction.save()
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("user_list", kwargs={"slug": self.online_auction.slug}))
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "Patron" in content
        assert "Club member" not in content
