"""Tests for the friction instrument: which form, which field, how many attempts, did they finish.

See auctions/friction_models.py for what the columns mean and auctions/form_friction.py for the
mixin that writes them.
"""

import re
import time
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.staticfiles.storage import staticfiles_storage
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from auctions import form_friction
from auctions.form_friction import SESSION_KEY, FormFrictionMixin, error_codes
from auctions.models import FormFailure
from auctions.tests import StandardTestCase
from auctions.views import (
    AuctionCreateView,
    AuctionInfo,
    AuctionUpdate,
    ClubEditView,
    ClubEmailSettingsView,
    ClubMembershipSettingsView,
    LotCreateView,
    LotUpdate,
)

INSTRUMENTED_VIEWS = [
    AuctionCreateView,
    AuctionInfo,
    AuctionUpdate,
    ClubEditView,
    ClubEmailSettingsView,
    ClubMembershipSettingsView,
    LotCreateView,
    LotUpdate,
]


class FormFrictionWiringTests(TestCase):
    """The mixin has to be ahead of Django's FormMixin or it is never called at all."""

    def test_the_mixin_comes_before_djangos_form_handling(self):
        from django.views.generic.edit import FormMixin as DjangoFormMixin

        for view in INSTRUMENTED_VIEWS:
            order = view.__mro__
            self.assertIn(FormFrictionMixin, order, f"{view.__name__} lost the instrument")
            if DjangoFormMixin in order:
                self.assertLess(
                    order.index(FormFrictionMixin),
                    order.index(DjangoFormMixin),
                    f"{view.__name__} has FormFrictionMixin behind FormMixin, so it never runs",
                )

    def test_the_instrumented_views_still_resolve_form_valid_and_form_invalid(self):
        for view in INSTRUMENTED_VIEWS:
            self.assertTrue(callable(view.form_valid), view.__name__)
            self.assertTrue(callable(view.form_invalid), view.__name__)


class ErrorCodeTests(TestCase):
    """Codes, never messages -- a message can carry whatever the user typed."""

    def test_codes_are_read_off_the_validation_errors(self):
        from django import forms

        class Example(forms.Form):
            name = forms.CharField()
            count = forms.IntegerField(max_value=5)

        form = Example(data={"count": "9"})
        self.assertFalse(form.is_valid())
        self.assertEqual(error_codes(form), {"name": ["required"], "count": ["max_value"]})

    def test_the_value_somebody_typed_is_not_in_the_result(self):
        from django import forms

        class Example(forms.Form):
            email = forms.EmailField()

        form = Example(data={"email": "definitely-not-an-email-address"})
        self.assertFalse(form.is_valid())
        self.assertEqual(error_codes(form), {"email": ["invalid"]})
        self.assertNotIn("definitely-not-an-email-address", str(error_codes(form)))

    def test_a_form_that_cannot_report_its_errors_returns_nothing_rather_than_raising(self):
        class Broken:
            @property
            def errors(self):
                message = "no"
                raise ValueError(message)

        self.assertEqual(error_codes(Broken()), {})


class _Recorder(FormFrictionMixin):
    """The mixin with the Django view underneath it replaced by a stub."""

    def __init__(self, request):
        self.request = request

    def form_invalid(self, form):
        return super().form_invalid(form)

    def form_valid(self, form):
        return super().form_valid(form)


class _Base:
    def form_invalid(self, form):
        return "invalid"

    def form_valid(self, form):
        return "valid"


class Recorder(_Recorder, _Base):
    pass


class FormFrictionMixinTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _request(self, user=None, path="/lots/new/"):
        request = self.factory.post(path)
        request.user = user or _Anonymous()
        session = _Session()
        request.session = session
        return request

    def _form(self, valid=False):
        from django import forms

        class Example(forms.Form):
            name = forms.CharField()

        return Example(data={"name": "ok"} if valid else {})

    def test_a_rejection_is_recorded_with_its_field_and_code(self):
        request = self._request()
        form = self._form()
        form.is_valid()
        self.assertEqual(Recorder(request).form_invalid(form), "invalid")
        row = FormFailure.objects.get()
        self.assertEqual(row.form_name, "Example")
        self.assertEqual(row.field_errors, {"name": ["required"]})
        self.assertEqual(row.attempt, 1)
        self.assertFalse(row.resolved)
        self.assertEqual(row.url, "/lots/new/")

    def test_attempts_climb_within_a_session(self):
        request = self._request()
        view = Recorder(request)
        for _ in range(3):
            form = self._form()
            form.is_valid()
            view.form_invalid(form)
        self.assertEqual([row.attempt for row in FormFailure.objects.order_by("id")], [1, 2, 3])

    def test_a_run_that_ends_in_success_is_marked_resolved(self):
        request = self._request()
        view = Recorder(request)
        for _ in range(2):
            form = self._form()
            form.is_valid()
            view.form_invalid(form)
        good = self._form(valid=True)
        good.is_valid()
        self.assertEqual(view.form_valid(good), "valid")
        self.assertEqual(FormFailure.objects.filter(resolved=True).count(), 2)
        self.assertIsNotNone(FormFailure.objects.first().resolved_at)

    def test_a_run_that_is_abandoned_stays_unresolved(self):
        """The column the whole table is for."""
        request = self._request()
        view = Recorder(request)
        for _ in range(2):
            form = self._form()
            form.is_valid()
            view.form_invalid(form)
        self.assertEqual(FormFailure.objects.filter(resolved=False).count(), 2)

    def test_getting_it_right_first_time_writes_nothing(self):
        request = self._request()
        good = self._form(valid=True)
        good.is_valid()
        Recorder(request).form_valid(good)
        self.assertEqual(FormFailure.objects.count(), 0)

    def test_one_persons_success_does_not_resolve_another_persons_run(self):
        alice = User.objects.create_user(username="alice", password="x")
        bob = User.objects.create_user(username="bob", password="x")
        for user in (alice, bob):
            request = self._request(user=user)
            form = self._form()
            form.is_valid()
            Recorder(request).form_invalid(form)
        request = self._request(user=alice)
        request.session[SESSION_KEY] = {"Example": 1}
        good = self._form(valid=True)
        good.is_valid()
        Recorder(request).form_valid(good)
        self.assertTrue(FormFailure.objects.get(user=alice).resolved)
        self.assertFalse(FormFailure.objects.get(user=bob).resolved)

    def test_a_held_enter_key_stops_writing_rows(self):
        request = self._request()
        view = Recorder(request)
        for _ in range(form_friction.MAX_ATTEMPTS_RECORDED + 5):
            form = self._form()
            form.is_valid()
            view.form_invalid(form)
        self.assertEqual(FormFailure.objects.count(), form_friction.MAX_ATTEMPTS_RECORDED)

    def test_the_instrument_never_breaks_the_form(self):
        """An instrument that can 500 a form is worse than no instrument."""
        request = self._request()
        request.session = _ExplodingSession()
        form = self._form()
        form.is_valid()
        with self.assertLogs("auctions.form_friction", level="ERROR"):
            self.assertEqual(Recorder(request).form_invalid(form), "invalid")

    def test_nothing_the_user_typed_is_stored(self):
        from django import forms

        class Example(forms.Form):
            email = forms.EmailField()

        form = Example(data={"email": "hunter2@@@"})
        form.is_valid()
        Recorder(self._request()).form_invalid(form)
        row = FormFailure.objects.get()
        self.assertNotIn("hunter2", str(row.field_errors))
        self.assertNotIn("hunter2", row.url)


class _Anonymous:
    is_authenticated = False


class _Session(dict):
    session_key = "test-session-key"


class _ExplodingSession:
    session_key = "x"

    message = "session backend is down"

    def get(self, *args, **kwargs):
        raise RuntimeError(self.message)

    def __setitem__(self, key, value):
        raise RuntimeError(self.message)


