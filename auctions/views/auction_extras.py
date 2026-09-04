"""The rest of an auction's admin surface: label config, bulk printing, no-shows, chat.

Smaller pages that did not belong with the check-in screens or the stats: the label field picker,
the bulk print sheets, pickup-location manifests, the add-to-calendar link, and the no-show
actions.
"""

import ast
import csv
import logging
import uuid
from datetime import timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.db.models import (
    Count,
    ExpressionWrapper,
    F,
    FloatField,
    Q,
)
from django.db.models.base import Model as Model
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView, View
from django.views.generic.edit import (
    FormMixin,
    FormView,
)
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from auctions.forms import (
    AuctionNoShowForm,
    LabelPrintFieldsForm,
    MultiAuctionTOSPrintLabelForm,
)
from auctions.models import (
    Auction,
    AuctionDropdown,
    AuctionTOS,
    ChatSubscription,
    ClubMember,
    Lot,
    PickupLocation,
    UserBan,
    add_price_info,
    guess_category,
)

from .base import AuctionViewMixin, close_modal_response
from .printing import LotLabelView

logger = logging.getLogger(__name__)


class AuctionLabelConfig(LoginRequiredMixin, AuctionViewMixin, FormView):
    form_class = LabelPrintFieldsForm
    template_name = "auction_print_setup.html"

    def get_success_url(self):
        return reverse("auction_printing", kwargs={"slug": self.auction.slug})

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["auction"] = self.auction
        return kwargs

    def form_valid(self, form):
        form.save()
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        return context


class AuctionBulkPrinting(AuctionViewMixin, FormView):
    model = Auction
    template_name = "auction_printing.html"
    form_class = MultiAuctionTOSPrintLabelForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["all_labels_count"] = self.auction.labels_qs.count()
        context["unprinted_label_count"] = self.auction.unprinted_labels_qs.count()
        context["printed_labels_count"] = context["all_labels_count"] - context["unprinted_label_count"]
        context["auction"] = self.auction
        return context

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["auctiontos"] = (
            AuctionTOS.objects.filter(auction=self.auction)
            .annotate(
                lots_count=Count(
                    "auctiontos_seller",
                    filter=Q(
                        auctiontos_seller__banned=False,
                        auctiontos_seller__is_deleted=False,
                    ),
                ),
                unprinted_labels_count=Count(
                    "auctiontos_seller",
                    filter=Q(
                        auctiontos_seller__banned=False,
                        auctiontos_seller__label_printed=False,
                        auctiontos_seller__is_deleted=False,
                    ),
                ),
            )
            .filter(lots_count__gt=0)
        )
        return kwargs

    def form_valid(self, form):
        print_only_unprinted = form.cleaned_data["print_only_unprinted"]
        selected_tos = []
        for key, value in form.cleaned_data.items():
            if key.startswith("tos_") and value:
                pk = key.split("_")[1]
                selected_tos.append(pk)
        data = {
            "selected_tos": selected_tos,
            "print_only_unprinted": print_only_unprinted,
        }
        url = reverse("auction_printing_pdf", kwargs={"slug": self.auction.slug})
        url_with_params = f"{url}?{urlencode(data)}"
        return HttpResponseRedirect(url_with_params)


