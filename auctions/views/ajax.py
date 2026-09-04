"""The small endpoints the pages call, rather than the pages themselves.

POST targets, HTMx fragments, and the handful of moderation actions (ban, unban, deactivate a lot).
If a view here renders anything it is a fragment, not a page. ``PageViewCreate`` is the one to know:
it is the write behind every page-view record on the site, and so the busiest endpoint here by a
wide margin.
"""

import logging
import re
from datetime import timedelta
from io import BytesIO

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db.models import (
    Q,
)
from django.db.models.base import Model as Model
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    JsonResponse,
)
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.exceptions import NotAuthenticated
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView
from user_agents import parse
from webpush import send_user_notification
from webpush.models import PushInformation

from auctions.filters import (
    AuctionTOSFilter,
)
from auctions.models import (
    Auction,
    AuctionCampaign,
    AuctionTOS,
    Bid,
    ClubMember,
    Invoice,
    Lot,
    LotImage,
    PageView,
    UserBan,
    UserData,
    UserInterestCategory,
    Watch,
)
from auctions.notifications import CATEGORY_LOT_SELLING, user_has_app_push
from auctions.tasks import (
    cancel_invoice_notification,
    schedule_invoice_notification,
    send_push_to_user,
)

from .base import (
    FEEDBACK_TEXT_MAX_LENGTH,
    INVOICE_NOTIFICATION_DELAY_SECONDS,
    UNASSIGNED_BIDDER_NUMBER_LABEL,
    AuctionViewMixin,
    _ensure_invoice_renewal_state,
    _process_invoice_membership_renewal,
    _sync_tos_alternate_split,
    auctions_available_for_contact_autofill,
    check_club_permission,
)

logger = logging.getLogger(__name__)


class CreateUserBan(APIView):
    """Ban a user - POST only"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        user = request.user
        bannedUser = User.objects.get(pk=pk)
        obj, created = UserBan.objects.update_or_create(
            banned_user=bannedUser,
            user=user,
            defaults={},
        )
        # bans apply to every auction this user administers (matching Auction.user_banned_by_admins),
        # not just auctions they created
        auctionsList = (
            Auction.objects.exclude(is_deleted=True)
            .filter(Q(created_by=user.pk) | Q(auctiontos__user=user, auctiontos__is_admin=True))
            .distinct()
        )
        # delete all bids the banned user has made on active lots or in active auctions this user administers
        bids = (
            Bid.objects.exclude(is_deleted=True)
            .filter(user=bannedUser, lot_number__is_deleted=False)
            .filter(Q(lot_number__user=user.pk) | Q(lot_number__auction__in=auctionsList))
            .select_related("lot_number__auction")
        )
        for bid in bids:
            if not bid.lot_number.ended:
                logger.info("Deleting bid %s", str(bid))
                bid.delete()
        # undo buy now purchases by the banned user in these auctions.  Clear auctiontos_winner
        # along with winner so the sale isn't left half-undone
        buy_now_lots = Lot.objects.exclude(is_deleted=True).filter(winner=bannedUser, auction__in=auctionsList)
        for lot in buy_now_lots:
            lot.winner = None
            lot.auctiontos_winner = None
            lot.winning_price = None
            lot.save()
        # ban all lots added by the banned user.  These are not deleted, just removed from the auction
        lots = Lot.objects.exclude(is_deleted=True).filter(
            Q(user=bannedUser) | Q(auctiontos_seller__user=bannedUser), auction__in=auctionsList
        )
        for lot in lots:
            if not lot.ended:
                logger.info("User %s has banned lot %s", str(user), lot)
                lot.banned = True
                lot.ban_reason = "The seller of this lot has been banned from this auction"
                lot.save()
        return redirect(reverse("userpage", kwargs={"slug": bannedUser.username}))


class LotDeactivate(APIView):
    """Deactivate or activate a lot - POST only"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        lot = Lot.objects.get(pk=pk, is_deleted=False)

        # Check permissions: lot owner or superuser can deactivate
        # Lots in auctions cannot be deactivated
        if lot.auction:
            messages.error(request, "Your account doesn't have permission to view this page")
            return redirect(reverse("home"))

        if not lot.is_owned_by(request.user) and not request.user.is_superuser:
            messages.error(request, "Your account doesn't have permission to view this page")
            return redirect(reverse("home"))

        if lot.deactivated:
            lot.deactivated = False
        else:
            bids = Bid.objects.exclude(is_deleted=True).filter(lot_number=lot.lot_number)
            for bid in bids:
                bid.delete()
            lot.deactivated = True
        lot.save()
        return HttpResponse("success")


