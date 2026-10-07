"""Bidder paddles: a sheet of paper per person, with their number on both halves, folded so it stands up.

Not labels. Nothing here uses the label settings, except to ask whether lot labels come out of a sheet
printer -- the one these go to -- so the page can ask for the label sheets to come out first. A printed
paddle is recorded on ``AuctionTOS`` as what it says, so a new number or name puts that person back in
"Not printed yet" however it changed.
"""

import io
import logging
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.staticfiles import finders
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.views.generic import TemplateView
from django_weasyprint import WeasyTemplateResponseMixin
from PIL import ImageFont

from auctions.models import AuctionTOS, UserLabelPrefs
from auctions.printing import THERMAL_PRESETS
from auctions.services import attachment_filename

from .base import AuctionViewMixin, check_club_permission

logger = logging.getLogger(__name__)

#: Sheets in one PDF. "Not printed yet" carries on from where the last one stopped.
MAX_PADDLES = 100
#: What a scanner reads as a bidder number, as on the club's paddle stickers.
BARCODE_PREFIX = "11111"
FONT = "fonts/BarlowCondensed-Bold.ttf"
#: Currencies of the countries on US Letter. Everywhere else prints on A4.
LETTER_CURRENCIES = frozenset({"USD", "CAD"})
PAPER_INCHES = {"letter": (8.5, 11.0), "A4": (210 / 25.4, 297 / 25.4)}
#: Clear of a printer's unprintable edge, and of the fold.
SIDE_MARGIN_INCHES = 0.3
NUMBER_MAX_PT = 260
NAME_MAX_PT = 40
NAME_MIN_PT = 14
#: What AuctionTOS.save() writes into a blank name or a number it couldn't generate.
NO_NAME = frozenset({"", "Unknown"})
NO_NUMBER = ("", "ERROR")


def paper_for(auction):
    return "letter" if auction.currency in LETTER_CURRENCIES else "A4"


def needs_paper_check(user):
    """Whether *user*'s lot labels come out of a sheet printer, which may still have label sheets in it."""
    preset = UserLabelPrefs.objects.filter(user=user).values_list("preset", flat=True).first()
    return (preset or UserLabelPrefs._meta.get_field("preset").default) not in THERMAL_PRESETS


@lru_cache(maxsize=1)
def font_path():
    return finders.find(FONT)


@lru_cache(maxsize=1)
def _font():
    return ImageFont.truetype(font_path(), 1000)


def width_in_ems(text):
    return max(_font().getlength(text) / 1000, 0.01)


def barcode_svg(value, max_width_mm):
    """*value* as Code 128 bars at their printed size, as inline SVG; "" if it can't be drawn."""
    try:
        import barcode
        from barcode.writer import SVGWriter

        code = barcode.get_barcode_class("code128")(value, writer=SVGWriter())
        # Half-millimetre bars scan from arm's length; a long number gets narrower ones rather than
        # running off the paddle. 20 is the quiet zone, ten bars' width each side.
        module_mm = min(0.5, max_width_mm / (len(code.build()[0]) + 20))
        buffer = io.BytesIO()
        code.write(
            buffer,
            options={
                "write_text": False,
                "module_width": module_mm,
                "module_height": 10.0,
                "quiet_zone": 10 * module_mm,
            },
        )
    except Exception:
        # A bidder number in letters Code 128 has no bars for, like "José": the paddle prints without one.
        logger.warning("Couldn't draw a paddle barcode for %r", value, exc_info=True)
        return ""
    svg = buffer.getvalue().decode("utf-8")
    # Bars only: the value is never written into the SVG, so nothing typed reaches the markup.
    return mark_safe(svg[svg.index("<svg") :])  # noqa: S308


def lay_out(tos, paper):
    """What one person's sheet says, and how big: the number as big as the half will hold."""
    width_in = PAPER_INCHES[paper][0] / 2 - 2 * SIDE_MARGIN_INCHES
    width_pt = width_in * 72
    name = "" if (tos.name or "").strip() in NO_NAME else tos.name
    return {
        "number": tos.bidder_number,
        "number_pt": round(min(NUMBER_MAX_PT, width_pt / width_in_ems(tos.bidder_number)), 1),
        "name": name,
        "name_pt": round(max(NAME_MIN_PT, min(NAME_MAX_PT, width_pt / width_in_ems(name))), 1),
        "barcode": barcode_svg(BARCODE_PREFIX + tos.bidder_number, width_in * 25.4),
    }