class AuctionBulkPrintingPDF(LotLabelView):
    """Admin page to print labels for multiple users at once"""

    allow_non_admins = False

    def get_queryset(self):
        return self.queryset

    def dispatch(self, request, *args, **kwargs):
        self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=kwargs["slug"]).first()
        self.is_auction_admin

        self.selected_tos = request.GET.get("selected_tos", None)
        self.print_only_unprinted = request.GET.get("print_only_unprinted", "True") == "True"
        if not self.selected_tos:
            self.queryset = self.auction.unprinted_labels_qs
        else:
            # selected_tos is a client-supplied string like "[1, 2, 3]" (AuctionTOS pks). Parse it
            # defensively: malformed input, a non-list literal, or non-integer elements must not 500.
            try:
                parsed = ast.literal_eval(self.selected_tos)
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                parsed = None
            if not isinstance(parsed, (list, tuple, set)):
                parsed = []
            cleaned = []
            for pk in parsed:
                try:
                    cleaned.append(int(pk))
                except (TypeError, ValueError):
                    continue
            self.selected_tos = cleaned
            if self.print_only_unprinted:
                self.queryset = self.auction.unprinted_labels_qs
            else:
                self.queryset = self.auction.lots_qs
            self.queryset = self.queryset.filter(auctiontos_seller__pk__in=self.selected_tos)
        if not self.get_queryset():
            if not self.selected_tos:
                messages.error(request, "No users selected")
            else:
                messages.error(request, "Couldn't find any labels to print")
            return redirect(reverse("auction_printing", kwargs={"slug": self.auction.slug}))
        if request.method.lower() in self.http_method_names:
            handler = getattr(self, request.method.lower(), self.http_method_not_allowed)
        else:
            handler = self.http_method_not_allowed
        return handler(request, *args, **kwargs)


def _lots_with_people(lots):
    """A lot queryset that already knows its seller and winner, and where each of them collects.

    Both location CSVs print those for every row, and reading them one lot at a time was four
    queries a row: the AuctionTOS, its pickup location, and the auction that
    ``AuctionTOS.display_name`` consults to decide between a name and a bidder number.
    """
    return lots.select_related(
        "auction",
        "auctiontos_seller__pickup_location",
        "auctiontos_seller__auction",
        "auctiontos_seller__user__userdata",
        "auctiontos_winner__pickup_location",
        "auctiontos_winner__auction",
        "auctiontos_winner__user__userdata",
    )


class PickupLocationsIncoming(View, AuctionViewMixin):
    """All lots destined for this location"""

    def dispatch(self, request, *args, **kwargs):
        self.location = PickupLocation.objects.filter(pk=kwargs.pop("pk")).first()
        if self.location:
            self.auction = self.location.auction
            self.is_auction_admin
            return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        # each row prints the winner, the seller and where the lot is coming from
        queryset = _lots_with_people(self.location.incoming_lots).order_by("-auctiontos_seller__name")
        response = HttpResponse(content_type="text/csv")
        name = self.location.name.lower().replace(" ", "_")
        response["Content-Disposition"] = f'attachment; filename="incoming_lots_destined_for_{name}.csv"'
        csv_writer = csv.writer(response)
        csv_writer.writerow(
            [
                "Lot number",
                "Lot name",
                "Winner name",
                "Origin",
                "Seller name",
            ]
        )
        for lot in queryset:
            csv_writer.writerow(
                [
                    lot.lot_number_display,
                    lot.lot_name,
                    lot.winner_name,
                    lot.location,
                    lot.seller_name,
                ]
            )
        self.auction.create_history(applies_to="LOTS", action="CSV download of incoming lots", user=request.user)
        return response


class PickupLocationsOutgoing(View, AuctionViewMixin):
    """CSV of all lots coming from this location"""

    def dispatch(self, request, *args, **kwargs):
        self.location = PickupLocation.objects.filter(pk=kwargs.pop("pk")).first()
        if self.location:
            self.auction = self.location.auction
            self.is_auction_admin
            return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        # each row prints the winner, the seller and where the lot is going
        queryset = _lots_with_people(self.location.outgoing_lots).order_by("-auctiontos_winner__pickup_location__name")
        response = HttpResponse(content_type="text/csv")
        name = self.location.name.lower().replace(" ", "_")
        response["Content-Disposition"] = f'attachment; filename="outgoing_lots_coming_from_{name}.csv"'
        csv_writer = csv.writer(response)
        csv_writer.writerow(["Lot number", "Seller name", "Lot name", "Destination", "Winner name"])
        for lot in queryset:
            csv_writer.writerow(
                [
                    lot.lot_number_display,
                    lot.seller_name,
                    lot.lot_name,
                    lot.winner_location,
                    lot.winner_name,
                ]
            )
        self.auction.create_history(applies_to="LOTS", action="CSV download of outgoing lots", user=request.user)
        return response


