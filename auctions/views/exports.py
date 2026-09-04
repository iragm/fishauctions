"""Taking data back out: the CSV exports, the reports, and the mailing list.

Everything an auction admin downloads or emails rather than reads on a page -- the lot and invoice
CSVs, the seller's report, the PayPal export -- plus the two views that push a set of participants
into a club or a marketing list.
"""

import csv
import logging
from datetime import timedelta
from urllib.parse import quote_plus, unquote

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.sites.models import Site
from django.core.exceptions import PermissionDenied
from django.db.models import (
    Count,
    IntegerField,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Sum,
)
from django.db.models.base import Model as Model
from django.http import (
    Http404,
    HttpResponse,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import ListView, TemplateView, View
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from auctions.filters import (
    AuctionTOSFilter,
    LotAdminFilter,
)
from auctions.models import (
    Auction,
    AuctionDropdown,
    AuctionTOS,
    Bid,
    ClubHistory,
    ClubMember,
    Invoice,
    Lot,
    LotHistory,
    PageView,
    add_price_info,
    find_image,
)
from auctions.species_matching import (
    suggest_species,
)

from .base import AuctionViewMixin, check_club_permission

logger = logging.getLogger(__name__)


class MyWonLotCSV(LoginRequiredMixin, View):
    """CSV file showing won lots"""

    def get(self, request):
        lots = add_price_info(
            Lot.objects.filter(Q(winner=request.user) | Q(auctiontos_winner__email=request.user.email))
            .exclude(is_deleted=True)
            # auction as well as species: lot.scientific_name reads the auction's setting, and a
            # query per row is not worth paying for a column.
            .select_related("species", "auction")
        )
        current_site = Site.objects.get_current()
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="my_won_lots_from_{current_site.domain.replace(".", "_")}.csv"'
        )
        writer = csv.writer(response)
        writer.writerow(["Lot number", "Name", "Scientific name", "Auction", "Winning price", "Link"])
        for lot in lots:
            writer.writerow(
                [
                    lot.lot_number_display,
                    lot.lot_name,
                    lot.scientific_name,
                    lot.auction,
                    f"{lot.currency_symbol}{lot.winning_price}",
                    "https://" + lot.full_lot_link,
                ]
            )
        return response


class MyLotReportView(LoginRequiredMixin, View):
    """CSV file showing sold lots"""

    def get(self, request):
        lots = add_price_info(
            Lot.objects.filter(Q(user=request.user) | Q(auctiontos_seller__email=request.user.email))
            .exclude(is_deleted=True)
            # auction too: lot.scientific_name reads the auction's setting (see the property).
            .select_related("bap_award__club_member__club", "species", "auction")
        )
        current_site = Site.objects.get_current()
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="my_lots_from_{current_site.domain.replace(".", "_")}.csv"'
        )
        writer = csv.writer(response)
        writer.writerow(
            [
                "Lot number",
                "Name",
                "Scientific name",
                "Auction",
                "Status",
                "Winning price",
                "My cut",
                "BAP points",
                "HAP points",
                "Culture points",
                "Points reason",
                "Points club",
                "Lot URL",
            ]
        )
        for lot in lots:
            status = "Unsold"
            if lot.banned:
                status = "Removed"
            elif lot.deactivated:
                status = "Deactivated"
            elif lot.winner or lot.auctiontos_winner:
                status = "Sold"
            bap_pts = hap_pts = cap_pts = points_reason = points_club = ""
            try:
                award = lot.bap_award
                bap_pts = award.points or ""
                hap_pts = award.hap_points or ""
                cap_pts = award.cap_points or ""
                points_reason = award.notes or ""
                points_club = award.club_member.club.name if award.club_member_id and award.club_member.club_id else ""
            except Exception:
                pass
            writer.writerow(
                [
                    lot.lot_number_display,
                    lot.lot_name,
                    lot.scientific_name,
                    lot.auction,
                    status,
                    lot.winning_price,
                    lot.your_cut,
                    bap_pts,
                    hap_pts,
                    cap_pts,
                    points_reason,
                    points_club,
                    "https://" + lot.full_lot_link,
                ]
            )
        return response


