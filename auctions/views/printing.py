"""Labels: what gets drawn on them, and getting them to a printer.

``LotLabelView`` renders the label sheet and is also where the app's Bluetooth print handoff is
emitted, so the printing templates themselves must stay free of any "is this the app" test. The
remote-print views below it are the queue for a printer somebody else's browser is holding open.
"""

import logging
import re
from io import BytesIO

import qr_code
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models.base import Model as Model
from django.http import (
    HttpResponse,
    HttpResponseBadRequest,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.views.generic import TemplateView, View
from django_weasyprint import WeasyTemplateResponseMixin
from qr_code.qrcode.utils import QRCodeOptions
from reportlab.platypus import (
    Image as PImage,
)

from auctions.models import (
    Auction,
    AuctionTOS,
    Lot,
    RemotePrintJob,
    UserLabelPrefs,
)

from .base import AuctionViewMixin

logger = logging.getLogger(__name__)


class LotLabelView(TemplateView, WeasyTemplateResponseMixin, AuctionViewMixin):
    """View and print labels for an auction"""

    # these are defined in urls.py and used in get_object(), below
    bidder_number = None
    username = None
    # This one is the old one, it has some good stuff in it like QR code
    # template_name = "invoice_labels.html"
    template_name = "label_template.html"
    allow_non_admins = True
    filename = ""  # this will be automatically generated in dispatch
    # Rendering a label sheet is what marks its labels printed. That is right for a PDF the user is
    # about to send to a printer, and wrong for the PNG raster path, where the label is only being
    # *drawn* -- the app posts labels/printed/ for the ones that actually come out.
    mark_labels_printed = True
    # Size the page to a single label instead of a sheet. The PNG raster is a picture of one label,
    # so an Avery preset's 8.5x11 page would otherwise come out as a label in the corner of a mostly
    # blank image. A preset whose page already holds exactly one label (both thermal presets) is
    # left exactly as it is, so the raster stays a pixel-for-pixel view of the PDF.
    single_label_page = False
    # A custom-scheme URL has no standard length limit and the app has no cap of its own (it prints
    # serially and cancellably), but platform URL handling varies, so keep the deep link near 2000
    # characters. The PDF path's own 100-label cap does not apply -- nothing is being laid out on a
    # page here.
    MAX_DEEP_LINK_LOTS = 300
    # Tuned for known overflow breakpoints (long seller emails/lot numbers) per label preset.
    # shrink_threshold: start scaling after this length.
    # ratio_base: numerator for ratio_base / text_length scaling.
    # min_ratio: floor so text stays readable.
    SELLER_EMAIL_FONT_CONFIG = {
        "sm": {"shrink_threshold": 18, "ratio_base": 14, "min_ratio": 0.6},
        "lg": {"shrink_threshold": 20, "ratio_base": 15, "min_ratio": 0.55},
        "thermal_sm": {"shrink_threshold": 17, "ratio_base": 13, "min_ratio": 0.45},
        "thermal_very_sm": {"shrink_threshold": 13, "ratio_base": 10, "min_ratio": 0.4},
    }
    LOT_NUMBER_FONT_CONFIG = {
        "sm": {"shrink_threshold": 6, "ratio_base": 4.2, "min_ratio": 0.6},
        "lg": {"shrink_threshold": 6, "ratio_base": 4.8, "min_ratio": 0.6},
        "thermal_sm": {"shrink_threshold": 5, "ratio_base": 3.5, "min_ratio": 0.45},
        "thermal_very_sm": {"shrink_threshold": 4, "ratio_base": 3, "min_ratio": 0.4},
    }

    def get_queryset(self):
        return self.tos.print_labels_qs

    def dispatch(self, request, *args, **kwargs):
        # check to make sure the user has permission to view this invoice
        self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=kwargs["slug"]).first()
        self.bidder_number = kwargs.pop("bidder_number", None)
        self.username = kwargs.pop("username", None)
        printing_for_self = False
        if self.bidder_number:
            self.tos = AuctionTOS.objects.filter(auction=self.auction, bidder_number=self.bidder_number).first()
        if self.username:
            self.tos = AuctionTOS.objects.filter(auction=self.auction, user__username=self.username).first()
        if not self.bidder_number and not self.username:
            self.tos = AuctionTOS.objects.filter(auction=self.auction, user=request.user).first()
        if not self.tos:
            if self.is_auction_admin:
                # should never get here as long as admins are following links
                messages.error(request, "Unable to find any labels to print.")
            else:
                messages.error(
                    request,
                    "You haven't joined this auction yet.  You need to join this auction and add lots before you can print labels.",
                )
            return redirect(self.auction.get_absolute_url())
        checks_pass = False
        if self.is_auction_admin:
            checks_pass = True
            # if this is an admin printing someone else's lots, the file name should be the name of the person whose lots they're printing
            self.filename = self.tos.name or self.tos.bidder_number
        if request.user.is_authenticated:
            if request.user == self.tos.user:
                printing_for_self = True
                checks_pass = True
                # if this is a user printing their own lots, the file name should be the name of the auction
                self.filename = str(self.auction)
        if printing_for_self:
            if self.auction.is_online and not self.auction.closed:
                messages.error(
                    request,
                    "This is an online auction; you should print your labels after the auction ends, and before you exchange lots.",
                )
                return redirect(self.auction.get_absolute_url())
        if checks_pass and self.tos:
            if not self.get_queryset():
                if printing_for_self:
                    messages.error(
                        request,
                        "You don't have any lots with printable labels in this auction.",
                    )
                else:
                    if not self.auction.is_online:
                        messages.error(request, "There aren't any lots with printable labels")
                    else:
                        messages.error(
                            request,
                            "No lots with printable labels.  Only lots with a winner will have a label generated for them.",
                        )
                return redirect(self.auction.get_absolute_url())
            return super().dispatch(request, *args, **kwargs)
        else:
            messages.error(request, "Your account doesn't have permission to view this page.")
            return redirect(reverse("home"))

    def get_pdf_filename(self):
        label_name = re.sub(r"[^a-zA-Z0-9]", "_", (self.filename or "labels").lower())
        return f"{label_name}.pdf"

    def get(self, request, *args, **kwargs):
        """Three ways to print, in the order they get asked.

        Gated here rather than in the templates that build bulk label links, because every bulk
        entry point funnels through this view -- the users-table anchors, ``?printredirect=``, the
        command palette, print-after-bulk-add, a bookmarked URL -- and gating them one at a time
        leaves entry points behind (it also keeps label printing out of the mobile-app UA
        conditionals the templates are deliberately free of; see
        MobileAppLabelPrintingVisibilityTests).

        1. ``?pdf=1`` -- the escape hatch, and it is checked first so it can never loop back into a
           branch. It is what the remote-print waiting page's "Print a PDF here" button links to,
           and the app arm has to respect it too or that button would bounce a phone straight back
           to the deep link it was trying to get out of.
        2. The app printing over Bluetooth: a deep link to its own printer. First of the two real
           arms, so somebody printing *from the phone* prints directly instead of routing a job
           through FCM back to the phone they are holding.
        3. A computer, with ``print_from_computer`` on and a phone that is actually reachable: a job
           pushed to that phone plus a page that waits on it.

        Everyone else gets the PDF, unchanged.

        All of it deliberately before ``get_context_data``, which marks labels printed as a side
        effect of rendering: in arms 2 and 3 nothing has printed yet, and what actually came out is
        reported afterwards (``labels/printed/`` for the deep link, the job's result post for a job).
        """
        if request.GET.get("pdf"):
            return super().get(request, *args, **kwargs)
        deep_link_response = self.bluetooth_deep_link_response()
        if deep_link_response is not None:
            return deep_link_response
        remote_print_response = self.remote_print_response()
        if remote_print_response is not None:
            return remote_print_response
        return super().get(request, *args, **kwargs)

    def remote_print_response(self):
        """The waiting page for a job pushed to the user's phone, or None to render the PDF.

        Only from a *computer*: in the app, arm 2 above has already had its say, and a phone that
        prints its own labels needs no job. ``can_print_from_computer`` is both halves of the
        question -- the preference is on, and a phone is heartbeating with a printer paired -- because
        a page that promises a print the phone cannot deliver is exactly what this feature is built to
        avoid.
        """
        from auctions.mobile.services import remote_print

        request = self.request
        if getattr(request, "is_mobile_app", False) or not request.user.is_authenticated:
            return None
        if not remote_print.can_print_from_computer(request.user):
            return None
        # Same queryset and order as the PDF, which is the order they come out of the printer.
        pks = list(self.get_queryset().values_list("pk", flat=True))
        if not pks:
            return None
        job = remote_print.start(request.user, pks)
        context = {
            "job": job,
            "label_count": job.total_count,
            # A batch bigger than one push can carry; the rest is a second run, the same way the
            # deep-link path splits one.
            "truncated_count": len(pks) if len(pks) > job.total_count else 0,
            "printer_name": job.device.printer_name if job.device else "",
            "pdf_url": self.request.get_full_path() + ("&" if self.request.GET else "?") + "pdf=1",
            "back_url": self.auction.get_absolute_url() if self.auction else reverse("selling"),
        }
        return render(self.request, "label_remote_print.html", context)

    def bluetooth_deep_link_response(self):
        """The ``fishauctions://print/?lots=…`` handoff page, or None to render the PDF."""
        if not getattr(self.request, "is_mobile_app", False) or not self.request.user.is_authenticated:
            return None
        prefs = UserLabelPrefs.objects.filter(user=self.request.user).first()
        if not prefs or prefs.print_method != "bluetooth":
            return None
        # Same queryset, same order as the PDF prints them -- that's the order they come out of the
        # printer.
        pks = list(self.get_queryset().values_list("pk", flat=True))
        if not pks:
            return None
        truncated_count = len(pks) if len(pks) > self.MAX_DEEP_LINK_LOTS else 0
        pks = pks[: self.MAX_DEEP_LINK_LOTS]
        context = {
            "deep_link": "fishauctions://print/?lots=" + ",".join(str(pk) for pk in pks),
            "label_count": len(pks),
            "truncated_count": truncated_count,
            "back_url": self.auction.get_absolute_url() if self.auction else reverse("selling"),
        }
        return render(self.request, "label_bluetooth_redirect.html", context)

    @staticmethod
    def get_seller_email_font_size(seller_email, preset):
        """Shrink seller email font for configured label presets when needed."""
        if not seller_email:
            return None
        config = LotLabelView.SELLER_EMAIL_FONT_CONFIG.get(preset)
        if not config:
            return None
        if len(seller_email) <= config["shrink_threshold"]:
            return None
        font_ratio = max(config["min_ratio"], config["ratio_base"] / len(seller_email))
        return f"{font_ratio:.2f}em"

    @staticmethod
    def get_lot_number_font_size(lot_number_display, preset):
        """Shrink lot number font for configured label presets when needed."""
        if not lot_number_display:
            return None
        lot_number_display = str(lot_number_display)
        config = LotLabelView.LOT_NUMBER_FONT_CONFIG.get(preset)
        if not config:
            return None
        if len(lot_number_display) <= config["shrink_threshold"]:
            return None
        font_ratio = max(config["min_ratio"], config["ratio_base"] / len(lot_number_display))
        return f"{font_ratio:.2f}em"

    def get_context_data(self, **kwargs):
        user_label_prefs, created = UserLabelPrefs.objects.get_or_create(user=self.request.user)
        context = {}
        context["empty_labels"] = user_label_prefs.empty_labels
        context["print_border"] = user_label_prefs.print_border
        context["first_column_width"] = 0.62
        if user_label_prefs.preset == "sm":
            # Avery 5160 labels
            context["page_width"] = 8.5
            context["page_height"] = 11
            context["label_width"] = 2.55
            context["label_height"] = 0.99
            context["label_margin_right"] = 0.19
            context["label_margin_bottom"] = 0.01
            context["page_margin_top"] = 0.57
            context["page_margin_bottom"] = 0.1
            context["page_margin_left"] = 0.23
            context["page_margin_right"] = 0
            context["font_size"] = 10
            context["unit"] = "in"
        elif user_label_prefs.preset == "lg":
            # Avery 18262 labels
            context["page_width"] = 8.5
            context["page_height"] = 11
            context["label_width"] = 3.85
            context["label_height"] = 1.2
            context["label_margin_right"] = 0.25
            context["label_margin_bottom"] = 0.13
            context["page_margin_top"] = 0.88
            context["page_margin_bottom"] = 0.6
            context["page_margin_left"] = 0.3
            context["page_margin_right"] = 0
            context["font_size"] = 13
            context["first_column_width"] = 0.75
            context["unit"] = "in"
        elif user_label_prefs.preset == "thermal_sm":
            # thermal label printer 3x2
            context["page_width"] = 3
            context["page_height"] = 2
            context["label_width"] = 2.78
            context["label_height"] = 1.9
            context["label_margin_right"] = 0
            context["label_margin_bottom"] = 0
            context["page_margin_top"] = 0.04
            context["page_margin_bottom"] = 0.04
            context["page_margin_left"] = 0.16
            context["page_margin_right"] = 0.04
            context["font_size"] = 13
            context["first_column_width"] = 0.75
            context["unit"] = "in"
            # override the user selected setting for thermal labels
            context["print_border"] = False
        elif user_label_prefs.preset == "thermal_very_sm":
            # thermal label printer 30252 (1 1/8" x 3 1/2")
            context["page_width"] = 3.5
            context["page_height"] = 1.125
            context["label_width"] = 3.3
            context["label_height"] = 1.025
            context["label_margin_right"] = 0
            context["label_margin_bottom"] = 0
            context["page_margin_top"] = 0.04
            context["page_margin_bottom"] = 0.04
            context["page_margin_left"] = 0.16
            context["page_margin_right"] = 0.04
            context["font_size"] = 12
            context["first_column_width"] = 0.75
            context["unit"] = "in"
            context["print_border"] = False
        else:
            context.update(
                {f"{field.name}": getattr(user_label_prefs, field.name) for field in UserLabelPrefs._meta.get_fields()}
            )
        unit = 2.54 if context.get("unit") == "cm" else 1

        context["label_width"] = context.get("label_width") * unit
        context["label_height"] = context.get("label_height") * unit
        context["label_margin_right"] = context.get("label_margin_right") * unit
        context["label_margin_bottom"] = context.get("label_margin_bottom") * unit

        context["page_margin_top"] = context.get("page_margin_top") * unit
        context["page_margin_bottom"] = context.get("page_margin_bottom") * unit
        context["page_margin_left"] = context.get("page_margin_left") * unit
        context["page_margin_right"] = context.get("page_margin_right") * unit

        context["page_width"] = context.get("page_width") * unit
        context["page_height"] = context.get("page_height") * unit

        # Calculate the available space on the page
        available_width = context["page_width"] - context["page_margin_left"] - context["page_margin_right"]

        available_height = context["page_height"] - context["page_margin_top"] - context["page_margin_bottom"]

        # Page breaks don't work, see https://github.com/Kozea/WeasyPrint/issues/1967
        # manually calculating
        labels_per_row = int(available_width // (context["label_width"] + context["label_margin_right"]))
        labels_per_column = int(available_height // (context["label_height"] + context["label_margin_bottom"]))
        context["labels_per_page"] = labels_per_row * labels_per_column

        if self.single_label_page and context["labels_per_page"] != 1:
            # Shrink the page onto the label. Only reached for sheet presets (and a custom size that
            # tiles): the thermal presets already describe one physical label per page and keep
            # their exact geometry.
            #
            # The page margins go with it. On a sheet they are the unprintable border of a sheet of
            # Avery stock -- keeping them here would print the label offset into a corner with a
            # wide blank margin above and to the left of it, which on a label roll is just wasted
            # label.
            for margin in ("page_margin_top", "page_margin_bottom", "page_margin_left", "page_margin_right"):
                context[margin] = 0
            context["page_width"] = context["label_width"]
            context["page_height"] = context["label_height"]
            context["labels_per_page"] = 1

        if context["labels_per_page"] == 0:
            messages.error(
                self.request,
                mark_safe(
                    "Your lot label setting may be wrong. The label size is too large for the page size.  <a href='/printing'>Adjust your label settings</a>"
                ),
                extra_tags="safe",
            )
            context["labels_per_page"] = 1

        labels = self.get_queryset().select_related(
            "auction",
            "auctiontos_seller",
            "auctiontos_winner",
            "auctiontos_winner__pickup_location",
            "species_category",
            "species",
            # A strain with no common name of its own falls back to its parent's, which the label
            # prints -- see Lot.common_name_line.  One join rather than a query per label.
            "species__parent",
            "user",
        )

        # Cap thermal labels at 100 per PDF
        is_thermal = user_label_prefs.preset in ["thermal_sm", "thermal_very_sm"]

        if is_thermal:
            # Check if we have more than 100 labels efficiently
            # We fetch 101 labels to determine if there are more than 100
            labels_list = list(labels[:101])
            if len(labels_list) > 100:
                # Show warning and limit to first 100
                total_labels_count = labels.count()
                labels = labels_list[:100]
                messages.warning(
                    self.request,
                    f"Only the first 100 labels are included in this PDF (you have {total_labels_count} total labels). "
                    f"To print the remaining labels, use the 'Print unprinted labels' option.",
                )
            else:
                # Use the list we already fetched (100 or fewer labels)
                labels = labels_list
        else:
            labels = list(labels)

        if self.mark_labels_printed:
            for label in labels:
                label.label_printed = True
                label.label_needs_reprinting = False
            Lot.objects.bulk_update(labels, ["label_printed", "label_needs_reprinting"])

        # First column width is fixed at 0.63 for most labels and overridden for large and thermal
        # context['first_column_width'] = (context['label_width'] / 4)
        # let's keep the QR code a fixed size regardless of the label size
        # context['qr_code_height'] = min(context['first_column_width'], context['label_height'] / 2)
        context["qr_code_height"] = 0.5 * 72
        height_for_text = context["label_height"] * 72
        if "qr_code" in self.auction.label_print_fields:
            height_for_text = height_for_text - context["qr_code_height"]
        leading_ratio = 1.3
        line_height = context["font_size"] * leading_ratio
        lines_that_fit = int(height_for_text / line_height * 1.2)
        lines_that_fit -= 1  # for the lot number
        first_column_fields = [
            "quantity_label",
            "donation_label",
            "min_bid_label",
            "buy_now_label",
            "custom_checkbox_label",
            "custom_dropdown_label",
            "i_bred_this_fish_label",
            "auction_date",
        ]
        first_column_fields_to_print = [
            field for field in first_column_fields if field in self.auction.label_print_fields
        ]
        # Split the fields: first column and overflow to second column
        first_column_fields = first_column_fields_to_print[:lines_that_fit]
        first_column_fields_to_put_in_second_column = first_column_fields_to_print[lines_that_fit:]

        for label in labels:
            label_first_column_fields = []
            label_second_column_fields = []
            for field in first_column_fields:
                label_first_column_fields.append(getattr(label, field))
            for field in first_column_fields_to_put_in_second_column:
                label_second_column_fields.append(getattr(label, field))
            label.first_column_fields = label_first_column_fields
            label.second_column_fields = label_second_column_fields
            label.seller_email_font_size = self.get_seller_email_font_size(label.seller_email, user_label_prefs.preset)
            label.lot_number_font_size = self.get_lot_number_font_size(
                label.lot_number_display, user_label_prefs.preset
            )
        context["labels"] = (["empty"] * context["empty_labels"]) + list(labels)
        context["text_area_width"] = context["label_width"] - context["first_column_width"]
        context["description_font_size"] = int(context["font_size"] * 0.7)
        context["first_column_font_size"] = int(context["font_size"] * 0.8)
        # for sizing
        context["all_borders"] = False
        return context

    def generate_qr_code(self, label, qr_code_width, qr_code_height):
        label_qr_code = qr_code.qrcode.maker.make_qr_code_image(
            label.qr_code,
            QRCodeOptions(
                size="T",
                border=1,
                error_correction="L",
                image_format="png",
            ),
        )
        image_stream = BytesIO(label_qr_code)
        return PImage(
            image_stream,
            width=qr_code_width,
            height=qr_code_height,
            lazy=0,
            hAlign="LEFT",
        )


class UnprintedLotLabelsView(LotLabelView):
    """Print lot labels, but only ones that haven't already been printed"""

    def get_queryset(self):
        return self.tos.unprinted_labels_qs


class SingleLotLabelView(LotLabelView):
    """Reprint labels for just one lot"""

    def get_queryset(self):
        return Lot.objects.filter(pk=self.lot.pk)

    def dispatch(self, request, *args, **kwargs):
        self.lot = get_object_or_404(Lot, pk=kwargs.pop("pk"), is_deleted=False)
        self.filename = f"label_{self.lot.lot_number_display}"
        if self.lot.auctiontos_seller:
            self.auction = self.lot.auctiontos_seller.auction
            if not self.lot.is_owned_by(request.user) and not self.is_auction_admin:
                messages.error(
                    request,
                    "You can't print labels for other people's lots unless you are an admin",
                )
                return redirect(reverse("home"))
        if not self.lot.auctiontos_seller:
            if self.lot.user and self.lot.user != request.user:
                messages.error(request, "You can only print labels for your own lots")
                return redirect(reverse("home"))
        # ?format=png (or ?fmt=png) returns a single rendered PNG via the shared label renderer
        # instead of the WeasyPrint PDF sheet, so the web endpoint matches the mobile app. Honors
        # ?resolution=WIDTHxHEIGHT&dpi=N (default 600x400 @ 203dpi).
        if (request.GET.get("format") or request.GET.get("fmt")) == "png":
            from auctions.mobile.services.labels import LabelService

            try:
                content, content_type = LabelService.render_label(
                    self.lot,
                    "png",
                    resolution=request.GET.get("resolution"),
                    dpi=request.GET.get("dpi"),
                    request=request,
                )
            except ValueError:
                logging.getLogger(__name__).warning(
                    "Invalid label rendering parameters for lot %s",
                    self.lot.pk,
                    exc_info=True,
                )
                return HttpResponseBadRequest("Invalid label rendering parameters.")
            return HttpResponse(content, content_type=content_type)
        # super() would try to find an auction
        return View.dispatch(self, request, *args, **kwargs)


class RemotePrintJobMixin(LoginRequiredMixin):
    """The job, scoped to the signed-in user.

    Session auth, not the mobile JWT: this half of the conversation is the *computer*, watching a job
    it started. 404 for somebody else's job -- the uuid is unguessable, and a 403 would only confirm
    that one exists.
    """

    def get_job(self, request, job_uuid):
        return get_object_or_404(RemotePrintJob, uuid=job_uuid, user=request.user)


class RemotePrintJobStatusView(RemotePrintJobMixin, View):
    """GET /printing/job/<uuid>/ — what the waiting page polls, once a second.

    Deliberately a plain JSON web view rather than a DRF endpoint: it is read by a page in the
    browser that started the job, on the session it already has.
    """

    def get(self, request, job_uuid):
        from auctions.mobile.services.remote_print import job_state

        return JsonResponse(job_state(self.get_job(request, job_uuid)))


class RemotePrintJobRetryView(RemotePrintJobMixin, View):
    """POST /printing/job/<uuid>/retry/ — "Try again": the same labels, a fresh job.

    A new row rather than a reset of the old one, because the old one is a record of something that
    really happened (and its phone may yet report on it). The lot list is copied from the job instead
    of re-derived from the queryset: a lot sold or deleted in between would silently shorten the
    batch, and the person is standing at the printer expecting the labels they asked for.
    """

    def post(self, request, job_uuid):
        from auctions.mobile.services import remote_print

        old_job = self.get_job(request, job_uuid)
        job = remote_print.create_job(request.user, old_job.lots)
        remote_print.dispatch(job)
        return JsonResponse({"job": str(job.uuid), **remote_print.job_state(job)})


class RemotePrintJobCancelView(RemotePrintJobMixin, View):
    """POST /printing/job/<uuid>/cancel/ — the user gave up on this one.

    Only the record is cancelled; a phone already feeding labels is not interrupted, because there is
    no channel to interrupt it with and it has its own Stop button next to the printer. What this
    does buy is that a late result post can no longer overwrite the answer the person chose.
    """

    def post(self, request, job_uuid):
        job = self.get_job(request, job_uuid)
        if not job.is_terminal:
            job.status = RemotePrintJob.STATUS_CANCELLED
            job.save(update_fields=["status", "updated_at"])
        return JsonResponse({"status": job.status})