class AddToCalendarView(LoginRequiredMixin, View):
    """Redirect or generate an 'Add to Calendar' link for a pickup location"""

    def dispatch(self, request, *args, **kwargs):
        # Extract query params
        self.calendar_type = request.GET.get("type")
        self.second = request.GET.get("second") in ("1", "true", "yes", "True")
        self.location_pk = request.GET.get("location")

        # Validate location exists
        self.location = get_object_or_404(PickupLocation, pk=self.location_pk)
        self.auction = self.location.auction

        if self.second and not self.location.second_pickup_time:
            messages.error(
                request,
                "This location does not have a second pickup time",
            )
            return redirect(self.auction.get_absolute_url())

        if not self.second and not self.location.pickup_time:
            messages.error(
                request,
                "This location does not have a pickup time",
            )
            return redirect(self.auction.get_absolute_url())

        # Confirm user has joined this auction
        self.tos = AuctionTOS.objects.filter(
            auction=self.location.auction,
            user=request.user,
        ).first()

        if not self.tos:
            messages.error(
                request,
                "You haven't joined this auction yet",
            )
            return redirect(self.auction.get_absolute_url())

        # if self.tos.pickup_location.pk is not self.location.pk:
        #     messages.error(
        #         request,
        #         "You can't add a location to your calendar unless you've selected it",
        #     )
        #     return redirect(self.auction.get_absolute_url())

        # "native" returns the event as JSON for the mobile app's native "add to device calendar"
        # bridge; "google"/"outlook" redirect to web calendars; "ics" downloads an .ics file.
        if self.calendar_type not in ("google", "outlook", "ics", "native"):
            messages.error(
                request,
                "Unknown calendar type requested",
            )
            return redirect(self.auction.get_absolute_url())

        self.tos.add_to_calendar = self.calendar_type
        self.tos.save()

        return super().dispatch(request, *args, **kwargs)

    def _build_event(self):
        """Return the shared event fields (title, details, start, end, location) for this pickup."""
        start = self.location.second_pickup_time if self.second else self.location.pickup_time
        if not start:
            msg = "Pickup time not available"
            raise Http404(msg)

        # Convert to UTC and define end
        end = start + timedelta(hours=1)

        # Build common event info
        title = f"{self.location.auction.title}"
        if self.second:
            title += " – second pickup"

        details = f"{self.location.auction.title}\n{self.location.description or ''}".strip()
        loc = self.location.address or f"{self.location.latitude},{self.location.longitude}"
        return title, details, start, end, loc

    def get(self, request, *args, **kwargs):
        """Handle GET: redirect user, return ICS, or return event JSON for the native app."""

        title, details, start, end, loc = self._build_event()

        if self.calendar_type == "native":
            # Consumed by the mobile app's addToCalendar JS bridge (see location_fragment_short.html),
            # which hands these fields to a native "add to device calendar" plugin.
            return JsonResponse(
                {
                    "title": title,
                    "details": details,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "location": loc,
                }
            )

        if self.calendar_type == "google":
            params = {
                "action": "TEMPLATE",
                "text": title,
                "dates": f"{start.strftime('%Y%m%dT%H%M%SZ')}/{end.strftime('%Y%m%dT%H%M%SZ')}",
                "details": details,
                "location": loc,
            }
            url = f"https://calendar.google.com/calendar/render?{urlencode(params)}"
            return redirect(url)

        elif self.calendar_type == "outlook":
            # Outlook supports ISO 8601 with UTC Z
            params = {
                "subject": title,
                "body": details,
                "startdt": start.isoformat().replace("+00:00", "Z"),
                "enddt": end.isoformat().replace("+00:00", "Z"),
                "location": loc,
            }
            url = f"https://outlook.live.com/calendar/0/deeplink/compose?{urlencode(params)}"
            return redirect(url)

        else:
            ics_content = self._generate_ics(title, details, start, end, loc)
            filename = f"{self.location.auction.slug}.ics"
            response = HttpResponse(ics_content, content_type="text/calendar")
            response["Content-Disposition"] = f'attachment; filename="{filename}"'
            return response

    def _generate_ics(self, title, description, start, end, location):
        """Return a valid ICS file string (UTC-based, RFC5545 compliant)"""
        uid = uuid.uuid4()
        now_utc = timezone.now()
        escaped_description = description.replace("\n", "\\n")
        return (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//YourSite//Auction Pickup//EN\r\n"
            "CALSCALE:GREGORIAN\r\n"
            "METHOD:PUBLISH\r\n"
            "BEGIN:VEVENT\r\n"
            f"UID:{uid}@yourdomain.com\r\n"
            f"DTSTAMP:{now_utc.strftime('%Y%m%dT%H%M%SZ')}\r\n"
            f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}\r\n"
            f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}\r\n"
            f"SUMMARY:{title}\r\n"
            f"DESCRIPTION:{escaped_description}\r\n"
            f"LOCATION:{location}\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )


class CategoryFinder(APIView):
    """API view which will return a category (or none) based on POST keyword lot_name"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        lot_name = request.POST["lot_name"]
        result = guess_category(lot_name)
        if result:
            result = {"name": result.name, "value": result.pk}
        else:
            result = {"value": None}
        return JsonResponse(result)


class AuctionFinder(APIView):
    """API view which will return information about an auction based on POST keyword auction.  Expects a pk."""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        return redirect(reverse("home"))

    def post(self, request, *args, **kwargs):
        try:
            self.auction = Auction.objects.filter(pk=request.POST["auction"]).first()
        except ValueError:
            self.auction = None
        if not self.auction or not AuctionTOS.objects.filter(user=request.user, auction=self.auction):
            # you don't get to query auctions you haven't joined
            result = {}
        else:
            result = {
                "use_categories": self.auction.use_categories,
                "reserve_price": self.auction.reserve_price,
                "buy_now": self.auction.buy_now,
                "use_quantity_field": self.auction.use_quantity_field,
                "custom_checkbox_name": self.auction.custom_checkbox_name,
                "custom_field_1": self.auction.custom_field_1,
                "custom_field_1_name": self.auction.custom_field_1_name,
                "use_donation_field": self.auction.use_donation_field,
                "use_i_bred_this_fish_field": self.auction.use_i_bred_this_fish_field,
                "use_custom_checkbox_field": self.auction.use_custom_checkbox_field,
                "use_custom_dropdown_field": self.auction.use_custom_dropdown_field,
                "custom_dropdown_required": self.auction.use_custom_dropdown_field == "required",
                "custom_dropdown_name": self.auction.custom_dropdown_name,
                "custom_dropdown_options": list(
                    AuctionDropdown.objects.filter(auction=self.auction)
                    .order_by("createdon")
                    .values_list("value", flat=True)
                ),
                "use_reference_link": self.auction.use_reference_link,
                "use_description": self.auction.use_description,
                "use_scientific_name": self.auction.use_scientific_name,
            }
        return JsonResponse(result)


class LotChatSubscribe(APIView):
    """Called when a user sends a chat message about a lot to create a ChatSubscription model"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        return redirect(reverse("home"))

    def post(self, request, *args, **kwargs):
        try:
            lot = Lot.objects.filter(pk=request.POST["lot"]).first()
        except ValueError:
            lot = None
        if not lot:
            msg = f"No lot found with key {lot}"
            raise Http404(msg)
        else:
            subscription = ChatSubscription.objects.filter(
                user=request.user,
                lot=lot,
            ).first()
            if not subscription:
                subscription = ChatSubscription.objects.create(
                    user=request.user,
                    lot=lot,
                )
            unsubscribed = request.POST["unsubscribed"]
            if unsubscribed == "true":  # classic javascript, again
                subscription.unsubscribed = True
            else:
                subscription.unsubscribed = False
            subscription.save()
        return JsonResponse({"unsubscribed": subscription.unsubscribed})


class ChatSubscriptions(LoginRequiredMixin, TemplateView):
    """Show chat messages on your lots and other lots"""

    template_name = "chat_subscriptions.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["subscriptions"] = self.request.user.userdata.subscriptions_with_new_message_annotation.order_by(
            "-new_message_count", "-lot__date_posted"
        )
        context["data"] = self.request.user.userdata
        return context