def _report_counts(auction, users):
    """Every per-person number the auction report prints, as four GROUP BYs.

    Keyed by ``AuctionTOS`` pk (lots) or by user pk (views, bids, other auctions), so the loop that
    writes the CSV can look each person up rather than asking the database about them.
    """
    tos_pks = [tos.pk for tos in users]
    user_pks = [tos.user_id for tos in users if tos.user_id]
    lots = Lot.objects.exclude(is_deleted=True).filter(auction=auction)
    submitted = {
        row["auctiontos_seller"]: row
        for row in lots.filter(auctiontos_seller__in=tos_pks)
        .order_by()
        .values("auctiontos_seller")
        .annotate(
            submitted=Count("pk"),
            sold=Count("pk", filter=Q(winning_price__isnull=False)),
            bred=Count("pk", filter=Q(i_bred_this_fish=True)),
        )
    }
    won = {
        row["auctiontos_winner"]: row["total"]
        for row in lots.filter(auctiontos_winner__in=tos_pks)
        .order_by()
        .values("auctiontos_winner")
        .annotate(total=Count("pk"))
    }
    views = {
        row["user"]: row["total"]
        for row in PageView.objects.filter(lot_number__auction=auction, user__in=user_pks)
        .order_by()
        .values("user")
        .annotate(total=Count("pk"))
    }
    bids = {
        row["user"]: row["total"]
        for row in Bid.objects.exclude(is_deleted=True)
        .filter(lot_number__auction=auction, user__in=user_pks)
        .order_by()
        .values("user")
        .annotate(total=Count("pk"))
    }
    auctions_joined = {
        row["user"]: row["total"]
        for row in AuctionTOS.objects.filter(user__in=user_pks).order_by().values("user").annotate(total=Count("pk"))
    }
    return {
        "submitted": submitted,
        "won": won,
        "views": views,
        "bids": bids,
        "auctions_joined": auctions_joined,
    }