class PaddleViewMixin(LoginRequiredMixin, AuctionViewMixin):
    """Whoever runs the auction's people, as on the Users tab, and only for an in-person auction."""

    allow_non_admins = True

    def refusal(self):
        """A redirect away from an online auction, or None. Raises for someone who can't manage people."""
        _ = self.can_add_edit_people
        if self.auction.is_online:
            messages.info(self.request, "Paddles are for in-person auctions.")
            return redirect(self.auction.get_absolute_url())
        return None

    def chosen_person(self, value):
        try:
            return AuctionTOS.objects.filter(auction=self.auction, pk=int(value)).first()
        except (TypeError, ValueError):
            return None


class AuctionPaddles(PaddleViewMixin, TemplateView):
    """Choose whose paddles to print: whoever hasn't got a current one, everyone, or one person."""

    template_name = "auctions/auction_paddles.html"

    def get(self, request, *args, **kwargs):
        refusal = self.refusal()
        if refusal:
            return refusal
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        auction = self.auction
        club = auction.club
        context.update(
            auction=auction,
            unprinted_count=len(auction.unprinted_paddles()),
            everyone_count=auction.paddle_people_qs.count(),
            people=AuctionTOS.objects.filter(auction=auction)
            .exclude(bidder_number__in=NO_NUMBER)
            .order_by("name", "pk")
            .only("pk", "name", "bidder_number"),
            chosen=self.chosen_person(self.request.GET.get("tos")),
            needs_paper_check=needs_paper_check(self.request.user),
            max_paddles=MAX_PADDLES,
            barcodes_url=(
                reverse("club_barcode_labels", kwargs={"slug": club.slug})
                if club and check_club_permission(self.request.user, club, "permission_view")
                else ""
            ),
        )
        return context


class AuctionPaddlesPDF(PaddleViewMixin, WeasyTemplateResponseMixin, TemplateView):
    """One page per person. The form's choice is turned into the people it means and redirected to, so
    fetching the PDF a second time prints the same paddles, not whoever is left unprinted by then.
    """

    template_name = "auctions/auction_paddles_print.html"

    def get(self, request, *args, **kwargs):
        refusal = self.refusal()
        if refusal:
            return refusal
        params = request.GET
        back = reverse("auction_paddles", kwargs={"slug": self.auction.slug})
        if params.get("who") == "one" and params.get("tos"):
            back += "?" + urlencode({"tos": params["tos"]})
        if needs_paper_check(request.user) and not params.get("plain_paper"):
            messages.warning(request, "Put plain paper in the printer, then tick the box.")
            return redirect(back)
        if "people" not in params:
            people = self.choose(params)
            if not people:
                messages.info(request, "There are no paddles to print.")
                return redirect(back)
            query = {key: params[key] for key in ("plain_paper", "token") if params.get(key)}
            query["people"] = ",".join(str(tos.pk) for tos in people)
            return redirect(f"{request.path}?{urlencode(query)}")
        pks = [int(pk) for pk in params["people"].split(",") if pk.strip().isdigit()]
        self.people = list(
            AuctionTOS.objects.filter(auction=self.auction, pk__in=pks)
            .exclude(bidder_number__in=NO_NUMBER)
            .order_by("name", "pk")[:MAX_PADDLES]
        )
        if not self.people:
            messages.info(request, "There are no paddles to print.")
            return redirect(back)
        return super().get(request, *args, **kwargs)

    def choose(self, params):
        who = params.get("who")
        if who == "one":
            person = self.chosen_person(params.get("tos"))
            return [person] if person and person.bidder_number not in NO_NUMBER else []
        if who == "everyone":
            return list(self.auction.paddle_people_qs[:MAX_PADDLES])
        return self.auction.unprinted_paddles()[:MAX_PADDLES]

    def get_pdf_filename(self):
        if len(self.people) == 1:
            return attachment_filename(f"paddle-{self.people[0].bidder_number}", "paddle") + ".pdf"
        return attachment_filename(f"{self.auction.slug}-paddles", "paddles") + ".pdf"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        paper = paper_for(self.auction)
        width, height = PAPER_INCHES[paper]
        context.update(
            auction=self.auction,
            paddles=[lay_out(tos, paper) for tos in self.people],
            sides=("front", "back"),
            paper=paper,
            page_width=round(width, 3),
            page_height=round(height, 3),
            half_width=round(width / 2, 3),
            side_margin=SIDE_MARGIN_INCHES,
            font_url=Path(font_path()).as_uri(),
        )
        return context

    def render_to_response(self, context, **response_kwargs):
        response = super().render_to_response(context, **response_kwargs)
        people = self.people
        # After the PDF exists, so one that fails to render leaves everybody unprinted.
        response.add_post_render_callback(lambda _response: AuctionTOS.mark_paddles_printed(people))
        token = self.request.GET.get("token", "")
        if re.fullmatch(r"[a-z0-9]{1,32}", token):
            # The page reloads when it sees this, so its counts include what just printed.
            response.set_cookie("paddles_printed", token, max_age=120, samesite="Lax")
        return response