class AddTosMemo(APIView, AuctionViewMixin):
    """API view to update the memo field of an auctiontos"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def dispatch(self, request, *args, **kwargs):
        pk = kwargs.pop("pk")
        self.auctiontos = AuctionTOS.objects.filter(pk=pk).first()
        if not self.auctiontos:
            raise Http404
        self.auction = self.auctiontos.auction
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        return redirect(reverse("home"))

    def post(self, request, *args, **kwargs):
        memo = request.POST["memo"]
        if memo or memo == "":
            self.auctiontos.memo = memo
            self.auctiontos.save()
            # Sync memo back to the linked ClubMember when the auction manages users through the club
            if self.auction.is_club_managed and self.auctiontos.clubmember_id:
                ClubMember.objects.filter(pk=self.auctiontos.clubmember_id).update(memo=memo)
            return JsonResponse({"result": "ok"})
        raise Http404


class AuctionNoShow(TemplateView, LoginRequiredMixin, AuctionViewMixin):
    """When someone doesn't show up for an auction, offer some tools to clean up the situation"""

    template_name = "auctions/noshow.html"

    def dispatch(self, request, *args, **kwargs):
        self.auction = get_object_or_404(Auction, slug=kwargs.pop("slug"), is_deleted=False)
        self.is_auction_admin
        self.tos = get_object_or_404(AuctionTOS, auction=self.auction, bidder_number=kwargs.pop("tos"))
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        context["tos"] = self.tos
        context["bought_lots"] = add_price_info(self.tos.bought_lots_qs)
        context["sold_lots"] = self.tos.lots_qs.annotate(
            full_buyer_refund=ExpressionWrapper(
                F("winning_price") + (F("winning_price") * F("auction__tax") / 100),
                output_field=FloatField(),
            )
        )
        return context


class AuctionNoShowAction(AuctionNoShow, FormMixin):
    """Refund lots, leave feedback, and ban this user"""

    template_name = "auctions/generic_admin_form.html"
    form_class = AuctionNoShowForm

    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs["auction"] = self.auction
        form_kwargs["tos"] = self.tos
        return form_kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["tooltip"] = f"Check any actions you wish to take against {self.tos.name}"
        context["unsold_lot_warning"] = "These actions cannot be undone!"
        context["modal_title"] = f"Take action against {self.tos.name}"
        return context

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            actions = f"Took actions against {self.tos.name}: "
            refund_sold_lots = form.cleaned_data["refund_sold_lots"]
            refund_bought_lots = form.cleaned_data["refund_bought_lots"]
            leave_negative_feedback = form.cleaned_data["leave_negative_feedback"]
            ban_this_user = form.cleaned_data["ban_this_user"]
            if refund_sold_lots:
                actions += "refunded sold lots, "
                for lot in self.tos.lots_qs:
                    if lot.winning_price:
                        lot.refund(100, request.user)
                    else:
                        lot.remove(True, request.user)
            if refund_bought_lots:
                actions += "refunded bought lots, "
                for lot in self.tos.bought_lots_qs:
                    lot.refund(100, request.user)
            if leave_negative_feedback:
                actions += "left negative feedback, "
                for lot in self.tos.bought_lots_qs:
                    lot.winner_feedback_rating = -1
                    lot.winner_feedback_text = "Did not pay"
                    lot.save()
                for lot in self.tos.lots_qs:
                    lot.feedback_rating - 1
                    lot.feedback_text = "Did not provide lot"
                    lot.save()
            if ban_this_user:
                actions += "banned user from future auctions, "
                # we will ban the user whether or not the tos was manually added
                # do not return any evidence to the caller of this request that the ban worked or didn't
                # as that could be used to determine if someone has an account on the site
                user = User.objects.filter(email=self.tos.email).first()
                if self.tos.email and user:
                    obj, created = UserBan.objects.update_or_create(
                        banned_user=user,
                        user=request.user,
                        defaults={},
                    )
            self.auction.create_history(applies_to="USERS", action=actions[:-2], user=request.user)
            return close_modal_response("reload-page")
        else:
            return self.form_invalid(form)
