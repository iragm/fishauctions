"""Tests for Part 7 — recruit volunteers (web feature)."""

import datetime
import uuid
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionHistory,
    AuctionTOS,
    Club,
    ClubMember,
    InvoiceAdjustment,
    MobileDevice,
    PickupLocation,
    VolunteerJob,
    VolunteerSignup,
)

User = get_user_model()


class VolunteerBase(TestCase):
    def setUp(self):
        now = timezone.now()
        self.admin = User.objects.create_user("vadmin", password="x", email="va@example.com")
        self.helper1 = User.objects.create_user("h1", password="x", email="h1@example.com")
        self.helper2 = User.objects.create_user("h2", password="x", email="h2@example.com")
        self.nodevice = User.objects.create_user("nd", password="x", email="nd@example.com")
        self.auction = Auction.objects.create(
            created_by=self.admin,
            title="Help Auction",
            is_online=False,
            date_start=now,
            date_end=now + datetime.timedelta(hours=6),
        )
        self.location = PickupLocation.objects.create(auction=self.auction, name="Main")
        for u in (self.helper1, self.helper2, self.nodevice):
            AuctionTOS.objects.create(auction=self.auction, user=u, pickup_location=self.location)
        for u in (self.helper1, self.helper2):
            MobileDevice.objects.create(user=u, device_uuid=uuid.uuid4())

    def _volunteers_url(self):
        return reverse("auction_volunteers", kwargs={"slug": self.auction.slug})

    def _job_url(self, job):
        return reverse("auction_volunteer_job", kwargs={"slug": self.auction.slug, "job_pk": job.pk})


class VolunteerPageGatingTests(VolunteerBase):
    def test_admin_can_view(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self._volunteers_url()).status_code, 200)

    def test_non_admin_forbidden(self):
        self.client.force_login(self.helper1)
        self.assertEqual(self.client.get(self._volunteers_url()).status_code, 403)

    def test_online_auction_404(self):
        self.auction.is_online = True
        self.auction.save()
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self._volunteers_url()).status_code, 404)

    def test_ribbon_shows_menu_item_in_person_only(self):
        self.client.force_login(self.admin)
        resp = self.client.get(self._volunteers_url())
        self.assertContains(resp, "Recruit volunteers")


class VolunteerHelperCountTests(VolunteerBase):
    def test_non_checkin_counts_joined_app_users(self):
        from auctions.views import volunteer_helper_count

        # helper1 + helper2 have devices; nodevice does not.
        self.assertEqual(volunteer_helper_count(self.auction), 2)

    def test_checkin_mode_counts_only_checked_in(self):
        from auctions.views import volunteer_helper_count

        club = Club.objects.create(name="C")
        ClubMember.objects.create(club=club, user=self.admin, permission_admin=True)
        self.auction.club = club
        self.auction.manage_users_through_club = "checkin"
        self.auction.save()
        self.assertTrue(self.auction.use_check_in_mode)
        # Only helper1 checked in.
        tos = AuctionTOS.objects.get(auction=self.auction, user=self.helper1)
        tos.checked_in = timezone.now()
        tos.save()
        self.assertEqual(volunteer_helper_count(self.auction), 1)


class VolunteerCreateTests(VolunteerBase):
    def test_create_job_fans_out_and_logs_history(self):
        self.client.force_login(self.admin)
        with patch("auctions.notifications.notify_user") as mock_notify:
            resp = self.client.post(
                self._volunteers_url(),
                {"description": "Run the door", "bounty": "", "people_needed": 2},
            )
        self.assertEqual(resp.status_code, 302)
        job = VolunteerJob.objects.get(auction=self.auction)
        self.assertEqual(job.people_needed, 2)
        self.assertIsNone(job.bounty)
        # One push per eligible helper (helper1, helper2), not the device-less user.
        notified = {c.args[0].pk for c in mock_notify.call_args_list}
        self.assertEqual(notified, {self.helper1.pk, self.helper2.pk})
        self.assertTrue(
            AuctionHistory.objects.filter(auction=self.auction, action__icontains="Asked for 2 people").exists()
        )


class VolunteerSignupTests(VolunteerBase):
    def _job(self, bounty=None, people_needed=1):
        return VolunteerJob.objects.create(
            auction=self.auction,
            created_by=self.admin,
            description="Sort lots",
            bounty=bounty,
            people_needed=people_needed,
        )

    def test_signup_with_bounty_creates_linked_discount(self):
        job = self._job(bounty=Decimal("10.00"))
        self.client.force_login(self.helper1)
        with patch("auctions.views.withdraw_volunteer_notification") as mock_withdraw:
            resp = self.client.post(self._job_url(job))
        self.assertEqual(resp.status_code, 302)
        signup = VolunteerSignup.objects.get(job=job)
        adj = signup.invoice_adjustment
        self.assertIsNotNone(adj)
        self.assertEqual(adj.adjustment_type, "DISCOUNT")
        self.assertEqual(adj.amount, 10)
        self.assertEqual(adj.notes, "Volunteer: Sort lots")
        self.assertEqual(adj.invoice.auctiontos_user.user, self.helper1)
        # Filling the (1-person) job fires the retract + history.
        mock_withdraw.assert_called_once()
        self.assertTrue(
            AuctionHistory.objects.filter(auction=self.auction, action__icontains="signed up for Sort lots").exists()
        )
        self.assertTrue(
            AuctionHistory.objects.filter(auction=self.auction, action__icontains="Volunteer job filled").exists()
        )

    def test_volunteer_no_bounty_creates_no_adjustment(self):
        job = self._job(bounty=None)
        self.client.force_login(self.helper1)
        self.client.post(self._job_url(job))
        signup = VolunteerSignup.objects.get(job=job)
        self.assertIsNone(signup.invoice_adjustment)
        self.assertEqual(InvoiceAdjustment.objects.count(), 0)

    def test_fill_boundary_refuses_next_signup(self):
        job = self._job(people_needed=1)
        self.client.force_login(self.helper1)
        self.client.post(self._job_url(job))
        self.client.force_login(self.helper2)
        self.client.post(self._job_url(job))
        self.assertEqual(VolunteerSignup.objects.filter(job=job).count(), 1)  # second refused

    def test_duplicate_signup_is_noop(self):
        job = self._job(people_needed=3)
        self.client.force_login(self.helper1)
        self.client.post(self._job_url(job))
        self.client.post(self._job_url(job))  # same user again
        self.assertEqual(VolunteerSignup.objects.filter(job=job).count(), 1)

    def test_signup_requires_tos(self):
        outsider = User.objects.create_user("outsider", password="x", email="o@example.com")
        job = self._job()
        self.client.force_login(outsider)
        self.client.post(self._job_url(job))
        self.assertEqual(VolunteerSignup.objects.filter(job=job).count(), 0)

    def test_cancel_job_logs_history_and_retracts(self):
        job = self._job()
        self.client.force_login(self.admin)
        with patch("auctions.views.withdraw_volunteer_notification") as mock_withdraw:
            self.client.post(self._volunteers_url(), {"action": "cancel", "job_pk": job.pk})
        job.refresh_from_db()
        self.assertTrue(job.canceled)
        mock_withdraw.assert_called_once()
        self.assertTrue(
            AuctionHistory.objects.filter(auction=self.auction, action__icontains="Canceled volunteer job").exists()
        )