class AuctionReportView(LoginRequiredMixin, AuctionViewMixin, View):
    """Get a CSV file showing all users who are participating in this auction"""

    def get(self, request):
        query = request.GET.get("query", None)
        response = HttpResponse(content_type="text/csv")
        end = timezone.now().strftime("%Y-%m-%d")
        if not query:
            filename = self.auction.slug + "-report-" + end
        else:
            filename = self.auction.slug + "-report-" + query + "-" + end
        response["Content-Disposition"] = f'attachment; filename="{filename}.csv"'
        writer = csv.writer(response)
        writer.writerow(
            [
                "Join date",
                "Bidder number",
                "Username",
                "Name",
                "Email",
                "Phone",
                "Address",
                "Location",
                "Miles to pickup location",
                "Club",
                "Lots viewed",
                "Lots bid",
                "Lots submitted",
                "Lots sold",
                "Lots won",
                "Invoice",
                "Total bought",
                "Gross sold",
                "Total payout",
                "Total club cut",
                "Invoice total due",
                "Breeder points",
                "Number of lots sold outside auction",
                "Total value of lots sold outside auction",
                "Seconds spent reading rules",
                "Other auctions joined",
                "Users who have banned this user",
                "Account created on",
                "Memo",
                self.auction.alternative_split_label.capitalize(),
                "Bidding allowed",
                "Added auction to their calendar",
            ]
        )
        # Use the auction's tos_qs property to get the has_ever_granted_permission annotation
        users = (
            self.auction.tos_qs.select_related("user__userdata")
            .select_related("pickup_location")
            .prefetch_related(
                Prefetch(
                    "auctiontos",
                    # the invoice's own numbers reach for its auction and that auction's club
                    queryset=Invoice.objects.select_related("auction__club", "club").order_by("-date"),
                )
            )
        )
        # Apply filter if query is provided
        if query:
            users = AuctionTOSFilter.generic(None, users, query)
        users = list(users)
        # Everything below used to be worked out one person at a time -- six `len(queryset)` calls
        # (each pulling every matching row into Python only to count it), an invoice lookup, and a
        # count of the person's other auctions. That is nine queries per row of a report an auction
        # of five hundred people runs. These are the same numbers, one GROUP BY each.
        counts = _report_counts(self.auction, users)
        # .annotate(distance_traveled=distance_to(\
        # '`auctions_userdata`.`latitude`', '`auctions_userdata`.`longitude`', \
        # lat_field_name='`auctions_pickuplocation`.`latitude`',\
        # lng_field_name="`auctions_pickuplocation`.`longitude`",\
        # approximate_distance_to=1)\
        # )
        for data in users:
            distance = ""
            club = ""
            if data.user and data.has_ever_granted_permission:
                # these things will only be written out if the user wants you to have it
                lotsViewed = counts["views"].get(data.user_id, 0)
                lotsBid = counts["bids"].get(data.user_id, 0)
                lot_qs = Lot.objects.exclude(is_deleted=True).filter(
                    user=data.user,
                    auction__isnull=True,
                    date_posted__gte=self.auction.date_start - timedelta(days=2),
                )
                if self.auction.is_online:
                    lotsOutsideAuction = lot_qs.filter(date_posted__lte=self.auction.date_end + timedelta(days=2))
                else:
                    lotsOutsideAuction = lot_qs.filter(date_posted__lte=self.auction.date_start + timedelta(days=5))
                numberLotsOutsideAuction = lotsOutsideAuction.count()
                profitOutsideAuction = lotsOutsideAuction.aggregate(total=Sum("winning_price"))["total"]
                if not profitOutsideAuction:
                    profitOutsideAuction = 0
                distance = data.distance_traveled or ""
                club = getattr(data.user.userdata, "club", None)
                username = data.user.username
                previous_auctions = max(counts["auctions_joined"].get(data.user_id, 0) - 1, 0)
                number_of_userbans = data.number_of_userbans
                account_age = data.user.date_joined
                add_to_calendar = "Yes" if data.add_to_calendar else ""
            else:
                add_to_calendar = ""
                previous_auctions = ""
                lotsViewed = ""
                lotsBid = ""  # noqa: F841 -- written out as a blank cell below
                numberLotsOutsideAuction = ""
                profitOutsideAuction = ""
                username = ""
                number_of_userbans = 0
                account_age = ""
            submitted = counts["submitted"].get(data.pk, {})
            lotsSumbitted = submitted.get("submitted", 0)
            lotsSold = submitted.get("sold", 0)
            breederPoints = submitted.get("bred", 0)
            lotsWon = counts["won"].get(data.pk, 0)
            address = data.address or ""
            # data.invoice is the prefetched one; gross_sold and total_club_cut below read it too,
            # so fetching it separately here meant two invoice queries per row rather than none.
            invoice = data.invoice
            if invoice:
                invoiceStatus = invoice.get_status_display()
                totalSpent = invoice.total_bought
                totalPaid = invoice.total_sold
                invoiceTotal = invoice.rounded_net
            else:
                invoiceStatus = ""
                totalSpent = 0
                totalPaid = 0
                invoiceTotal = 0
            writer.writerow(
                [
                    data.createdon.strftime("%m-%d-%Y"),
                    data.bidder_number,
                    username,
                    data.name,
                    data.email,
                    data.phone_as_string,
                    address,
                    data.pickup_location,
                    distance,
                    club,
                    lotsViewed,
                    lotsBid,
                    lotsSumbitted,
                    lotsSold,
                    lotsWon,
                    invoiceStatus,
                    f"{totalSpent:.2f}",
                    f"{data.gross_sold:.2f}",
                    f"{totalPaid:.2f}",
                    f"{data.total_club_cut:.2f}",
                    f"{invoiceTotal:.2f}",
                    breederPoints,
                    numberLotsOutsideAuction,
                    profitOutsideAuction,
                    data.time_spent_reading_rules,
                    previous_auctions,
                    number_of_userbans,
                    account_age,
                    data.memo,
                    "Yes" if data.is_club_member else "",
                    # Spelled out both ways on purpose: this file gets edited and fed back into the user
                    # importer, where a blank permission cell is ambiguous (it used to mean "no").
                    "Yes" if data.bidding_allowed else "No",
                    add_to_calendar,
                ]
            )
        self.auction.create_history(
            applies_to="USERS",
            action="Exported user CSV",
            user=request.user,
        )
        return response