class UserUnban(APIView):
    """Unban a user - POST only"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        user = request.user
        bannedUser = User.objects.get(pk=pk)
        obj, created = UserBan.objects.update_or_create(
            banned_user=bannedUser,
            user=user,
            defaults={},
        )
        obj.delete()
        return redirect(reverse("userpage", kwargs={"slug": bannedUser.username}))


class ImagesPrimary(APIView):
    """Make the specified image the default image for the lot
    Takes pk of image as post param
    this does not check lot.can_add_images, which is deliberate (who cares if you rotate...)
    """

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            lotImage = LotImage.objects.get(pk=int(request.POST["pk"]))
        except (LotImage.DoesNotExist, ValueError, KeyError):
            return HttpResponse("Image not found, specify a valid pk")
        if not lotImage.lot_number.image_permission_check(request.user):
            messages.error(request, "Only the lot creator can change images")
            return redirect(reverse("home"))
        LotImage.objects.filter(lot_number=lotImage.lot_number.pk).update(is_primary=False)
        lotImage.is_primary = True
        lotImage.save()
        return HttpResponse("Success")


class ImagesRotate(APIView):
    """Rotate an image associated with a lot
    Takes pk of image and angle as post params
    """

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            pk = int(request.POST["pk"])
            angle = int(request.POST["angle"])
        except (KeyError, ValueError):
            return HttpResponse("user, pk, and angle are required")
        try:
            lotImage = LotImage.objects.get(pk=pk)
        except LotImage.DoesNotExist:
            return HttpResponse(f"Image {pk} not found")
        if not lotImage.lot_number.image_permission_check(request.user):
            messages.error(request, "Only the lot creator can rotate images")
            return redirect(reverse("home"))
        if not lotImage.image:
            return HttpResponse("No image")
        temp_image = Image.open(BytesIO(lotImage.image.read()))
        temp_image = temp_image.rotate(angle, expand=True)
        if temp_image.mode in ("RGBA", "P"):
            temp_image = temp_image.convert("RGB")
        output = BytesIO()
        temp_image.save(output, format="JPEG", quality=85)
        output.seek(0)
        # Overwrite the original image
        lotImage.image.save(
            lotImage.image.name.replace("images/", ""),
            ContentFile(output.read()),
            save=True,
        )
        return HttpResponse("Success")


class Feedback(APIView):
    """Leave feedback on a lot
    This can be done as a buyer or a seller
    api/feedback/lot_number/buyer
    api/feedback/lot_number/seller
    """

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, pk, leave_as):
        data = request.POST
        try:
            lot = Lot.objects.get(pk=pk, is_deleted=False)
        except Lot.DoesNotExist:
            msg = f"No lot found with key {pk}"
            raise Http404(msg)
        winner_checks_pass = False
        seller_checks_pass = False
        if leave_as == "winner":
            if lot.winner:
                if lot.winner.pk == request.user.pk:
                    winner_checks_pass = True
            if lot.auctiontos_winner:
                if lot.auctiontos_winner.user:
                    if (lot.auctiontos_winner.user.pk == request.user.pk) or (
                        lot.auctiontos_winner.email == request.user.email
                    ):
                        winner_checks_pass = True
        if winner_checks_pass:
            rating = data.get("rating")
            if rating:
                lot.feedback_rating = rating
                lot.save()
            text = data.get("text")
            if text:
                # Truncate text to max length to prevent database errors
                lot.feedback_text = text[:FEEDBACK_TEXT_MAX_LENGTH]
                lot.save()
        if leave_as == "seller" and lot.is_owned_by(request.user):
            seller_checks_pass = True
        if seller_checks_pass:
            rating = data.get("rating")
            if rating:
                lot.winner_feedback_rating = rating
                lot.save()
            text = data.get("text")
            if text:
                # Truncate text to max length to prevent database errors
                lot.winner_feedback_text = text[:FEEDBACK_TEXT_MAX_LENGTH]
                lot.save()
        if not winner_checks_pass and not seller_checks_pass:
            messages.error(request, "Only the seller or winner of a lot can leave feedback")
            return redirect(reverse("home"))
        return HttpResponse("Success")


def clean_referrer(url):
    """Make a URL more human readable"""
    if not url:
        url = ""
    url = re.sub(r"^https?://", "", url)  # no http/s at the beginning
    if Site.objects.get_current().domain not in url:
        url = re.sub(r"\?.*", "", url)  # remove get params
    url = re.sub(r"^www\.", "", url)  # www
    url = re.sub(r"/+$", "", url)  # trailing /
    # if someone has facebook.example.com, it would be recorded as FB...
    # can update this if it becomes an issue
    if re.search(r"(facebook)\.", url):
        url = "Facebook"
    if re.search(r"(google)\.", url):
        url = "Google"
    return url


class PageViewCreate(APIView):
    """Record page views"""

    authentication_classes = [SessionAuthentication]
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.POST
        auction = data.get("auction", None)
        if auction:
            auction = Auction.objects.filter(pk=auction).first()
        lot_number = data.get("lot", None)
        if lot_number:
            lot_number = Lot.objects.filter(pk=lot_number, is_deleted=False).first()
        url = data.get("url", None)
        url_without_params = re.sub(r"\?.*", "", url)
        url_without_params = url_without_params[:600]
        first_view = data.get("first_view", False)
        if request.user.is_authenticated:
            user = request.user
            session_id = None
        else:
            # anonymous users go by session
            user = None
            # saving the session will force key generation
            if not request.session.session_key:
                request.session.save()
            session_id = request.session.session_key
        if first_view == "true":  # good ol Javascript
            user_agent = request.META.get("HTTP_USER_AGENT", "")
            # platform = 'UNKNOWN'
            os = "UNKNOWN"
            parsed_ua = parse(user_agent)
            # if parsed_ua.is_mobile:
            #     platform = 'MOBILE'
            # if parsed_ua.is_tablet:
            #     platform = 'TABLET'
            # elif parsed_ua.is_pc:
            #     platform = 'DESKTOP'
            user_agent = user_agent[:200]
            referrer = clean_referrer(data.get("referrer", None)[:600])
            source = data.get("src", None)
            uid = data.get("uid", None)
            # mark auction campaign results if applicable present
            ip = ""
            x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
            if x_forwarded_for:
                ip = x_forwarded_for.split(",")[0]
            else:
                ip = request.META.get("REMOTE_ADDR")
            if uid:  # and not request.user.is_authenticated:
                userdata = UserData.objects.filter(unsubscribe_link=uid).first()
                if userdata:
                    userdata.last_activity = timezone.now()
                    userdata.save()
            if source:
                campaign = AuctionCampaign.objects.filter(uuid=source).first()
                if campaign and campaign.result == "NONE":
                    campaign.result = "VIEWED"
                    campaign.save()
                if campaign and campaign.user is not None and campaign.auction is not None:
                    tos = AuctionTOS.objects.filter(user=campaign.user, auction=campaign.auction).first()
                    if tos:
                        campaign.result = "JOINED"
                        campaign.save()
            if "Googlebot" not in user_agent and "Baiduspider" not in user_agent:
                PageView.objects.create(
                    lot_number=lot_number,
                    url=url_without_params,
                    auction=auction,
                    session_id=session_id,
                    user=user,
                    user_agent=user_agent,
                    ip_address=ip[:100],
                    platform=parsed_ua.os.family,
                    os=os,
                    referrer=referrer[:600],
                    title=data.get("title", "")[:600],
                    source=source,
                )
                if user:
                    UserData.objects.filter(user=user).update(last_activity=timezone.now())
            if user and lot_number and lot_number.species_category:
                # create/increment interest in this category for this view
                UserInterestCategory.add_interest(user, lot_number.species_category, settings.VIEW_WEIGHT)
            if auction and user:
                if not source:
                    source = referrer
                try:
                    campaign = AuctionCampaign.objects.create(
                        auction=auction,
                        user=user,
                        email=user.email,
                        source=source[:200],
                    )
                except ValidationError:
                    # campaign already exists
                    pass
        # code below would run on subsequent pageviews.  Not worth the extra server effort for an update every 10 seconds.
        # some corresponding js on base_page_view.html is also commented out
        # else:
        #     pageview = PageView.objects.filter(
        #         url = url_without_params,
        #         session_id = session_id,
        #         user = user,
        #     ).order_by('-date_start').first()
        #     if pageview:
        #         # this is the second (or more) time this user has viewed this page
        #         pageview.total_time += 10
        #         pageview.date_end = timezone.now()
        #         pageview.save()
        return HttpResponse("Success")


class InvoicePaid(APIView):
    """Mark an invoice as paid/ready/open - POST only

    Restricted to authenticated auction admins (or club admins for renewal-only invoices).
    The status change books/reverses club-ledger (ClubMoney) entries and can trigger a
    membership renewal, so it must never be reachable via the invoice's no-login UUID:
    that link is emailed to the bidder, who could otherwise mark their own invoice PAID.
    UUID access to an invoice is view-only and handled separately by ``InvoiceNoLoginView``.
    """

    authentication_classes = [TokenAuthentication, SessionAuthentication]
    permission_classes = [AllowAny]  # Auth is enforced manually in post()

    def post(self, request, *args, **kwargs):
        # Only accept statuses that are real choices on the model. An unvalidated status
        # (e.g. "BANANA") would be written straight to the DB and silently break every
        # `status == "PAID"` / `status in ("DRAFT", "UNPAID")` check across the codebase.
        new_status = kwargs["status"]
        valid_statuses = {value for value, _label in Invoice._meta.get_field("status").choices}
        if new_status not in valid_statuses:
            msg = "Invalid invoice status"
            raise Http404(msg)
        # Changing invoice status is an admin-only action (see class docstring).
        if not request.user.is_authenticated:
            raise NotAuthenticated()
        invoice = get_object_or_404(Invoice, pk=kwargs["pk"])
        auction = invoice.auction
        if auction:
            if not auction.permission_check(request.user):
                raise PermissionDenied()
        elif invoice.club:
            # Renewal-only invoice (no auction): check club permission
            if not check_club_permission(request.user, invoice.club, "permission_add_edit"):
                raise PermissionDenied()
        else:
            raise PermissionDenied()
        if new_status in ("PAID", "UNPAID") and not invoice.renewal_needed:
            _ensure_invoice_renewal_state(invoice)
        # Core: persist the new invoice status. Everything else is "extra"
        # and must not be allowed to block the status change.
        invoice.status = new_status
        run_at = None
        if new_status in ("UNPAID", "PAID"):
            run_at = timezone.now() + timedelta(seconds=INVOICE_NOTIFICATION_DELAY_SECONDS)
            invoice.invoice_notification_due = run_at
        elif new_status == "DRAFT":
            invoice.invoice_notification_due = None
        invoice.save()
        try:
            if run_at:
                schedule_invoice_notification(invoice.pk, run_at)
            elif new_status == "DRAFT":
                cancel_invoice_notification(invoice.pk)
        except Exception:
            logger.exception("schedule/cancel invoice notification failed for invoice %s", invoice.pk)
        if new_status == "PAID":
            try:
                _process_invoice_membership_renewal(
                    invoice, acting_user=request.user if request.user.is_authenticated else None
                )
            except Exception:
                logger.exception("invoice membership renewal failed for invoice %s", invoice.pk)
            try:
                buyer_tos = getattr(invoice, "auctiontos_user", None)
                if buyer_tos and buyer_tos.clubmember_id:
                    buyer_tos.clubmember.update_last_club_activity()
            except Exception:
                logger.exception("last_club_activity update failed for invoice %s buyer", invoice.pk)
        user = request.user if request.user.is_authenticated else None
        # Club-only renewal invoices have no auction (and no auctiontos_user); skip the
        # auction history entry rather than raising/logging an AttributeError every time.
        if auction and invoice.auctiontos_user:
            try:
                auction.create_history(
                    applies_to="INVOICES",
                    action=f"Set invoice for {invoice.auctiontos_user.name} to {invoice.get_status_display()}",
                    user=user,
                )
            except Exception:
                logger.exception("create_history failed for invoice %s", invoice.pk)
        is_admin = True  # This endpoint is now admin-only (no UUID/member-facing access)
        buttons_html = render_to_string("invoice_buttons.html", {"invoice": invoice})
        renewal_ctx = {"invoice": invoice, "is_admin": is_admin}
        renewal_html = render_to_string("auctions/partials/invoice_membership_renewal.html", renewal_ctx)
        # Include the renewal section as an OOB swap so the locked/unlocked visual
        # state and the "already processed" warning reflect the new invoice state
        # immediately without requiring a page reload.
        renewal_oob = ""
        if 'id="invoice-membership-renewal"' in renewal_html:
            renewal_oob = renewal_html.replace(
                '<div id="invoice-membership-renewal"',
                '<div hx-swap-oob="outerHTML" id="invoice-membership-renewal"',
                1,
            )
        return HttpResponse(buttons_html + renewal_oob, status=200)


class APIPostView(APIView):
    """POST only method to do stuff, logged in users only"""

    authentication_classes = [TokenAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        raise NotImplementedError()


class InvoiceRenewalNeededToggleView(APIView):
    authentication_classes = [TokenAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        invoice = get_object_or_404(Invoice, pk=pk)
        if invoice.auction:
            if not invoice.auction.permission_check(request.user):
                raise PermissionDenied()
        elif invoice.club:
            if not check_club_permission(request.user, invoice.club, "permission_add_edit"):
                raise PermissionDenied()
        else:
            raise PermissionDenied()
        if invoice.renewal_processed:
            return HttpResponseBadRequest("Renewal already processed for this invoice.")
        renewal_needed = str(request.POST.get("renewal_needed", "")).lower() in ("1", "true", "on", "yes")
        invoice.renewal_needed = renewal_needed
        invoice.renewal_manually_set = True
        invoice.save(update_fields=["renewal_needed", "renewal_manually_set"])
        # Checking the box makes the user a club member for this invoice: apply the club
        # member discount and (in club member discount split mode) the alternate split.
        _sync_tos_alternate_split(invoice.auctiontos_user, invoice)
        invoice.recalculate()
        ctx = {"invoice": invoice, "is_admin": True, "csrf_token": get_token(request)}
        body = render_to_string("auctions/partials/invoice_membership_renewal.html", ctx, request=request)
        # OOB swaps so the invoice fee row, discount row, tax row, final total, and quick-checkout
        # summary all update in real time when the box is toggled.
        fee_row = render_to_string("auctions/partials/invoice_membership_fee_row.html", ctx, request=request)
        discount_row = render_to_string("auctions/partials/invoice_club_member_discount_row.html", ctx, request=request)
        tax_row = render_to_string("auctions/partials/invoice_tax_row.html", ctx, request=request)
        total_row = render_to_string("auctions/partials/invoice_final_total_row.html", ctx, request=request)
        oob_fee = fee_row.replace("<tr id=", '<tr hx-swap-oob="outerHTML" id=', 1)
        oob_discount = discount_row.replace("<tr id=", '<tr hx-swap-oob="outerHTML" id=', 1)
        oob_tax = tax_row.replace("<tr id=", '<tr hx-swap-oob="outerHTML" id=', 1)
        oob_total = total_row.replace("<tr id=", '<tr hx-swap-oob="outerHTML" id=', 1)
        # Wrap <tr> OOB swaps in <table> so the browser's HTML parser does not discard
        # them when they appear outside a table context, while still letting htmx find
        # and process the hx-swap-oob attribute (unlike <template>, whose content is
        # inert and not reachable by querySelectorAll).
        oob_fee = f"<table>{oob_fee}</table>"
        oob_discount = f"<table>{oob_discount}</table>"
        oob_tax = f"<table>{oob_tax}</table>"
        oob_total = f"<table>{oob_total}</table>"
        oob_summary_checkout = (
            f'<span id="quick-checkout-invoice-summary" hx-swap-oob="outerHTML">{invoice.invoice_summary_short}</span>'
        )
        # Also update the invoice-summary-short span on the full invoice page (invoice.html)
        oob_summary_invoice = (
            f'<span id="invoice-summary-short" hx-swap-oob="outerHTML">{invoice.invoice_summary_short}</span>'
        )
        # Update the modal title (generic_admin_form.html) when the renewal checkbox is toggled
        # while the auctiontos/clubmember admin modal is open.
        modal_name = invoice.invoice_summary
        oob_modal_title = f'<h5 class="modal-title" id="modal-invoice-title" hx-swap-oob="outerHTML">{modal_name}</h5>'
        response = HttpResponse(
            body
            + oob_fee
            + oob_discount
            + oob_tax
            + oob_total
            + oob_summary_checkout
            + oob_summary_invoice
            + oob_modal_title
        )
        # Signal the quick-checkout page to regenerate QR codes now that the total has changed.
        response["HX-Trigger"] = "renewalToggled"
        return response


class UpdateLotPushNotificationsView(APIPostView):
    def post(self, request, *args, **kwargs):
        userdata = request.user.userdata
        userdata.push_notifications_when_lots_sell = True
        userdata.save()
        return JsonResponse({"result": "success"})


class LotPushTestNotificationView(APIPostView):
    def post(self, request, *args, **kwargs):
        lot = get_object_or_404(Lot, pk=kwargs["pk"], is_deleted=False)
        if not Watch.objects.filter(lot_number=lot, user=request.user).exists():
            return JsonResponse({"result": "error", "message": "You must watch this lot first."}, status=403)
        # Test the channel the real notification will actually use, otherwise an app user's test
        # would go to a browser they aren't looking at (or fail) while the real one goes to the app.
        if user_has_app_push(request.user):
            send_push_to_user.delay(
                request.user.pk,
                title=f"{lot.lot_name} test notification",
                body=f"Lot {lot.lot_number_display} test notification for this watched lot.",
                url=f"https://{lot.full_lot_link}",
                category=CATEGORY_LOT_SELLING,
                collapse_key=f"lot_sell_notification_test_{lot.pk}",
                auction_pk=lot.auction_id,
            )
            return JsonResponse({"result": "success"})
        if not PushInformation.objects.filter(user=request.user).exists():
            return JsonResponse({"result": "error", "message": "No push subscription found."}, status=400)

        payload = {
            "head": f"{lot.lot_name} test notification",
            "body": f"Lot {lot.lot_number_display} test notification for this watched lot.",
            "url": f"https://{lot.full_lot_link}",
            "tag": f"lot_sell_notification_test_{lot.pk}",
        }
        if lot.thumbnail:
            payload["icon"] = lot.thumbnail.display_url
        send_user_notification(user=request.user, payload=payload, ttl=10000)
        return JsonResponse({"result": "success"})


class CheckUsernameAvailability(APIView):
    """GET /check-username/?username=foo — returns JSON for real-time signup validation.
    No authentication required (used on the public signup form).
    """

    authentication_classes = [SessionAuthentication]
    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        username = request.GET.get("username", "").strip()
        if not username:
            return JsonResponse({"available": False, "error": "No username provided"})
        taken = User.objects.filter(username__iexact=username).exists()
        return JsonResponse({"available": not taken})


class AuctionTOSValidation(AuctionViewMixin, APIPostView):
    """For real time validation on the auctiontos admin create form
    See views.AuctionTOSAdmin for the corresponding js and view
    """

    def post(self, request, *args, **kwargs):
        pk = request.POST.get("pk", None)
        try:
            pk = int(pk) if pk is not None else None
        except ValueError:
            pk = None
        name = request.POST.get("name", None)
        bidder_number = request.POST.get("bidder_number", None)
        email = request.POST.get("email", None)
        # note: be careful what you dump in result
        # javascript will fill out any id on the form with this info
        result = {
            "id_bidder_number": "",
            "id_name": "",
            "id_email": "",
            "id_address": "",
            "id_is_club_member": "",
            "id_phone_number": "",
            "id_memo": "",
            "name_tooltip": "",
            "bidder_number_tooltip": "",
            "email_tooltip": "",
        }
        base_qs = self.auction.tos_qs
        if pk:
            base_qs = base_qs.exclude(pk=pk)
        if name and not email and not pk:
            old_auctions = auctions_available_for_contact_autofill(
                self.request.user, extra_created_by=self.auction.created_by
            )
            qs = AuctionTOS.objects.filter(auction__in=old_auctions, email__isnull=False).order_by("-createdon")
            old_tos = AuctionTOSFilter.generic(self, qs, name, match_names_only=True).first()
            if old_tos:
                result["id_name"] = old_tos.name
                result["id_email"] = old_tos.email
                result["id_address"] = old_tos.address
                result["id_is_club_member"] = old_tos.is_club_member
                result["id_phone_number"] = old_tos.phone_number
                result["id_memo"] = old_tos.memo
            else:
                logger.info("no user found in older auctions with name %s", name)
        if name:
            existing_tos_in_this_auction = AuctionTOSFilter.generic(self, base_qs, name, match_names_only=True).first()
            if existing_tos_in_this_auction:
                existing_bidder_number = existing_tos_in_this_auction.bidder_number or UNASSIGNED_BIDDER_NUMBER_LABEL
                result["name_tooltip"] = (
                    f"There's already a user in this auction named {existing_tos_in_this_auction.name} "
                    f"(bidder number: {existing_bidder_number})"
                )
            else:
                logger.info("no user found in older auctions with name %s", name)
        if email:
            existing_tos_in_this_auction = base_qs.filter(email=email).first()
            if existing_tos_in_this_auction:
                result["email_tooltip"] = "Email is already in this auction"
            else:
                logger.info("no user found in this auction with email %s", email)
        if bidder_number:
            existing_tos_in_this_auction = base_qs.filter(bidder_number=bidder_number).first()
            if existing_tos_in_this_auction:
                result["bidder_number_tooltip"] = "Bidder number in use"
            elif self.auction.is_club_managed:
                clash = ClubMember.objects.filter(
                    club=self.auction.club, bidder_number=bidder_number, is_deleted=False
                ).first()
                if clash:
                    result["bidder_number_tooltip"] = f"Bidder number in use by {clash.name}"
            else:
                logger.info("no user found in this auction with email %s", email)
        return JsonResponse(result)