class FrictionEndToEndTests(StandardTestCase):
    """Through a real view, so the wiring is proved and not just the mixin."""

    def test_a_rejected_auction_edit_is_recorded_and_then_resolved(self):
        self.client.login(username="my_lot", password="testpassword")
        url = reverse("edit_auction", kwargs={"slug": self.online_auction.slug})
        response = self.client.post(url, {"tax": "not a number"})
        self.assertEqual(response.status_code, 200)
        row = FormFailure.objects.get()
        self.assertEqual(row.form_name, "AuctionEditForm")
        self.assertEqual(row.user, self.user)
        self.assertEqual(row.url, url)
        self.assertFalse(row.resolved)
        self.assertIn("tax", row.field_errors)

    def test_a_rejected_lot_submission_is_recorded(self):
        """The seller's path, not the organizer's: a bounced lot is somebody who did not sell."""
        self.user.first_name = "Test"
        self.user.last_name = "Seller"
        self.user.save()
        userdata = self.user.userdata
        userdata.address = "123 Test Street"
        userdata.save()
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.post(reverse("new_lot"), {})
        self.assertEqual(response.status_code, 200)
        row = FormFailure.objects.filter(form_name="CreateLotForm").first()
        self.assertIsNotNone(row, f"nothing recorded; rows are {list(FormFailure.objects.all())}")
        self.assertEqual(row.user, self.user)


class FormFailureModelTests(TestCase):
    def test_str_names_the_form_and_the_fields(self):
        row = FormFailure(form_name="AuctionEditForm", field_errors={"tax": ["invalid"]}, attempt=3)
        self.assertIn("AuctionEditForm", str(row))
        self.assertIn("tax", str(row))
        self.assertIn("3", str(row))

    def test_a_row_with_no_field_errors_still_has_a_str(self):
        self.assertIn("no field", str(FormFailure(form_name="X")))


class AbandonTokenTests(TestCase):
    """The signed form name is what bounds an endpoint that cannot require authentication."""

    def test_a_token_round_trips(self):
        self.assertEqual(
            form_friction.read_abandon_token(form_friction.abandon_token("AuctionEditForm")), "AuctionEditForm"
        )

    def test_a_forged_token_is_worth_nothing(self):
        for token in ("", "nonsense", "eyJmb3JtIjogIkZha2VGb3JtIn0:junk"):
            self.assertEqual(form_friction.read_abandon_token(token), "")

    def test_a_stale_token_expires(self):
        from django.core import signing

        token = signing.dumps({"form": "X"}, salt=form_friction.ABANDON_SALT)
        with patch(
            "django.core.signing.time.time", return_value=time.time() + form_friction.ABANDON_TOKEN_MAX_AGE + 60
        ):
            self.assertEqual(form_friction.read_abandon_token(token), "")

    def test_an_instrumented_view_hands_the_page_a_token(self):
        for view in INSTRUMENTED_VIEWS:
            self.assertTrue(hasattr(view, "get_context_data"), view.__name__)
            self.assertIn(FormFrictionMixin, view.__mro__)