class AddAuctionUsersToClub(LoginRequiredMixin, AuctionViewMixin, View):
    """Add all auction participants (with email) to the auction's associated club.

    Only creates new ClubMember records — never updates existing ones.
    Skips participants without an email address.
    """

    def post(self, request, *args, **kwargs):
        auction = self.auction
        club = auction.club
        if not club:
            messages.error(request, "This auction is not associated with a club.")
            return redirect(reverse("auction_tos_list", kwargs={"slug": auction.slug}))

        # Permission check: must have add_edit permission on the club or be the auction creator
        if (
            not request.user.is_superuser
            and not check_club_permission(request.user, club, "permission_add_edit")
            and not check_club_permission(request.user, club, "permission_manage_auctions")
        ):
            messages.error(request, "You don't have permission to add members to that club.")
            return redirect(reverse("auction_tos_list", kwargs={"slug": auction.slug}))

        tos_qs = (
            AuctionTOS.objects.filter(auction=auction)
            .exclude(email="")
            .filter(email__isnull=False)
            .select_related("user")
        )
        added_count = 0
        skipped_count = 0
        # Who is already a member, in two queries rather than two per person in the auction.
        # Emails are matched case-insensitively, as the per-row lookup did.
        members_by_email = {}
        members_by_user = {}
        for member_email, member_user_id in ClubMember.objects.filter(club=club).values_list("email", "user_id"):
            if member_email:
                members_by_email[member_email.lower()] = True
            if member_user_id:
                members_by_user[member_user_id] = True
        for tos in tos_qs:
            existing = bool(tos.email and members_by_email.get(tos.email.lower()))
            if not existing and tos.user_id:
                existing = bool(members_by_user.get(tos.user_id))
            if existing:
                skipped_count += 1
                continue
            ClubMember.objects.create(
                club=club,
                user=tos.user,
                name=tos.name or "",
                email=tos.email,
                phone_number=tos.phone_number or "",
                address=tos.address or "",
                source=str(auction.title)[:200],
                added_by=request.user,
            )
            # keep the maps current so two TOS rows with the same email do not both get added
            if tos.email:
                members_by_email[tos.email.lower()] = True
            if tos.user_id:
                members_by_user[tos.user_id] = True
            added_count += 1

        if added_count:
            messages.success(
                request,
                f"Added {added_count} user{'s' if added_count != 1 else ''} to {club.name}."
                + (f"  {skipped_count} already in club." if skipped_count else ""),
            )
            auction.create_history(
                applies_to="USERS",
                action=f"Added {added_count} auction participants to club '{club.name}' ({skipped_count} already members).",
                user=request.user,
            )
            ClubHistory.objects.create(
                club=club,
                user=request.user,
                action=f"Added {added_count} participant{'s' if added_count != 1 else ''} from auction '{auction}' ({skipped_count} already members)",
                applies_to="MEMBERS",
            )
        else:
            messages.info(
                request,
                f"No new users to add — all {skipped_count} participant{'s' if skipped_count != 1 else ''} with an email are already in {club.name}."
                if skipped_count
                else "No participants with email addresses found.",
            )
        return redirect(reverse("auction_tos_list", kwargs={"slug": auction.slug}))


