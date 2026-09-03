"""Getting lots in at once: the bulk table, the quick-add page, and the CSV importer.

Four ways into the same rows. ``SaveLotAjax`` is the per-row save behind the bulk table and is the
one place that writes to the species name cache on a row's first save, bounded to the five
suggestions the page offered.
"""

import json
import logging
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import (
    Q,
)
from django.db.models.base import Model as Model
from django.forms import modelformset_factory
from django.http import (
    Http404,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView, View
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from auctions.forms import (
    QUICK_ADD_LOT_FIELDS,
    LotFormSetHelper,
    QuickAddLot,
)
from auctions.models import (
    CUSTOM_DROPDOWN_MAX_LENGTH,
    Auction,
    AuctionDropdown,
    AuctionHistory,
    AuctionTOS,
    Category,
    Invoice,
    Lot,
    normalize_email,
)
from auctions.services import (
    LOT_ADD_BLOCK_BULK_DISABLED,
    LOT_ADD_BLOCK_NO_TOS,
    apply_club_member_to_tos,
    ensure_club_member,
    existing_tos_for_club_member,
    lot_add_block,
    recalculate_seller_invoice,
    save_new_lot,
)
from auctions.species_matching import record_choice as record_species_choice
from auctions.species_matching import remember as remember_species
from auctions.species_matching import (
    visible_species,
)

from .base import AuctionViewMixin
from .bulk_add import CSVContactImportMixin

logger = logging.getLogger(__name__)


class BulkAddLots(LoginRequiredMixin, AuctionViewMixin, TemplateView):
    """Add/edit lots of lots for a given auctiontos pk"""

    template_name = "auctions/bulk_add_lots.html"
    allow_non_admins = True

    def get(self, *args, **kwargs):
        lot_formset = self.LotFormSet(
            form_kwargs={
                "tos": self.tos,
                "auction": self.auction,
                # "custom_lot_numbers_used": [],
                "is_admin": self.is_admin,
            },
            queryset=self.queryset,
        )
        helper = LotFormSetHelper()
        context = self.get_context_data(**kwargs)
        context["formset"] = lot_formset
        context["helper"] = helper
        return self.render_to_response(context)

    def post(self, *args, **kwargs):
        lot_formset = self.LotFormSet(
            self.request.POST,
            form_kwargs={
                "tos": self.tos,
                "auction": self.auction,
                # "custom_lot_numbers_used": [],
                "is_admin": self.is_admin,
            },
            queryset=self.queryset,
        )
        if lot_formset.is_valid():
            lots = lot_formset.save(commit=False)
            new_lot_count = 0
            # Which of these rows the seller actually moved the species on.  A rejection is
            # evidence about a lot, not about a save, so re-posting a row whose species was
            # cleared last week must not count a second time -- see record_choice.
            species_moved = {id(form.instance) for form in lot_formset.forms if "species" in form.changed_data}
            for lot in lots:
                lot_is_new = not lot.pk
                if lot_is_new:
                    new_lot_count += 1
                    # save_new_lot is shared with the command palette's add_lot action so a lot
                    # added by voice lands exactly the same way as one added on this page.
                    save_new_lot(lot, auction=self.auction, tos=self.tos, added_by=self.request.user)
                else:
                    lot.auctiontos_seller = self.tos
                    lot.auction = self.auction
                    owner = self.tos.lot_owner(self.request.user)
                    if owner:
                        lot.user = owner
                    lot.save()
                # What the seller did with the species this lot name was remembered as.  The same
                # report the ajax bulk page makes -- this is the other bulk page, and a remembered
                # answer cleared here is exactly the same evidence.  See record_choice.
                if self.auction.use_scientific_name and lot.lot_name:
                    record_species_choice(
                        lot.lot_name, lot.species, first_save=lot_is_new, changed=id(lot) in species_moved
                    )
            if lots:
                updated_lot_count = len(lots) - new_lot_count
                self.auction.create_history(
                    applies_to="LOTS",
                    action=f"Bulk added {new_lot_count}{f' and updated {updated_lot_count}' if updated_lot_count else ''} lots for {self.tos.name}",
                    user=self.request.user,
                )
                messages.success(self.request, f"Updated lots for {self.tos.name}")
                recalculate_seller_invoice(self.auction, self.tos)
            # when saving labels, it doesn't take you off from the page you're on
            # So we need to go somewhere, and then say "download labels"
            if "print" in str(self.request.GET.get("type", "")):
                print_url = f"printredirect={reverse('print_labels_by_bidder_number', kwargs={'slug': self.auction.slug, 'bidder_number': self.tos.bidder_number})}"
            else:
                print_url = ""
            if self.is_admin:
                redirect_url = reverse("auction_tos_list", kwargs={"slug": self.auction.slug})
                if print_url:
                    redirect_url += "?" + print_url
            else:
                redirect_url = reverse("selling")
                if print_url:
                    redirect_url += "?" + print_url
            return redirect(redirect_url)

        context = self.get_context_data(**kwargs)
        context["formset"] = lot_formset
        context["helper"] = LotFormSetHelper()
        return self.render_to_response(context)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["tos"] = self.tos
        context["auction"] = self.auction
        context["is_admin"] = self.is_admin
        return context

    def dispatch(self, request, *args, **kwargs):
        self.auction = get_object_or_404(Auction, slug=kwargs.pop("slug"), is_deleted=False)
        self.is_admin = False
        if not self.auction:
            raise Http404
        bidder_number = kwargs.pop("bidder_number", None)
        self.tos = None
        if bidder_number:
            self.tos = AuctionTOS.objects.filter(bidder_number=bidder_number, auction=self.auction).first()
        if self.is_auction_admin:
            self.is_admin = True
        if not self.tos:
            # if you don't got permission to edit this auction, you can only add lots for yourself
            self.tos = (
                AuctionTOS.objects.filter(auction=self.auction)
                .filter(Q(email=request.user.email) | Q(user=request.user))
                .first()
            )
        block = lot_add_block(self.auction, self.tos, self.is_admin)
        if block:
            code, message = block
            messages.error(request, message)
            if code == LOT_ADD_BLOCK_NO_TOS:
                return redirect(
                    f"{reverse('auction_main', kwargs={'slug': self.auction.slug})}?next={reverse('bulk_add_lots_auto_for_myself', kwargs={'slug': self.auction.slug})}"
                )
            if code == LOT_ADD_BLOCK_BULK_DISABLED:
                return redirect(self.auction.add_lot_link)
            return redirect(reverse("auction_main", kwargs={"slug": self.auction.slug}))
        self.queryset = self.tos.unbanned_lot_qs
        if self.auction.max_lots_per_user:
            # default rows should be the max that are allowed in the auction
            if self.queryset.count() > self.auction.max_lots_per_user:
                extra = self.queryset.count()
            else:
                extra = self.auction.max_lots_per_user - self.queryset.count()
            # but of course sometimes admisn will break the rules for their users:
            extra = max(extra, 0)
        else:
            extra = 5  # default rows to show if max_lots_per_user is not set for this auction
        self.LotFormSet = modelformset_factory(
            Lot,
            extra=extra,
            fields=QUICK_ADD_LOT_FIELDS,
            form=QuickAddLot,
        )
        return super().dispatch(request, *args, **kwargs)


class BulkAddLotsAuto(LoginRequiredMixin, AuctionViewMixin, TemplateView):
    """Add/edit lots with auto-save functionality - lots are saved as user types"""

    template_name = "auctions/bulk_add_lots_auto.html"
    allow_non_admins = True

    def get(self, *args, **kwargs):
        context = self.get_context_data(**kwargs)
        return self.render_to_response(context)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["tos"] = self.tos
        context["auction"] = self.auction
        context["is_admin"] = self.is_admin

        # Get existing lots for this user/auction
        context["existing_lots"] = self.queryset.order_by("-date_posted")

        # Get custom field configurations
        context["use_custom_checkbox"] = self.auction.use_custom_checkbox_field and self.auction.custom_checkbox_name
        context["custom_checkbox_name"] = self.auction.custom_checkbox_name if context["use_custom_checkbox"] else ""

        context["use_custom_field_1"] = self.auction.custom_field_1 != "disable" and self.auction.custom_field_1_name
        context["custom_field_1_name"] = self.auction.custom_field_1_name if context["use_custom_field_1"] else ""
        context["custom_field_1_required"] = self.auction.custom_field_1 == "required"
        context["custom_dropdown_name"] = self.auction.custom_dropdown_name
        context["custom_dropdown_options"] = list(
            AuctionDropdown.objects.filter(auction=self.auction).order_by("createdon").values_list("value", flat=True)
        )
        context["use_custom_dropdown"] = (
            self.auction.use_custom_dropdown_field != "disable"
            and self.auction.custom_dropdown_name
            and len(context["custom_dropdown_options"]) >= 2
        )
        context["custom_dropdown_required"] = self.auction.use_custom_dropdown_field == "required"

        context["use_i_bred_this_fish"] = self.auction.use_i_bred_this_fish_field
        context["use_quantity"] = self.auction.use_quantity_field
        context["use_donation"] = self.auction.use_donation_field

        context["reserve_price_mode"] = self.auction.reserve_price
        context["buy_now_mode"] = self.auction.buy_now
        context["minimum_bid"] = self.auction.minimum_bid

        context["auto_add_images"] = self.auction.auto_add_images
        context["use_scientific_name"] = self.auction.use_scientific_name

        # Lot limit settings
        context["max_lots_per_user"] = self.auction.max_lots_per_user
        context["allow_additional_lots_as_donation"] = self.auction.allow_additional_lots_as_donation
        context["current_lot_count"] = self.queryset.count()

        # For determining number of initial blank rows
        max_lots = self.auction.max_lots_per_user
        current_count = self.queryset.count()
        if max_lots:
            initial_rows = min(5, max_lots - current_count) if current_count < max_lots else 0
        else:
            initial_rows = 5
        context["initial_rows"] = max(initial_rows, 1)  # At least 1 row

        return context

    def dispatch(self, request, *args, **kwargs):
        self.get_auction(kwargs.pop("slug", ""))
        bidder_number = kwargs.pop("bidder_number", None)
        self.tos = None

        # Security: Only admins can access the bidder_number URL
        if bidder_number:
            # Check admin status first
            if not self.is_auction_admin:
                messages.error(request, "Only auction admins can add lots for other users")
                return redirect(reverse("auction_main", kwargs={"slug": self.auction.slug}))
            self.tos = AuctionTOS.objects.filter(bidder_number=bidder_number, auction=self.auction).first()
            if not self.tos:
                messages.error(request, "User not found in this auction")
                return redirect(reverse("auction_tos_list", kwargs={"slug": self.auction.slug}))

        self.is_admin = self.is_auction_admin

        if not self.tos:
            # if you don't got permission to edit this auction, you can only add lots for yourself
            self.tos = (
                AuctionTOS.objects.filter(auction=self.auction)
                .filter(Q(email=request.user.email) | Q(user=request.user))
                .first()
            )
        block = lot_add_block(self.auction, self.tos, self.is_admin)
        if block:
            code, message = block
            messages.error(request, message)
            if code == LOT_ADD_BLOCK_NO_TOS:
                return redirect(
                    f"{reverse('auction_main', kwargs={'slug': self.auction.slug})}?next={reverse('bulk_add_lots_auto_for_myself', kwargs={'slug': self.auction.slug})}"
                )
            if code == LOT_ADD_BLOCK_BULK_DISABLED:
                return redirect(self.auction.add_lot_link)
            return redirect(reverse("auction_main", kwargs={"slug": self.auction.slug}))
        self.queryset = self.tos.unbanned_lot_qs
        return super().dispatch(request, *args, **kwargs)


class SaveLotAjax(APIView, AuctionViewMixin):
    """AJAX endpoint to save a single lot"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    allow_non_admins = True

    def post(self, request, *args, **kwargs):
        try:
            data = json.loads(request.body)
            lot_id = data.get("lot_id")
            bidder_number = data.get("bidder_number")

            # Determine which TOS we're adding lots for
            self.tos = None
            self.is_admin = self.is_auction_admin

            if bidder_number:
                # Someone is trying to add lots for a specific user
                # Only admins can do this
                if not self.is_admin:
                    return JsonResponse({"success": False, "error": "Only auction admins can add lots for other users"})
                self.tos = AuctionTOS.objects.filter(bidder_number=bidder_number, auction=self.auction).first()
                if not self.tos:
                    return JsonResponse({"success": False, "error": "User not found in this auction"})
            else:
                # Adding lots for yourself
                self.tos = (
                    AuctionTOS.objects.filter(auction=self.auction)
                    .filter(Q(email=request.user.email) | Q(user=request.user))
                    .first()
                )
                if not self.tos:
                    return JsonResponse({"success": False, "error": "You must join this auction before adding lots"})

            # Check if user has permission to add lots
            if not self.tos.selling_allowed and not self.is_admin:
                return JsonResponse(
                    {"success": False, "error": "You don't have permission to add lots to this auction"}
                )

            # Create or get existing lot
            if lot_id:
                lot = Lot.objects.filter(lot_number=lot_id, auction=self.auction, auctiontos_seller=self.tos).first()
                if not lot:
                    return JsonResponse({"success": False, "error": "Lot not found"})
                is_new = False

                # Check if lot can be edited
                if not lot.can_be_edited and not self.is_admin:
                    return JsonResponse(
                        {"success": False, "error": lot.cannot_be_edited_reason or "This lot cannot be edited"}
                    )
            else:
                lot = Lot(
                    auction=self.auction,
                    auctiontos_seller=self.tos,
                    user=self.tos.lot_owner(request.user),
                    added_by=request.user,
                )
                is_new = True
            # What the species was before this save touched it, so record_choice can tell a seller
            # taking the answer off a lot from a seller editing the price of a lot they already
            # took it off.  Only the first of those is evidence about the name.
            species_before = lot.species_id

            admin_bypassed_lot_limit = False  # Track if admin bypassed lot limit
            admin_bypassed_selling_allowed = False  # Track if admin bypassed selling_allowed

            # Check if admin is bypassing selling_allowed restriction
            if self.is_admin and not self.tos.selling_allowed:
                admin_bypassed_selling_allowed = True

            # Check lot limits
            if is_new and self.auction.max_lots_per_user:
                current_count = self.tos.unbanned_lot_qs.count()
                # Admins can bypass limits for both their own lots and other users' lots
                bypass_limit = self.is_admin
                limit_exceeded = current_count >= self.auction.max_lots_per_user

                if limit_exceeded and not bypass_limit:
                    # Check if donation lots are allowed beyond the limit
                    donation = data.get("donation", False)
                    if not donation or not self.auction.allow_additional_lots_as_donation:
                        return JsonResponse(
                            {
                                "success": False,
                                "errors": {
                                    "general": f"You have reached the maximum of {self.auction.max_lots_per_user} lots for this auction"
                                },
                            }
                        )

                # Track if admin bypassed the limit for visual feedback
                admin_bypassed_lot_limit = bypass_limit and limit_exceeded

            # Validate and save fields
            errors = {}

            lot_name = data.get("lot_name", "").strip()
            if not lot_name:
                errors["lot_name"] = "Lot name is required"
            elif len(lot_name) > 40:
                errors["lot_name"] = "Lot name must be 40 characters or less"
            else:
                lot.lot_name = lot_name

            # Species category (auto-set to Uncategorized)
            if not lot.species_category_id:
                lot.species_category = Category.objects.filter(name="Uncategorized").first()

            # Scientific name.  Only ever a pk from the suggestions endpoint; anything else is
            # rejected rather than coerced, so the column can't fill up with free text.
            if self.auction.use_scientific_name:
                species_id = data.get("species")
                if species_id in (None, "", "0"):
                    lot.species = None
                else:
                    species = visible_species(request.user, self.auction.club).filter(pk=species_id).first()
                    # Whatever is already on the lot stays allowed even when it isn't this
                    # person's to pick: an unapproved species another admin added is still the
                    # right answer for that lot, and every save posts the field back, so
                    # rejecting it here would make the row unsaveable rather than just unpickable.
                    if not species and str(lot.species_id) == str(species_id):
                        species = lot.species
                    if not species:
                        errors["species"] = "Pick a scientific name from the list"
                    else:
                        lot.species = species
            # No else: with the field switched off there is nothing on the page to post, so an
            # ajax save of any other field would otherwise wipe a species that is already stored.
            # Turning the setting off hides the field; it does not throw the column away.

            # Custom checkbox
            if self.auction.use_custom_checkbox_field and self.auction.custom_checkbox_name:
                lot.custom_checkbox = data.get("custom_checkbox", False)

            # Custom field 1
            if self.auction.custom_field_1 != "disable" and self.auction.custom_field_1_name:
                custom_field_1 = data.get("custom_field_1", "").strip()
                if self.auction.custom_field_1 == "required" and not custom_field_1:
                    errors["custom_field_1"] = f"{self.auction.custom_field_1_name} is required"
                elif len(custom_field_1) > 60:
                    errors["custom_field_1"] = f"{self.auction.custom_field_1_name} must be 60 characters or less"
                else:
                    lot.custom_field_1 = custom_field_1

            custom_dropdown_options = list(
                AuctionDropdown.objects.filter(auction=self.auction).values_list("value", flat=True)
            )
            if (
                self.auction.use_custom_dropdown_field != "disable"
                and self.auction.custom_dropdown_name
                and len(custom_dropdown_options) >= 2
            ):
                custom_dropdown = data.get("custom_dropdown", "").strip()
                if len(custom_dropdown) > CUSTOM_DROPDOWN_MAX_LENGTH:
                    errors["custom_dropdown"] = (
                        f"Custom dropdown value must be {CUSTOM_DROPDOWN_MAX_LENGTH} characters or less"
                    )
                elif custom_dropdown and custom_dropdown not in custom_dropdown_options:
                    errors["custom_dropdown"] = "Select a valid custom dropdown option"
                elif self.auction.use_custom_dropdown_field == "required" and not custom_dropdown:
                    errors["custom_dropdown"] = f"{self.auction.custom_dropdown_name} is required"
                else:
                    lot.custom_dropdown = custom_dropdown
            else:
                lot.custom_dropdown = ""

            # I bred this fish
            if self.auction.use_i_bred_this_fish_field:
                lot.i_bred_this_fish = data.get("i_bred_this_fish", False)

            # Quantity
            if self.auction.use_quantity_field:
                quantity = data.get("quantity")
                if quantity is None or quantity == "":
                    quantity = 1
                try:
                    quantity = int(quantity)
                    if quantity < 1:
                        errors["quantity"] = "Quantity must be at least 1"
                    else:
                        lot.quantity = quantity
                except (ValueError, TypeError):
                    errors["quantity"] = "Quantity must be a number"
            else:
                lot.quantity = 1

            # Donation
            if self.auction.use_donation_field:
                lot.donation = data.get("donation", False)

            # Reserve price
            if self.auction.reserve_price != "disable":
                reserve_price = data.get("reserve_price")
                if reserve_price is None or reserve_price == "":
                    reserve_price = self.auction.minimum_bid
                try:
                    reserve_price = Decimal(str(reserve_price))
                    if reserve_price < Decimal("0.01"):
                        errors["reserve_price"] = "Minimum bid must be at least $0.01"
                    elif reserve_price > 2000:
                        errors["reserve_price"] = "Minimum bid must be $2000 or less"
                    elif self.auction.only_whole_dollar_bids and reserve_price != reserve_price.to_integral_value():
                        errors["reserve_price"] = "This auction only allows whole dollar amounts"
                    else:
                        lot.reserve_price = reserve_price
                except (ValueError, TypeError, InvalidOperation):
                    errors["reserve_price"] = "Minimum bid must be a number"

                if self.auction.reserve_price == "required" and not reserve_price:
                    errors["reserve_price"] = "Minimum bid is required"
            else:
                lot.reserve_price = self.auction.minimum_bid

            # Buy now price
            if self.auction.buy_now != "disable":
                buy_now_price = data.get("buy_now_price")
                if buy_now_price is not None and buy_now_price != "":
                    try:
                        buy_now_price = Decimal(str(buy_now_price))
                        if buy_now_price < Decimal("0.01"):
                            errors["buy_now_price"] = "Buy now price must be at least $0.01"
                        elif buy_now_price > 1000:
                            errors["buy_now_price"] = "Buy now price must be $1000 or less"
                        elif self.auction.only_whole_dollar_bids and buy_now_price != buy_now_price.to_integral_value():
                            errors["buy_now_price"] = "This auction only allows whole dollar amounts"
                        else:
                            lot.buy_now_price = buy_now_price
                    except (ValueError, TypeError, InvalidOperation):
                        errors["buy_now_price"] = "Buy now price must be a number"
                else:
                    lot.buy_now_price = None

                if self.auction.buy_now == "required" and not buy_now_price:
                    errors["buy_now_price"] = "Buy now price is required"
            else:
                lot.buy_now_price = None

            if errors:
                return JsonResponse({"success": False, "errors": errors})

            # Save the lot - locking is handled in Lot.save() for both standard and seller_dash modes
            lot.save()

            # The species half of the save, and only once the row has really been saved: a row that
            # bounced on its price is not somebody's answer about what the fish is.
            if self.auction.use_scientific_name and lot.lot_name:
                # What the seller did with the answer this name was remembered as: left it alone,
                # cleared it with the X, or picked something else.  This is the half that was
                # missing -- the page wrote to a site-wide cache on a first save and never reported
                # back, so one misclick was the site's answer for good.  Before remember() below,
                # deliberately: the person teaching the site a pairing must not also be counted as a
                # second person agreeing with it.  See species_matching.record_choice.
                record_species_choice(
                    lot.lot_name, lot.species, first_save=is_new, changed=lot.species_id != species_before
                )
                # Remember it only on the row's first save, where the name and the species were
                # entered together and the pairing is really what the person meant.  On a later edit
                # they may well have rewritten the lot name and left the old species sitting there,
                # and the cache is global -- one stale row would teach every club that "sponge
                # filter" is a guppy.
                if is_new and lot.species:
                    remember_species(lot.lot_name, lot.species, source="user", user=request.user)

            # Create auction history entry
            if is_new:
                # New lot created
                AuctionHistory.objects.create(
                    auction=self.auction,
                    user=request.user,
                    action=f"created lot #{lot.lot_number_display}: {lot.lot_name}",
                    applies_to="LOTS",
                )
            else:
                # Check if lot is more than 20 minutes old
                lot_age = timezone.now() - lot.date_posted
                if lot_age.total_seconds() > 1200:  # 20 minutes in seconds
                    AuctionHistory.objects.create(
                        auction=self.auction,
                        user=request.user,
                        action=f"edited lot #{lot.lot_number_display}: {lot.lot_name}",
                        applies_to="LOTS",
                    )

            # Update invoice
            invoice = Invoice.objects.filter(auctiontos_user=self.tos, auction=self.auction).first()
            if not invoice:
                invoice = Invoice.objects.create(auctiontos_user=self.tos, auction=self.auction)
            invoice.recalculate()

            return JsonResponse(
                {
                    "success": True,
                    "lot_id": lot.lot_number,
                    "lot_number_display": lot.lot_number_display,
                    "lot_link": lot.lot_link,
                    "lot_pk": lot.pk,
                    "is_new": is_new,
                    "admin_bypassed_lot_limit": admin_bypassed_lot_limit,
                    "admin_bypassed_selling_allowed": admin_bypassed_selling_allowed,
                }
            )

        except json.JSONDecodeError:
            return JsonResponse({"success": False, "error": "Invalid JSON data"})
        except Exception:
            logger.exception("Failed to save lot via lot modal for auction %s", self.auction.pk)
            return JsonResponse({"success": False, "error": "Unable to save lot."})

    def dispatch(self, request, *args, **kwargs):
        # Let DRF's IsAuthenticated permission return a clean 401/403 instead of crashing
        # on request.user.email below when an unauthenticated (e.g. expired session) request comes in
        if not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)
        self.get_auction(kwargs.pop("slug", ""))

        # Get bidder_number from POST data if present (for admin adding lots for specific user)
        bidder_number = None
        if request.method == "POST":
            try:
                data = json.loads(request.body)
                bidder_number = data.get("bidder_number")
            except (json.JSONDecodeError, AttributeError):
                pass

        # Security check: Only admins can specify a bidder_number
        self.is_admin = self.is_auction_admin
        if bidder_number and not self.is_admin:
            return JsonResponse({"success": False, "error": "Only auction admins can add lots for other users"})

        # Get the TOS - either for specified bidder or for current user
        if bidder_number:
            self.tos = AuctionTOS.objects.filter(bidder_number=bidder_number, auction=self.auction).first()
            if not self.tos:
                return JsonResponse({"success": False, "error": "User not found in this auction"})
        else:
            self.tos = (
                AuctionTOS.objects.filter(auction=self.auction)
                .filter(Q(email=request.user.email) | Q(user=request.user))
                .first()
            )

        if not self.tos:
            return JsonResponse({"success": False, "error": "You must join this auction first"})

        if not self.tos.selling_allowed and not self.is_admin:
            return JsonResponse({"success": False, "error": "You don't have permission to add lots"})

        if not self.is_admin and not self.auction.can_submit_lots:
            return JsonResponse({"success": False, "error": "Lot submission has ended"})

        return super().dispatch(request, *args, **kwargs)


class ImportLotsFromCSV(LoginRequiredMixin, CSVContactImportMixin, AuctionViewMixin, View):
    """Import or update lots from a CSV file.

    Each row either updates an existing lot (matched by lot number) or creates a lot under a seller (an
    AuctionTOS, matched/created by normalized email then name). The shared preview surfaces lots to
    create/update, seller possible-duplicates (merge into existing vs create new), and skipped rows with
    reasons before anything is written."""

    import_record_kind = "lot"
    import_supports_duplicates = True
    import_preview_columns = (
        ("Lot #", "lot_number"),
        ("Lot name", "lot_name"),
        ("Seller", "name"),
        ("Email", "email"),
    )

    LOT_NUMBER_FIELDS = ["lot number", "lot_number", "lot #", "number"]
    EMAIL_FIELDS = ["email", "e-mail", "email address", "e-mail address"]
    NAME_FIELDS = ["name", "full name", "first name", "firstname", "bidder name"]
    LOT_NAME_FIELDS = ["lot name", "lot_name", "item", "item name"]
    DESCRIPTION_FIELDS = ["description", "desc", "details"]
    QUANTITY_FIELDS = ["quantity", "qty", "amount"]
    RESERVE_PRICE_FIELDS = ["reserve price", "reserve_price", "minimum bid", "min bid", "starting bid"]
    BUY_NOW_PRICE_FIELDS = ["buy now price", "buy_now_price", "buy now", "buynow"]
    CATEGORY_FIELDS = ["category", "species category", "species_category"]
    BRED_FIELDS = ["breeder points", "i bred this fish", "i_bred_this_fish", "bred"]
    DONATION_FIELDS = ["donation", "donate"]

    def import_target_id(self):
        return f"auction:{self.auction.pk}"

    def import_done_url(self):
        return reverse("auction_lot_list", kwargs={"slug": self.auction.slug})

    def import_cancel_url(self):
        return reverse("auction_lot_list", kwargs={"slug": self.auction.slug})

    def get(self, request, *args, **kwargs):
        preview_token = request.GET.get("preview")
        if preview_token:
            return self.render_preview(preview_token)
        return self._hx_aware_redirect(self.import_cancel_url())

    def post(self, request, *args, **kwargs):
        import_response = self.handle_import_post(request)
        if import_response is not None:
            return import_response
        csv_file = request.FILES.get("csv_file", None)
        if not csv_file:
            messages.error(request, "No CSV file provided")
            return self._hx_aware_redirect(self.import_cancel_url())
        return self.handle_csv_upload(csv_file)

    def _custom_field_specs(self):
        """Resolve the auction's configurable custom-field column names + valid dropdown values."""
        checkbox_fields = ["custom checkbox", "custom_checkbox"]
        if self.auction.use_custom_checkbox_field and self.auction.custom_checkbox_name:
            checkbox_fields.append(self.auction.custom_checkbox_name.lower())
        field_1_fields = ["custom field", "custom_field_1", "custom field 1"]
        if self.auction.custom_field_1 != "disable" and self.auction.custom_field_1_name:
            field_1_fields.append(self.auction.custom_field_1_name.lower())
        dropdown_fields = ["custom dropdown", "custom_dropdown"]
        if self.auction.custom_dropdown_name:
            dropdown_fields.append(self.auction.custom_dropdown_name.lower())
        dropdown_options = set()
        if self.auction.use_custom_dropdown_field != "disable" and self.auction.custom_dropdown_name:
            dropdown_options = set(AuctionDropdown.objects.filter(auction=self.auction).values_list("value", flat=True))
            if len(dropdown_options) < 2:
                dropdown_options = set()
        return checkbox_fields, field_1_fields, dropdown_fields, dropdown_options

    @staticmethod
    def _to_int(value, default=None):
        try:
            return int(value) if value else default
        except (TypeError, ValueError):
            return default

    def _parse_lot_row(self, row):
        checkbox_fields, field_1_fields, dropdown_fields, dropdown_options = self._custom_field_specs()
        custom_dropdown = self.extract_csv_field(row, dropdown_fields)
        if custom_dropdown not in dropdown_options:
            custom_dropdown = ""
        category_name = self.extract_csv_field(row, self.CATEGORY_FIELDS)
        category = Category.objects.filter(name__iexact=category_name).first() if category_name else None
        return {
            "lot_number": self.extract_csv_field(row, self.LOT_NUMBER_FIELDS),
            "email": normalize_email(self.extract_csv_field(row, self.EMAIL_FIELDS))[:254],
            "name": self.extract_csv_field(row, self.NAME_FIELDS)[:181],
            "lot_name": self.extract_csv_field(row, self.LOT_NAME_FIELDS)[:40],
            "description": self.extract_csv_field(row, self.DESCRIPTION_FIELDS),
            "quantity": self._to_int(self.extract_csv_field(row, self.QUANTITY_FIELDS, "1"), 1),
            "reserve_price": self._to_int(self.extract_csv_field(row, self.RESERVE_PRICE_FIELDS)),
            "buy_now_price": self._to_int(self.extract_csv_field(row, self.BUY_NOW_PRICE_FIELDS)),
            "category_id": category.pk if category else None,
            # Tri-state: None when the row didn't say, so an update can't silently clear a flag that
            # changes the invoice (breeder points, donations) just because the column was left blank.
            "i_bred_this_fish": self.parse_csv_boolean(self.extract_csv_field(row, self.BRED_FIELDS)),
            "donation": self.parse_csv_boolean(self.extract_csv_field(row, self.DONATION_FIELDS)),
            "custom_checkbox": self.parse_csv_boolean(self.extract_csv_field(row, checkbox_fields)),
            "custom_field_1": self.extract_csv_field(row, field_1_fields)[:60],
            "custom_dropdown": custom_dropdown,
        }

    def _find_lot(self, lot_number):
        if not lot_number:
            return None
        lot = Lot.objects.exclude(is_deleted=True).filter(auction=self.auction, custom_lot_number=lot_number).first()
        if lot:
            return lot
        lot_number_int = self._to_int(lot_number)
        if lot_number_int is not None:
            return (
                Lot.objects.exclude(is_deleted=True).filter(auction=self.auction, lot_number_int=lot_number_int).first()
            )
        return None

    @staticmethod
    def _seller_label(tos):
        label = tos.name or "(no name)"
        if tos.email:
            return f"{label} ({tos.email})"
        return label

    def _seller_invoice_open(self, tos):
        invoice = Invoice.objects.filter(auctiontos_user=tos, auction=self.auction).first()
        return invoice is None or invoice.status == "DRAFT"

    def plan_row(self, row):
        fields = self._parse_lot_row(row)
        base = {"fields": fields, "target_pk": None, "target_display": "", "match_type": None, "seller_pk": None}
        # Step 1: update an existing lot matched by lot number (no seller involved)
        lot = self._find_lot(fields["lot_number"])
        if lot:
            return {**base, "action": "update", "target_pk": lot.pk, "reason": "Update existing lot"}
        # Step 2: a new lot needs both a seller name and email
        if not fields["name"] or not fields["email"]:
            return {**base, "action": "skip", "reason": "Missing lot number and complete bidder information"}
        if not fields["lot_name"]:
            return {**base, "action": "skip", "reason": "Missing required lot information (lot name)"}
        # Step 3: resolve the seller — exact email match attaches silently; a name-only match is a duplicate
        seller_by_email = self.auction.find_user(email=fields["email"])
        if seller_by_email:
            if not self._seller_invoice_open(seller_by_email):
                return {**base, "action": "skip", "reason": f"{seller_by_email.name}'s invoice is not open"}
            return {
                **base,
                "action": "create",
                "seller_pk": seller_by_email.pk,
                "reason": "New lot for existing seller",
            }
        seller_by_name = self.auction.find_user(name=fields["name"])
        if seller_by_name:
            if not self._seller_invoice_open(seller_by_name):
                return {**base, "action": "skip", "reason": f"{seller_by_name.name}'s invoice is not open"}
            return {
                **base,
                "action": "duplicate",
                "target_pk": seller_by_name.pk,
                "seller_pk": seller_by_name.pk,
                "target_display": self._seller_label(seller_by_name),
                "match_type": "name",
                "reason": "Seller name matches an existing user",
            }
        return {**base, "action": "create", "reason": "New lot and new seller"}

    def _update_lot(self, lot, fields):
        # Don't touch winner, winning_price, partial_refund, banned — only the importable fields.
        if fields.get("lot_name"):
            lot.lot_name = fields["lot_name"]
        if fields.get("description"):
            lot.summernote_description = fields["description"]
        if fields.get("quantity"):
            lot.quantity = fields["quantity"]
        if fields.get("reserve_price") is not None:
            lot.reserve_price = fields["reserve_price"]
        if fields.get("buy_now_price") is not None:
            lot.buy_now_price = fields["buy_now_price"]
        if fields.get("category_id"):
            lot.species_category_id = fields["category_id"]
        # Only when the row actually said yes or no; a blank cell (or a file with no such column at all)
        # leaves the lot's current flag alone instead of clearing it.
        for field_name in ("i_bred_this_fish", "donation", "custom_checkbox"):
            value = fields.get(field_name)
            if value is not None:
                setattr(lot, field_name, value)
        if fields.get("custom_field_1"):
            lot.custom_field_1 = fields["custom_field_1"]
        if fields.get("custom_dropdown"):
            lot.custom_dropdown = fields["custom_dropdown"]
        lot.save()

    def _resolve_seller(self, fields, action, decision):
        """Return (seller, created_bool). Reuses the matched seller unless the admin chose to create a new
        record for a name-match duplicate."""
        seller_pk = action.get("seller_pk")
        make_new = action["action"] == "duplicate" and decision == "create"
        if seller_pk and not make_new:
            seller = AuctionTOS.objects.filter(pk=seller_pk, auction=self.auction).first()
            if seller:
                return seller, False
        name = fields.get("name", "")
        email = fields.get("email", "")
        # In a club-managed auction the club owns the bidder number, so an imported seller needs a
        # member record like any other participant. Creating it also creates the participant row
        # (signals), so adopt that instead of adding a second one for the same person.
        member, _created = ensure_club_member(self.auction, name=name, email=email)
        adopted = existing_tos_for_club_member(self.auction, member)
        if adopted is not None:
            return adopted, True
        seller = AuctionTOS(
            auction=self.auction,
            pickup_location=self.auction.location_qs.first(),
            manually_added=True,
            name=name,
            email=email,
        )
        apply_club_member_to_tos(self.auction, seller, member)
        seller.save()
        return seller, True

    def _create_lot(self, fields, seller):
        new_lot = Lot(
            lot_name=fields.get("lot_name", ""),
            summernote_description=fields.get("description") or "",
            quantity=fields.get("quantity") or 1,
            reserve_price=(
                fields["reserve_price"] if fields.get("reserve_price") is not None else self.auction.minimum_bid
            ),
            buy_now_price=fields.get("buy_now_price"),
            i_bred_this_fish=bool(fields.get("i_bred_this_fish")),
            donation=bool(fields.get("donation")),
            custom_checkbox=bool(fields.get("custom_checkbox")),
            custom_field_1=fields.get("custom_field_1", ""),
            custom_dropdown=fields.get("custom_dropdown", ""),
            auctiontos_seller=seller,
            auction=self.auction,
            added_by=self.request.user,
        )
        owner = seller.lot_owner(self.request.user)
        if owner:
            new_lot.user = owner
        if fields.get("category_id"):
            new_lot.species_category_id = fields["category_id"]
        new_lot.save()

    def apply_action(self, action, decision):
        kind = action["action"]
        if kind == "skip":
            return "skipped"
        fields = action.get("fields", {})
        if kind == "update":
            lot = Lot.objects.exclude(is_deleted=True).filter(pk=action.get("target_pk"), auction=self.auction).first()
            if not lot:
                return "skipped"
            self._update_lot(lot, fields)
            return "updated"
        # create or duplicate → resolve the seller, re-check the invoice, create the lot
        seller, created_seller = self._resolve_seller(fields, action, decision)
        if not self._seller_invoice_open(seller):
            return "skipped"
        if created_seller:
            self._users_created = getattr(self, "_users_created", 0) + 1
        self._create_lot(fields, seller)
        return "created"

    def message_import_results(self, results):
        parts = []
        if results.get("created"):
            parts.append(f"{results['created']} lots created")
        if results.get("updated"):
            parts.append(f"{results['updated']} lots updated")
        users_created = getattr(self, "_users_created", 0)
        if users_created:
            parts.append(f"{users_created} users added")
        if results.get("skipped"):
            parts.append(f"{results['skipped']} rows skipped")
        if parts:
            messages.success(self.request, ", ".join(parts))

    def record_import_history(self, results, filename=None):
        if not results.get("created") and not results.get("updated"):
            return
        history_msg = f"CSV import: {results.get('created', 0)} lots created, {results.get('updated', 0)} lots updated"
        users_created = getattr(self, "_users_created", 0)
        if users_created:
            history_msg += f", {users_created} users added"
        if filename:
            history_msg += f" from {filename}"
        self.auction.create_history(applies_to="LOTS", action=history_msg, user=self.request.user)

    def process_csv_data(self, csv_reader, filename=None):
        """Parse the upload into planned actions and show the review page; nothing is written yet."""
        token = self.build_preview(csv_reader, filename=filename)
        return self.redirect_to_preview(token)