class AbandonmentBeaconTests(StandardTestCase):
    """Somebody edited a form and left. On this site that is the common way of giving up."""

    def setUp(self):
        super().setUp()
        self.url = reverse("form_abandoned")
        self.token = form_friction.abandon_token("AuctionEditForm")

    def test_an_abandonment_is_recorded_with_the_fields_left_unsaved(self):
        response = self.client.post(
            self.url,
            {"token": self.token, "fields": "tax,minimum_bid", "seconds": "94", "url": "/auctions/x/edit/"},
        )
        self.assertEqual(response.status_code, 201)
        row = FormFailure.objects.get()
        self.assertEqual(row.kind, "abandoned")
        self.assertEqual(row.form_name, "AuctionEditForm")
        self.assertEqual(sorted(row.field_errors), ["minimum_bid", "tax"])
        self.assertEqual(row.seconds_on_page, 94)
        self.assertEqual(row.url, "/auctions/x/edit/")

    def test_a_signed_in_person_is_recorded_as_themselves(self):
        self.client.login(username="my_lot", password="testpassword")
        self.client.post(self.url, {"token": self.token, "fields": "tax", "seconds": "10"})
        self.assertEqual(FormFailure.objects.get().user, self.user)

    def test_a_form_name_nobody_signed_is_refused(self):
        """The endpoint takes no authentication, so its vocabulary has to come from the server."""
        response = self.client.post(self.url, {"token": "made-up", "fields": "tax", "seconds": "5"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(FormFailure.objects.count(), 0)

    def test_the_same_form_is_only_recorded_once_a_session(self):
        for _ in range(5):
            self.client.post(self.url, {"token": self.token, "fields": "tax", "seconds": "5"})
        self.assertEqual(FormFailure.objects.count(), 1)

    def test_a_different_form_in_the_same_session_is_recorded(self):
        self.client.post(self.url, {"token": self.token, "fields": "tax", "seconds": "5"})
        other = form_friction.abandon_token("ClubEditForm")
        self.client.post(self.url, {"token": other, "fields": "name", "seconds": "5"})
        self.assertEqual(FormFailure.objects.count(), 2)

    def test_an_absurd_number_of_fields_is_capped(self):
        fields = ",".join(f"field_{index}" for index in range(500))
        self.client.post(self.url, {"token": self.token, "fields": fields, "seconds": "5"})
        self.assertEqual(len(FormFailure.objects.get().field_errors), form_friction.MAX_ABANDONED_FIELDS)

    def test_a_nonsense_duration_does_not_500(self):
        for seconds in ("abc", "", "-1", "999999999999"):
            FormFailure.objects.all().delete()
            self.client.cookies.clear()
            response = self.client.post(self.url, {"token": self.token, "fields": "tax", "seconds": seconds})
            self.assertIn(response.status_code, (200, 201), seconds)

    def test_an_off_site_url_is_not_filed_as_one_of_ours(self):
        self.client.post(self.url, {"token": self.token, "fields": "tax", "seconds": "5", "url": "javascript:alert(1)"})
        self.assertEqual(FormFailure.objects.get().url, "")

    def test_no_value_anybody_typed_reaches_the_row(self):
        """Values are the one thing an abandonment must not keep: they were never saved."""
        self.client.post(
            self.url,
            {"token": self.token, "fields": "tax", "seconds": "5", "values": "secret stuff", "tax": "77"},
        )
        row = FormFailure.objects.get()
        self.assertNotIn("secret", str(row.field_errors))
        self.assertNotIn("77", str(row.field_errors))


class UnsavedChangesBarTests(StandardTestCase):
    """The bar itself: on every page, finding its own forms.

    It used to be a per-template include, and it was on 7 of the 99 templates that render a form.
    """

    def test_the_bar_and_its_script_are_on_every_page(self):
        self.client.login(username="my_lot", password="testpassword")
        for url in ("/", reverse("preferences"), reverse("edit_auction", kwargs={"slug": self.online_auction.slug})):
            page = self.client.get(url).content.decode()
            self.assertIn('id="unsaved-changes-bar"', page, url)
            # Through the storage: whether this name is hashed depends on whether collectstatic has
            # run, which differs between CI and a dev container -- see fishauctions/static_storage.py.
            self.assertIn(staticfiles_storage.url("js/unsaved_changes.js"), page, url)
            self.assertIn('id="unsaved-changes-config"', page, url)

    def test_an_instrumented_page_carries_a_usable_token(self):
        self.client.login(username="my_lot", password="testpassword")
        page = self.client.get(reverse("edit_auction", kwargs={"slug": self.online_auction.slug})).content.decode()
        match = re.search(r'id="unsaved-changes-config" data-beacon-url="[^"]*" data-token="([^"]*)"', page)
        self.assertIsNotNone(match, "no token on the auction settings page")
        self.assertEqual(form_friction.read_abandon_token(match.group(1)), "AuctionEditForm")

    def test_a_page_with_no_instrumented_form_still_gets_the_bar_but_no_token(self):
        """The bar is a usability feature; the beacon is an instrument. They are independent."""
        page = self.client.get("/").content.decode()
        self.assertIn('id="unsaved-changes-bar"', page)
        self.assertIn('data-token=""', page)

    def test_the_old_per_template_include_is_gone(self):
        """Seven templates included it and ninety-two did not; that is why it is in base.html now."""
        self.assertFalse((Path(__file__).resolve().parent / "templates" / "leave_page_warning.js").exists())


class BeaconCsrfTests(StandardTestCase):
    """The beacon has to carry a CSRF token, and the default test client hides that it does not.

    ``FormAbandonedBeacon`` uses DRF's ``SessionAuthentication``, which enforces CSRF for a
    signed-in session. Django's test client sets ``_dont_enforce_csrf_checks`` unless it is built
    with ``enforce_csrf_checks=True``, so every other test here would pass with the token missing
    and every abandonment by a logged-in organizer -- which is every abandonment worth having --
    would 403 in production.
    """

    def setUp(self):
        super().setUp()
        self.url = reverse("form_abandoned")
        self.token = form_friction.abandon_token("AuctionEditForm")

    def _strict(self, login=False):
        client = Client(enforce_csrf_checks=True)
        if login:
            client.login(username="my_lot", password="testpassword")
        return client

    def test_a_signed_in_beacon_with_a_csrf_token_is_recorded(self):
        client = self._strict(login=True)
        client.get(reverse("edit_auction", kwargs={"slug": self.online_auction.slug}))
        response = client.post(
            self.url,
            {
                "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
                "token": self.token,
                "fields": "tax",
                "seconds": "30",
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(FormFailure.objects.get().user, self.user)

    def test_a_signed_in_beacon_without_one_is_refused(self):
        client = self._strict(login=True)
        client.get(reverse("edit_auction", kwargs={"slug": self.online_auction.slug}))
        response = client.post(self.url, {"token": self.token, "fields": "tax", "seconds": "30"})
        self.assertEqual(response.status_code, 403)

    def test_the_page_hands_the_script_a_csrf_token_to_send(self):
        self.client.login(username="my_lot", password="testpassword")
        page = self.client.get(reverse("edit_auction", kwargs={"slug": self.online_auction.slug})).content.decode()
        match = re.search(r'id="unsaved-changes-config"[^>]*data-csrf="([^"]+)"', page)
        self.assertIsNotNone(match, "no CSRF token on the config element")
        self.assertGreater(len(match.group(1)), 10)

    def test_the_script_puts_it_in_the_body_rather_than_a_header(self):
        """sendBeacon cannot set headers, so it has to ride in the POST body."""
        script = (Path(__file__).resolve().parent / "static" / "js" / "unsaved_changes.js").read_text()
        self.assertIn('payload.append("csrfmiddlewaretoken", csrfToken)', script)


class AnonymousRunTests(TestCase):
    """A signed-out person needs a session key, or their run cannot be told from anybody else's."""

    def setUp(self):
        self.factory = RequestFactory()

    def _request(self, session):
        request = self.factory.post("/lots/new/")
        request.user = _Anonymous()
        request.session = session
        return request

    def _rejected_form(self):
        from django import forms

        class Example(forms.Form):
            name = forms.CharField()

        form = Example(data={})
        form.is_valid()
        return form

    def test_a_first_bounce_forces_a_session_key_into_existence(self):
        from django.contrib.sessions.backends.db import SessionStore

        session = SessionStore()
        self.assertIsNone(session.session_key)
        Recorder(self._request(session)).form_invalid(self._rejected_form())
        row = FormFailure.objects.get()
        self.assertTrue(row.session_id, "an anonymous failure with no session id can never be resolved")

    def test_one_anonymous_success_cannot_resolve_everybody_elses_failures(self):
        """The reason the empty session id mattered: it is the same value for every visitor."""
        FormFailure.objects.create(form_name="Example", session_id="", attempt=1)
        FormFailure.objects.create(form_name="Example", session_id="", attempt=1)

        class NoKeySession(dict):
            session_key = None

        session = NoKeySession()
        session[SESSION_KEY] = {"Example": 1}
        good = self._rejected_form()
        Recorder(self._request(session)).form_valid(good)
        self.assertEqual(FormFailure.objects.filter(resolved=True).count(), 0)