class AddSingleAuctionTOSToClub(LoginRequiredMixin, View):
    """Add a single AuctionTOS participant to the auction's associated club."""

    def post(self, request, pk):
        tos = get_object_or_404(AuctionTOS, pk=pk)
        auction = tos.auction
        club = auction.club
        if not club:
            return HttpResponse("No club associated with this auction.", status=400)

        if (
            not request.user.is_superuser
            and not check_club_permission(request.user, club, "permission_add_edit")
            and not check_club_permission(request.user, club, "permission_manage_auctions")
        ):
            raise PermissionDenied()

        existing = None
        if tos.email:
            existing = ClubMember.objects.filter(club=club, email__iexact=tos.email, is_deleted=False).first()
        if not existing and tos.user:
            existing = ClubMember.objects.filter(club=club, user=tos.user, is_deleted=False).first()

        if not existing:
            ClubMember.objects.create(
                club=club,
                user=tos.user,
                name=tos.name or "",
                email=tos.email or "",
                phone_number=tos.phone_number or "",
                address=tos.address or "",
                source=str(auction.title)[:200],
                added_by=request.user,
            )
            messages.success(request, f"Added {tos.name} to {club.name}.")
            auction.create_history(
                applies_to="USERS",
                action=f"Added {tos.name} to club '{club.name}'.",
                user=request.user,
            )
            ClubHistory.objects.create(
                club=club,
                user=request.user,
                action=f"Added {tos.name} from auction '{auction}'",
                applies_to="MEMBERS",
            )

        if request.headers.get("HX-Request"):
            return HttpResponse("", headers={"HX-Refresh": "true"})
        return redirect(reverse("auction_tos_list", kwargs={"slug": auction.slug}))


class ComposeEmailToUsers(LoginRequiredMixin, AuctionViewMixin, TemplateView):
    """Generate a mailto: link with BCC for filtered users - HTMX endpoint"""

    template_name = "email_users_button.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Get query parameter
        query = self.request.GET.get("query", "")
        # Get all users for the auction
        users = AuctionTOS.objects.filter(auction=self.auction).select_related("user")

        # Apply filter if query is provided
        if query:
            users = AuctionTOSFilter.generic(None, users, query)

        # Collect valid emails (non-null and non-empty)
        emails = list(users.filter(email__isnull=False).exclude(email="").values_list("email", flat=True))
        # Default values
        mailto_url = "#"
        email_count = 0

        if emails:
            # Limit to avoid overly long URLs (conservative cap)
            max_emails = 60
            if len(emails) > max_emails:
                emails = emails[:max_emails]

            bcc = ",".join(emails)
            subject = f"{self.auction.title}"
            body = f"Hello,\n\nThis message is being sent to participants in {self.auction.title}.\n\n"

            if "open" in query or "ready" in query:
                url = reverse("my_auction_invoice", kwargs={"slug": self.auction.slug})
                body += f"You can view your invoice here: https://{Site.objects.get_current().domain}{url}\n\n"
            mailto_url = f"mailto:?bcc={quote_plus(bcc)}&subject={quote_plus(subject)}&body={quote_plus(body)}"
            email_count = len(emails)

        context.update(
            {
                "mailto_url": mailto_url,
                "email_count": email_count,
            }
        )
        return context


class MarketingList(LoginRequiredMixin, View):
    """Get a CSV file showing all users from all auctions you're an admin for"""

    def get(self, request):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = "attachment; filename=all_auction_contacts.csv"
        writer = csv.writer(response)
        found = []
        writer.writerow(["Name", "Email", "Phone"])
        auctions = Auction.objects.filter(
            Q(created_by=request.user) | Q(auctiontos__is_admin=True, auctiontos__user=request.user)
        ).distinct()
        users = AuctionTOS.objects.filter(auction__in=auctions).exclude(email_address_status="BAD")
        for user in users:
            if user.email not in found:
                writer.writerow([user.name, user.email, user.phone_as_string])
                found.append(user.email)
        for auction in auctions:
            auction.create_history(
                applies_to="USERS",
                action="Exported marketing list CSV for all their auctions (including this one)",
                user=request.user,
            )
        return response


