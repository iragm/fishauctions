"""The post-auction survey: answering by link, the free-text box, the admin page, the emails."""

import datetime
from unittest.mock import patch

from django.template import Context, Template
from django.urls import reverse
from django.utils import timezone
from post_office.models import EmailTemplate

from auctions import auction_survey
from auctions.models import Auction, AuctionTOS, Club, ClubMember, Invoice
from auctions.tests import StandardTestCase


def render(source, **context):
    return Template(source).render(Context(context))


class SurveyPageTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.invoiceB.refresh_from_db()
        self.url = reverse("auction_survey", kwargs={"slug": self.online_auction.slug})

    def answer(self, answer, **extra):
        return self.client.post(self.url, {"answer": answer, "uuid": self.invoiceB.no_login_link, **extra})

    def test_opening_an_emailed_button_answers_nothing(self):
        # Mail scanners open every link in a message, both buttons included, and don't run scripts.
        response = self.client.get(self.url, {"answer": "not_fun", "uuid": self.invoiceB.no_login_link})
        self.assertEqual(response.status_code, 200)
        self.tosB.refresh_from_db()
        self.assertEqual(self.tosB.survey_answer, "")
        self.assertEqual(self.tosB.email_address_status, AuctionTOS._meta.get_field("email_address_status").default)

    def test_the_page_sends_the_emailed_answer_from_a_script(self):
        response = self.client.get(self.url, {"answer": "great", "uuid": self.invoiceB.no_login_link})
        self.assertContains(response, 'button[value="great"]')
        self.assertContains(response, 'name="answer" value="great"')
        self.assertNotContains(self.client.get(self.url, {"uuid": self.invoiceB.no_login_link}), "button.click()")
        self.assertNotContains(
            self.client.get(self.url, {"answer": "meh", "uuid": self.invoiceB.no_login_link}), "button.click()"
        )

    def test_the_posted_answer_is_recorded_without_signing_in(self):
        response = self.answer("great")
        self.assertRedirects(
            response,
            auction_survey.survey_url(self.online_auction, token=self.invoiceB.no_login_link),
            fetch_redirect_response=False,
        )
        self.tosB.refresh_from_db()
        self.assertEqual(self.tosB.survey_answer, "great")
        self.assertIsNotNone(self.tosB.survey_answered_on)
        self.assertContains(self.client.get(response.url), "Thanks, got it.")

    def test_answering_from_the_email_verifies_the_address(self):
        AuctionTOS.objects.filter(pk=self.tosB.pk).update(email="b@example.com")
        member = ClubMember.objects.create(club=Club.objects.create(name="Club"), name="B", email="b@example.com")
        self.answer("not_fun")
        self.tosB.refresh_from_db()
        member.refresh_from_db()
        self.assertEqual(self.tosB.email_address_status, "VALID")
        self.assertEqual(member.email_address_status, "VALID")

    def test_changing_your_mind_keeps_the_last_click(self):
        self.answer("great")
        self.answer("not_fun")
        self.tosB.refresh_from_db()
        self.assertEqual(self.tosB.survey_answer, "not_fun")

    def test_an_unknown_answer_is_ignored(self):
        self.answer("meh")
        self.tosB.refresh_from_db()
        self.assertEqual(self.tosB.survey_answer, "")

    def test_a_bad_token_is_404(self):
        response = self.client.get(self.url, {"answer": "great", "uuid": "not-a-token"})
        self.assertEqual(response.status_code, 404)

    def test_another_auctions_token_is_404(self):
        other = reverse("auction_survey", kwargs={"slug": self.in_person_auction.slug})
        response = self.client.get(other, {"uuid": self.invoiceB.no_login_link})
        self.assertEqual(response.status_code, 404)

    def test_signed_out_without_a_token_goes_to_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_signed_in_participant_answers_and_comments(self):
        self.client.force_login(self.userB)
        self.client.post(self.url, {"answer": "great"})
        response = self.client.post(self.url, {"comments": "  More plants next time  "})
        self.assertEqual(response.status_code, 302)
        self.tosB.refresh_from_db()
        self.assertEqual(self.tosB.survey_answer, "great")
        self.assertEqual(self.tosB.survey_comments, "More plants next time")

    def test_comments_through_the_token(self):
        self.client.post(self.url, {"comments": "Fun", "uuid": self.invoiceB.no_login_link})
        self.tosB.refresh_from_db()
        self.assertEqual(self.tosB.survey_comments, "Fun")

    def test_somebody_who_did_not_join_gets_404(self):
        self.client.force_login(self.user_who_does_not_join)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_survey_off_sends_people_to_the_auction(self):
        Auction.objects.filter(pk=self.online_auction.pk).update(post_auction_survey=False)
        response = self.answer("great")
        self.assertEqual(response.status_code, 302)
        self.tosB.refresh_from_db()
        self.assertEqual(self.tosB.survey_answer, "")


class SurveyResultsTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        AuctionTOS.objects.filter(pk=self.tosB.pk).update(
            survey_answer="great", survey_comments="Loved it", survey_answered_on=timezone.now()
        )
        AuctionTOS.objects.filter(pk=self.tosC.pk).update(survey_answer="not_fun", survey_answered_on=timezone.now())
        self.url = reverse("auction_survey_results", kwargs={"slug": self.online_auction.slug})

    def test_admin_sees_free_text(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Loved it")
        self.assertContains(response, "2 gave feedback: 1 great, 1 not so fun")

    def test_participants_cannot(self):
        self.client.force_login(self.userB)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_more_menu_links_it(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("auction_stats", kwargs={"slug": self.online_auction.slug}))
        self.assertContains(response, self.url)

    def test_stat(self):
        self.assertEqual(
            Auction.objects.get(pk=self.online_auction.pk).survey_stats,
            {"answered": 2, "great": 1, "not_fun": 1, "comments": 1},
        )


class SurveyEmailTests(StandardTestCase):
    """In person, the invoice email asks. Online it goes out before pickup, so it never does."""

    def setUp(self):
        super().setUp()
        self.invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.in_person_buyer)
        self.invoiceB.refresh_from_db()
        self.html = EmailTemplate.objects.get(name="invoice_ready", language="").html_content
        self.text = EmailTemplate.objects.get(name="invoice_ready", language="").content

    def test_an_in_person_invoice_email_asks_by_default(self):
        out = render(self.html, invoice=self.invoice, domain="example.com")
        self.assertIn(f"How was {self.in_person_auction}?", out)
        self.assertIn(f"?answer=great&amp;uuid={self.invoice.no_login_link}", out)
        self.assertIn("Not so fun", out)
        text = render(self.text, invoice=self.invoice, domain="example.com")
        self.assertIn(f"?answer=not_fun&uuid={self.invoice.no_login_link}", text)

    def test_an_online_invoice_email_never_asks(self):
        self.assertTrue(self.online_auction.post_auction_survey)
        self.assertNotIn("How was", render(self.html, invoice=self.invoiceB, domain="example.com"))
        self.assertNotIn("How was", render(self.text, invoice=self.invoiceB, domain="example.com"))

    def test_invoice_email_does_not_ask_when_feedback_is_off(self):
        Auction.objects.filter(pk=self.in_person_auction.pk).update(post_auction_survey=False)
        self.invoice.refresh_from_db()
        self.assertNotIn("How was", render(self.html, invoice=self.invoice, domain="example.com"))
        self.assertNotIn("How was", render(self.text, invoice=self.invoice, domain="example.com"))

    def test_invoice_email_does_not_ask_twice(self):
        AuctionTOS.objects.filter(pk=self.in_person_buyer.pk).update(survey_answer="great")
        self.invoice.refresh_from_db()
        self.assertNotIn("How was", render(self.html, invoice=self.invoice, domain="example.com"))

    def test_separate_email_template_renders_the_buttons(self):
        template = EmailTemplate.objects.get(name="auction_survey", language="")
        out = render(template.html_content, invoice=self.invoiceB, auction=self.online_auction, domain="example.com")
        self.assertIn("answer=great", out)


class SendSurveyEmailsTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.user.userdata.is_trusted = True
        self.user.userdata.save()
        past = timezone.now() - datetime.timedelta(days=2)
        self.location.pickup_time = past
        self.location.save()
        Auction.objects.filter(pk=self.online_auction.pk).update(date_end=past)
        AuctionTOS.objects.filter(pk__in=[self.tosB.pk, self.online_tos.pk]).update(email="x@example.com")

    @patch("auctions.auction_survey.mail.send")
    def test_sent_once_to_everyone_with_an_invoice(self, send):
        self.assertEqual(auction_survey.send_survey_emails(), 2)
        self.assertEqual(send.call_args.kwargs["template"], "auction_survey")
        self.assertTrue(Auction.objects.get(pk=self.online_auction.pk).survey_emails_sent)
        self.assertEqual(auction_survey.send_survey_emails(), 0)

    @patch("auctions.auction_survey.mail.send")
    def test_people_who_already_answered_are_skipped(self, send):
        AuctionTOS.objects.filter(pk=self.tosB.pk).update(survey_answer="great")
        self.assertEqual(auction_survey.send_survey_emails(), 1)

    @patch("auctions.auction_survey.mail.send")
    def test_not_before_the_auction_is_pretty_much_over(self, send):
        self.location.pickup_time = timezone.now() - datetime.timedelta(hours=2)
        self.location.save()
        self.assertEqual(auction_survey.send_survey_emails(), 0)
        self.assertFalse(Auction.objects.get(pk=self.online_auction.pk).survey_emails_sent)

    @patch("auctions.auction_survey.mail.send")
    def test_not_for_an_auction_long_over(self, send):
        old = timezone.now() - datetime.timedelta(days=20)
        self.location.pickup_time = old
        self.location.save()
        Auction.objects.filter(pk=self.online_auction.pk).update(date_end=old)
        self.assertEqual(auction_survey.send_survey_emails(), 0)

    @patch("auctions.auction_survey.mail.send")
    def test_not_when_feedback_is_off(self, send):
        Auction.objects.filter(pk=self.online_auction.pk).update(post_auction_survey=False)
        self.assertEqual(auction_survey.send_survey_emails(), 0)

    def test_an_in_person_auction_never_sends_it_separately(self):
        past = timezone.now() - datetime.timedelta(days=2)
        Auction.objects.filter(pk=self.in_person_auction.pk).update(
            date_start=past, lot_submission_end_date=past, online_bidding="disable"
        )
        self.assertNotIn(self.in_person_auction.pk, [auction.pk for auction in auction_survey.due_auctions()])

    @patch("auctions.auction_survey.mail.send")
    def test_not_when_the_auction_emails_nobody_their_invoice(self, send):
        Auction.objects.filter(pk=self.online_auction.pk).update(email_users_when_invoices_ready=False)
        self.assertEqual(auction_survey.send_survey_emails(), 0)

    @patch("auctions.auction_survey.mail.send")
    def test_unsubscribed_people_are_skipped_and_everybody_else_can_unsubscribe(self, send):
        self.userB.userdata.has_unsubscribed = True
        self.userB.userdata.save()
        self.assertEqual(auction_survey.send_survey_emails(), 1)
        self.assertEqual(send.call_args.kwargs["context"]["unsubscribe"], str(self.user.userdata.unsubscribe_link))

    @patch("auctions.auction_survey.mail.send")
    def test_people_without_an_account_are_skipped(self, send):
        AuctionTOS.objects.filter(pk=self.tosB.pk).update(user=None)
        self.assertEqual(auction_survey.send_survey_emails(), 1)

    @patch("auctions.auction_survey.mail.send")
    def test_untrusted_creator_emails_nobody(self, send):
        self.user.userdata.is_trusted = False
        self.user.userdata.save()
        self.assertEqual(auction_survey.send_survey_emails(), 0)
        send.assert_not_called()


class SurveyInThePaletteTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        past = timezone.now() - datetime.timedelta(hours=48)
        self.location.pickup_time = past
        self.location.save()
        Auction.objects.filter(pk=self.online_auction.pk).update(date_end=past)
        self.userB.userdata.last_auction_used = self.online_auction
        self.userB.userdata.save()
        self.client.force_login(self.userB)

    def _titles(self):
        groups = self.client.get(reverse("command_palette")).json()["groups"]
        return [item["title"] for group in groups for item in group["items"]]

    def test_offered_once_the_auction_is_over(self):
        self.assertIn("How was This auction is online?", self._titles())

    def test_not_once_answered(self):
        AuctionTOS.objects.filter(pk=self.tosB.pk).update(survey_answer="great")
        self.assertNotIn("How was This auction is online?", self._titles())

    def test_not_when_the_survey_is_off(self):
        Auction.objects.filter(pk=self.online_auction.pk).update(post_auction_survey=False)
        self.assertNotIn("How was This auction is online?", self._titles())


class SurveyOnTheInvoicePageTests(StandardTestCase):
    """Everyone ends up on their invoice, including app users and people who skip the email."""

    def setUp(self):
        super().setUp()
        self.invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.in_person_buyer)
        Invoice.objects.filter(pk=self.invoice.pk).update(status="UNPAID")
        self.url = reverse("invoice_by_pk", kwargs={"pk": self.invoice.pk})
        self.question = f"How was {self.in_person_auction}?"

    def test_in_person_asks_once_the_invoice_is_ready(self):
        self.client.force_login(self.user_with_no_lots)
        self.assertContains(self.client.get(self.url), self.question)

    def test_not_while_the_invoice_is_open(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(status="DRAFT")
        self.client.force_login(self.user_with_no_lots)
        self.assertNotContains(self.client.get(self.url), self.question)

    def test_not_to_the_admin_looking_at_it(self):
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get(self.url), self.question)

    def test_not_once_answered_or_when_off(self):
        self.client.force_login(self.user_with_no_lots)
        AuctionTOS.objects.filter(pk=self.in_person_buyer.pk).update(survey_answer="great")
        self.assertNotContains(self.client.get(self.url), self.question)
        AuctionTOS.objects.filter(pk=self.in_person_buyer.pk).update(survey_answer="")
        Auction.objects.filter(pk=self.in_person_auction.pk).update(post_auction_survey=False)
        self.assertNotContains(self.client.get(self.url), self.question)

    def test_the_no_login_link_answers_with_its_token(self):
        response = self.client.get(reverse("invoice_no_login", kwargs={"uuid": self.invoice.no_login_link}))
        self.assertContains(response, f'name="uuid" value="{self.invoice.no_login_link}"')
        survey = reverse("auction_survey", kwargs={"slug": self.in_person_auction.slug})
        self.client.post(survey, {"answer": "great", "uuid": self.invoice.no_login_link})
        self.in_person_buyer.refresh_from_db()
        self.assertEqual(self.in_person_buyer.survey_answer, "great")

    def test_online_waits_for_pickup(self):
        self.invoiceB.refresh_from_db()
        url = reverse("invoice_by_pk", kwargs={"pk": self.invoiceB.pk})
        question = f"How was {self.online_auction}?"
        self.client.force_login(self.userB)
        self.assertNotContains(self.client.get(url), question)
        past = timezone.now() - datetime.timedelta(days=2)
        self.location.pickup_time = past
        self.location.save()
        Auction.objects.filter(pk=self.online_auction.pk).update(date_end=past)
        self.assertContains(self.client.get(url), question)