class AuctionInvoicesPayPalCSV(LoginRequiredMixin, AuctionViewMixin, View):
    """Get a CSV file of all unpaid invoices that owe the club money"""

    def get(self, request, chunk):
        # Create the HttpResponse object with the appropriate CSV header.
        response = HttpResponse(content_type="text/csv")
        due_date = timezone.now().strftime("%m/%d/%Y")
        current_site = Site.objects.get_current()
        response["Content-Disposition"] = f'attachment; filename="{self.auction.slug}-paypal-{chunk}.csv"'
        writer = csv.writer(response)
        writer.writerow(
            [
                "Recipient Email",
                "Recipient First Name",
                "Recipient Last Name",
                "Invoice Number",
                "Due Date",
                "Reference",
                "Item Name",
                "Description",
                "Item Amount",
                "Shipping Amount",
                "Discount Amount",
                "Currency Code",
                "Note to Customer",
                "Terms and Conditions",
                "Memo to Self",
            ]
        )
        count = 0
        chunkSize = 150  # attention: this is also set in models.auction.paypal_invoice_chunks
        no_email_count = 0
        # Keep every unpaid invoice's stored total fresh before billing (side effect preserved).
        for invoice in self.auction.paypal_invoices:
            invoice.recalculate()
        # Bill only the invoices that still owe the club after rounding, advancing the chunk
        # counter over the exact same set that auction.paypal_invoice_chunks counts
        # (auction.paypal_invoices_to_export), so every billed invoice lands in a chunk the UI
        # offers -- see Item 21.
        for invoice in self.auction.paypal_invoices_to_export:
            count += 1
            if count <= chunkSize * chunk and count > chunkSize * (chunk - 1):
                reference = ""
                itemName = "Auction total"
                description = ""
                shippingAmount = 0
                discountAmount = 0
                currencyCode = self.auction.created_by.userdata.currency
                noteToCustomer = f"https://{current_site.domain}/invoices/{invoice.pk}/"
                termsAndConditions = ""
                memoToSelf = invoice.auctiontos_user.memo
                # Bill the rounded balance so the PayPal invoice matches the invoice total the
                # buyer sees. Every invoice here already owes the club (rounded_net_after_payments
                # < 0); a missing email is reported via no_email_count but still consumes its slot.
                if invoice.auctiontos_user.email:
                    name_parts = (invoice.auctiontos_user.name or "").split()
                    if len(name_parts) >= 2:
                        first_name = name_parts[0][:20]
                        last_name = name_parts[-1][:20]
                    else:
                        first_name = ""
                        last_name = name_parts[0][:20] if name_parts else ""
                    writer.writerow(
                        [
                            invoice.auctiontos_user.email,
                            first_name,
                            last_name,
                            invoice.pk,
                            due_date,
                            reference,
                            itemName,
                            description,
                            abs(invoice.rounded_net_after_payments),
                            shippingAmount,
                            discountAmount,
                            currencyCode,
                            noteToCustomer,
                            termsAndConditions,
                            memoToSelf,
                        ]
                    )
                else:
                    no_email_count += 1
        self.auction.create_history(
            applies_to="USERS",
            action=f"Exported PayPal invoices CSV.  {no_email_count} users had no email address and were not included in the CSV.",
            user=request.user,
        )
        return response


class AuctionLotsCSV(LoginRequiredMixin, AuctionViewMixin, View):
    """Get a CSV file showing all sold lots, who bought/sold them, and the winner's location"""

    def get(self, request):
        # Create the HttpResponse object with the appropriate CSV header.
        query = request.GET.get("query", None)
        response = HttpResponse(content_type="text/csv")
        if not query:
            filename = "all-lot-list"
        else:
            filename = "lot-list-" + query
            query = unquote(query)
        response["Content-Disposition"] = f'attachment; filename="{self.auction.slug}-{filename}.csv"'
        writer = csv.writer(response)
        custom_dropdown_enabled = (
            self.auction.use_custom_dropdown_field != "disable"
            and bool(self.auction.custom_dropdown_name)
            and AuctionDropdown.objects.filter(auction=self.auction).count() >= 2
        )
        first_row_fields = [
            "Lot number",
            "Lot",
            "Seller",
            "Seller email",
            "Seller phone",
            "Seller location",
            "Winner",
            "Winner email",
            "Winner phone",
            "Winner location",
            "Breeder points",
            "Donation",
            "Sell price",
            "Club Cut",
            "Seller cut",
        ]
        # Only when the auction actually collected one, so a club that turned the field off
        # doesn't get an empty column in every report.
        if self.auction.use_scientific_name:
            first_row_fields.insert(2, "Scientific name")
        if self.auction.use_custom_checkbox_field and self.auction.custom_checkbox_name:
            first_row_fields.append(self.auction.custom_checkbox_name)
        if self.auction.custom_field_1 != "disable" and self.auction.custom_field_1_name:
            first_row_fields.append(self.auction.custom_field_1_name)
        if custom_dropdown_enabled:
            first_row_fields.append(self.auction.custom_dropdown_name)
        writer.writerow(first_row_fields)
        lots = self.auction.lots_qs.select_related("species", "auction")
        lots = add_price_info(lots)
        if query:
            lots = LotAdminFilter.generic(None, lots, query)
        for lot in lots:
            row = [
                lot.lot_number_display,
                lot.lot_name,
                lot.auctiontos_seller.name,
                lot.auctiontos_seller.email,
                lot.auctiontos_seller.phone_as_string,
                lot.location,
                lot.auctiontos_winner.name if lot.auctiontos_winner else "",
                lot.auctiontos_winner.email if lot.auctiontos_winner else "",
                lot.auctiontos_winner.phone_as_string if lot.auctiontos_winner else "",
                lot.winner_location,
                lot.i_bred_this_fish_display,
                lot.donation,
                f"{lot.winning_price:.2f}" if lot.winning_price else "",
                f"{lot.club_cut:.2f}" if lot.winning_price else "",
                f"{lot.your_cut:.2f}" if lot.winning_price else "",
            ]
            if self.auction.use_scientific_name:
                row.insert(2, lot.scientific_name)
            if self.auction.use_custom_checkbox_field and self.auction.custom_checkbox_name:
                row.append(lot.custom_checkbox_label)
            if self.auction.custom_field_1 != "disable" and self.auction.custom_field_1_name:
                row.append(lot.custom_field_1)
            if custom_dropdown_enabled:
                row.append(lot.custom_dropdown)
            writer.writerow(row)
        self.auction.create_history(
            applies_to="LOTS",
            action=f"Exported lot list CSV for {query or 'all lots'}",
            user=request.user,
        )
        return response


class LeaveFeedbackView(LoginRequiredMixin, ListView):
    """Show all pickup locations belonging to the current user"""

    model = Lot
    template_name = "leave_feedback.html"
    ordering = ["-date_posted"]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        cutoffDate = timezone.now() - timedelta(days=90)
        context["won_lots"] = (
            Lot.objects.exclude(is_deleted=True)
            .filter(
                Q(winner=self.request.user) | Q(auctiontos_winner__user=self.request.user),
                date_posted__gte=cutoffDate,
            )
            .order_by("-date_posted")
        )
        context["sold_lots"] = (
            Lot.objects.exclude(is_deleted=True)
            .filter(
                Q(user=self.request.user) | Q(auctiontos_seller__user=self.request.user),
                date_posted__gte=cutoffDate,
                winning_price__isnull=False,
            )
            .order_by("-date_posted")
        )
        return context


class FindImageIcon(APIView):
    """Return a handy little icon if the lot name will have an image associated with it"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def dispatch(self, request, *args, **kwargs):
        self.auction = get_object_or_404(Auction, slug=kwargs.pop("slug"), is_deleted=False)
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        name = request.POST["name"]
        result = find_image(name, None, self.auction)
        if result:
            return HttpResponse("image available")
        return HttpResponse("")


class SpeciesSuggestions(APIView):
    """Given a lot name, return the handful of species it might be.

    Backs the scientific-name picker on every lot form.  The list is always short and always
    comes out of the Species table, so the client can render it as a ``<select>`` and the server
    can reject anything that isn't in it -- see ``configure_species_field`` and
    ``clean_species_for_auction`` in forms.py.
    """

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        name = (request.POST.get("name") or "").strip()
        # The last-typed name wins: on the bulk-add page several rows can be in flight at once and
        # the client matches responses back up by this.
        if not name:
            return JsonResponse({"name": name, "choices": [], "source": "none"})
        # Optional, and only ever a tie-break inside suggest_species.  Not validated beyond "is it
        # a number" on purpose: a category that doesn't exist simply matches nothing.
        category = request.POST.get("category") or None
        matches, source = suggest_species(
            name,
            user=request.user,
            category=int(category) if category and category.isdigit() else None,
        )
        return JsonResponse(
            {
                "name": name,
                "source": source,
                "choices": [
                    {
                        "id": species.pk,
                        "scientific_name": species.full_scientific_name,
                        "common_name": species.common_name,
                        # The category the lot will get if this species is picked -- by name for
                        # the line of text the forms show, and by pk so the category picker can be
                        # set to it rather than left showing whatever the name guesser said.
                        "category": str(species.category) if species.category else "",
                        "category_id": species.category_id or "",
                        "label": species.label,
                    }
                    for species in matches
                ],
            }
        )


class AuctionChats(AuctionViewMixin, LoginRequiredMixin, ListView):
    """Auction admins view to show and delete all chats for an auction"""

    model = LotHistory
    template_name = "chats.html"

    # def dispatch(self, request, *args, **kwargs):
    #     self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=kwargs.pop("slug")).first()
    #     if not self.auction:
    #         raise Http404
    #     self.is_auction_admin
    #     return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        # get related auctiontos if the user has joined the auction
        auctiontos_subquery = AuctionTOS.objects.filter(user=OuterRef("user"), auction=self.auction).values("pk")[:1]
        qs = (
            LotHistory.objects.filter(
                lot__auction=self.auction,
                changed_price=False,
            )
            .annotate(auctiontos_pk=Subquery(auctiontos_subquery, output_field=IntegerField(), null=True))
            .order_by("-timestamp")
        )
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        return context


class AuctionChatDeleteUndelete(APIView, AuctionViewMixin):
    """HTMX for auction admins only"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def dispatch(self, request, *args, **kwargs):
        pk = kwargs.get("pk")
        self.history = get_object_or_404(LotHistory, pk=pk, lot__auction__is_deleted=False)
        self.auction = self.history.lot.auction
        if not self.auction:
            raise Http404
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        # Toggle the removed field
        self.history.removed = not self.history.removed
        self.history.save()
        if not self.history.removed:
            result = f'<span id="message_{self.history.pk}" class="btn btn-sm btn-danger">Delete</span>'
        else:
            result = f'<span id="message_{self.history.pk}" class="btn btn-sm btn-secondary">Deleted</span>'
            self.auction.create_history(
                applies_to="USERS",
                action="Deleted chat message",
                user=self.request.user,
            )
        return HttpResponse(result)


class AuctionShowHighBidder(APIView, AuctionViewMixin):
    """HTMX for auction admins only"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def dispatch(self, request, *args, **kwargs):
        pk = kwargs.get("pk")
        self.lot = get_object_or_404(Lot, pk=pk, is_deleted=False, auction__isnull=False)
        self.auction = self.lot.auction
        self.is_auction_admin
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        if not self.lot.max_bid_revealed_by:
            self.lot.max_bid_revealed_by = request.user
            self.lot.save()
            LotHistory.objects.create(
                lot=self.lot,
                user=self.request.user,
                message=f"{self.request.user} has looked at the max bid on this lot",
                changed_price=True,
            )
        return HttpResponse(f"Max bid: ${self.lot.max_bid}")
