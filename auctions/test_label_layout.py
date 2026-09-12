"""Every lot label preset, rendered with worst-case lots and held to the layout rules.

The rules are in :mod:`auctions.printing`'s docstring. These tests render the real
``label_template.html`` through WeasyPrint and read the laid-out boxes back, so "nothing runs into
anything else" and "the winner is always on the label" are checked against where the text actually
landed, clip rectangles included -- not against what the template or ``plan_label`` meant to do.
"""

import re
from dataclasses import dataclass
from datetime import timedelta

from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase
from django.utils import timezone
from weasyprint import HTML
from weasyprint.formatting_structure.boxes import TextBox

from auctions.mobile.services.label_pdf import build_label_view
from auctions.models import Lot, PickupLocation, UserLabelPrefs
from auctions.printing import split_label_tags, text_width_pt, wrapped_lines
from auctions.tests import StandardTestCase

ALL_LABEL_FIELDS = (
    "qr_code,lot_name,scientific_name,category,donation_label,min_bid_label,buy_now_label,custom_field_1,"
    "custom_checkbox_label,custom_dropdown_label,i_bred_this_fish_label,quantity_label,auction_date,"
    "seller_name,seller_email,description_label"
)
# A fraction of a CSS pixel, for layout rounding.
EPSILON = 0.5


@dataclass
class PlacedText:
    text: str
    rect: tuple
    clip: tuple

    @property
    def horizontally_whole(self):
        return self.rect[0] >= self.clip[0] - EPSILON and self.rect[2] <= self.clip[2] + EPSILON

    @property
    def visible(self):
        """Every bit of it is on the label: not cut by a column or the label's edge."""
        return (
            self.horizontally_whole
            and self.rect[1] >= self.clip[1] - EPSILON
            and self.rect[3] <= self.clip[3] + EPSILON
        )

    @property
    def partly_visible(self):
        return (
            self.rect[0] < self.clip[2]
            and self.rect[2] > self.clip[0]
            and self.rect[1] < self.clip[3]
            and self.rect[3] > self.clip[1]
        )


def _intersect(a, b):
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


def _overlap(a, b):
    return min(a[2], b[2]) - max(a[0], b[0]) > EPSILON and min(a[3], b[3]) - max(a[1], b[1]) > EPSILON


def laid_out_text(html):
    """Every run of text on page one, with where it landed and the rectangle it is clipped to."""
    page_box = HTML(string=html).render().pages[0]._page_box
    placed = []

    def walk(box, clip):
        if box.style["overflow"] == "hidden":
            x, y = box.padding_box_x(), box.padding_box_y()
            clip = _intersect(clip, (x, y, x + box.padding_width(), y + box.padding_height()))
        if isinstance(box, TextBox) and box.text.strip():
            rect = (box.position_x, box.position_y, box.position_x + box.width, box.position_y + box.height)
            placed.append(PlacedText(box.text, rect, clip))
        for child in getattr(box, "children", ()):
            walk(child, clip)

    walk(page_box, (float("-inf"), float("-inf"), float("inf"), float("inf")))
    return placed


def squeezed(text):
    """*text* without whitespace, so a tag that wraps after its "/" still matches."""
    return re.sub(r"\s+", "", text)


class LabelLayoutTests(StandardTestCase):
    """The layout rules, for every preset: an unsold and a sold worst case, and an online one.

    Each label is rendered once, in setUpTestData, and every test reads the same layouts.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for auction in (cls.in_person_auction, cls.online_auction):
            auction.label_print_fields = ALL_LABEL_FIELDS
            auction.use_custom_checkbox_field = True
            auction.custom_checkbox_name = "BAP/HAP/CARES"
            auction.use_custom_dropdown_field = "allow"
            auction.custom_dropdown_name = "Source"
            auction.use_i_bred_this_fish_field = True
            auction.use_donation_field = True
            auction.use_quantity_field = True
            auction.save()
        auction = cls.in_person_auction
        cls.in_person_tos.name = "Caleb Buxton"
        cls.in_person_tos.email = "caleb.buxton.longer.email@example.com"
        cls.in_person_tos.save()
        cls.in_person_buyer.name = "Bartholomew Featherstonehaugh"
        cls.in_person_buyer.save()
        # An online auction only prints lots that sold, and with a second pickup point it prints where
        # the winner collects -- one more line under the winner's name.
        hall = PickupLocation.objects.create(
            name="Northeast Council of Aquarium Societies, Hall B",
            auction=cls.online_auction,
            pickup_time=timezone.now() + timedelta(days=30),
        )
        cls.tosB.name = "Bartholomew Featherstonehaugh"
        cls.tosB.pickup_location = hall
        cls.tosB.save()
        worst_case = {
            "auction": auction,
            "auctiontos_seller": cls.in_person_tos,
            # The longest name a lot can have: lot_name is 40 characters.
            "lot_name": "Orange Venezuelan Corydoras - Group of 6",
            "quantity": 6,
            "donation": True,
            "custom_checkbox": True,
            "custom_dropdown": "Wild caught",
            "i_bred_this_fish": True,
            "custom_field_1": "Pair, proven spawners",
            "summernote_description": "<p>" + "Eats everything, spawns in soft water. " * 20 + "</p>",
        }
        unsold = Lot.objects.create(**worst_case, reserve_price=25, buy_now_price=100)
        sold = Lot.objects.create(**worst_case, auctiontos_winner=cls.in_person_buyer, winning_price=40)
        online = {**worst_case, "auction": cls.online_auction, "auctiontos_seller": cls.online_tos}
        online_sold = Lot.objects.create(**online, auctiontos_winner=cls.tosB, winning_price=40)
        cls.label_lots = {"unsold": unsold, "sold": sold, "online": online_sold}

        request = RequestFactory().get("/")
        request.user = cls.admin_user
        cls.rendered = {}
        for preset, _ in UserLabelPrefs.PRESETS:
            UserLabelPrefs.objects.update_or_create(user=cls.admin_user, defaults={"preset": preset})
            for kind, lot in cls.label_lots.items():
                view = build_label_view(lot, request, mark_printed=False)
                context = view.get_context_data()
                html = render_to_string(view.template_name, context)
                name = f"{preset}, {kind}"
                cls.rendered[name] = (context, context["labels"][0], laid_out_text(html))

    @staticmethod
    def visible_text(placed):
        return squeezed("".join(p.text for p in placed if p.visible))

    def test_every_preset_is_covered(self):
        self.assertEqual(len(self.rendered), len(self.label_lots) * len(UserLabelPrefs.PRESETS))

    def test_nothing_crosses_a_column_edge(self):
        """Rule 1: any text that shows at all shows its whole width."""
        for name, (_context, _label, placed) in self.rendered.items():
            with self.subTest(name):
                self.assertEqual([p.text for p in placed if p.partly_visible and not p.horizontally_whole], [])

    def test_nothing_is_cut_in_half(self):
        """Rule 3: the middle band stops at a whole line -- no half a line of description."""
        for name, (_context, _label, placed) in self.rendered.items():
            with self.subTest(name):
                self.assertEqual([p.text for p in placed if p.partly_visible and not p.visible], [])

    def test_nothing_overlaps(self):
        """Rule 3: the middle band never runs under the winner or seller pinned to the bottom."""
        for name, (_context, _label, placed) in self.rendered.items():
            with self.subTest(name):
                shown = [p for p in placed if p.partly_visible]
                clashes = [
                    (a.text, b.text) for i, a in enumerate(shown) for b in shown[i + 1 :] if _overlap(a.rect, b.rect)
                ]
                self.assertEqual(clashes, [])

    def test_every_tag_is_on_the_label(self):
        """Rule 2: a tag that doesn't fit on the left moves right; none is lost off the bottom.

        Unless the label can't hold them at all. The Dymo label, sold, with the winner's pickup
        location and every field turned on, needs about 79pt of its 73: then the lot name is down to
        one line, and only then are the tags clamped -- at a whole line, which the other tests hold.

        A *sold* label makes no such promise: there the tags are the first thing to give way, because
        the winner, the lot name and the pickup location are what that label is read for.
        """
        for name, (context, label, placed) in self.rendered.items():
            with self.subTest(name):
                visible = self.visible_text(placed)
                tags = label.tags_left + label.tags_right
                self.assertIn("BAP/HAP/CARES", tags)
                missing = [tag for tag in tags if squeezed(tag) not in visible]
                if missing:
                    # Whatever is missing moved to the right column first; the left column never loses one.
                    self.assertTrue(set(missing) <= set(label.tags_right))
                    needed = wrapped_lines(
                        " · ".join(label.tags_right),
                        width_pt=(context["label_width"] - context["first_column_width"]) * 72,
                        font_size_pt=context["tag_font_size"],
                    )
                    self.assertLess(label.tags_lines, needed)
                    if not label.sold:
                        self.assertGreaterEqual(label.tags_lines, 1)  # a line is always kept for them

    def test_lot_number_name_and_owner_are_on_the_label(self):
        """Rule 3: the lot name and who it belongs to survive however much else there is."""
        for name, (_context, label, placed) in self.rendered.items():
            with self.subTest(name):
                visible = self.visible_text(placed)
                self.assertIn(squeezed(str(label.lot_number_display)), visible)
                self.assertIn(squeezed("Orange Venezuelan"), visible)
                if label.sold:
                    self.assertIn(squeezed("Winner: Bartholomew Featherstonehaugh"), visible)
                    self.assertNotIn("Seller:", visible)
                else:
                    self.assertIn(squeezed("Seller: Caleb Buxton"), visible)

    def test_empty_fields_take_no_space(self):
        """Rule 4: a sold lot has no minimum bid, buy-now price or breeder mark to print."""
        for name, (_context, label, _placed) in self.rendered.items():
            with self.subTest(name):
                tags = label.tags_left + label.tags_right
                self.assertNotIn("", tags)
                priced = [tag for tag in tags if tag.startswith(("Min:", "Buy:")) or tag == "(B)"]
                self.assertEqual(len(priced), 0 if label.sold else 3)

    def test_thermal_qr_code_fills_its_column(self):
        """The 3x2 thermal label's QR code is as wide as the column it sits in (issue #970)."""
        context = self.rendered["thermal_sm, sold"][0]
        self.assertEqual(context["qr_size"], context["first_column_width"])

    def test_online_label_keeps_the_winner_the_lot_name_and_the_pickup_location(self):
        """What a sold label is for, on every preset -- an online auction prints nothing else.

        The three are what gets the lot to the person who won it. Anything else on a sold label gives
        way to them; a long pickup location is allowed to cost the tags line.
        """
        for preset, _ in UserLabelPrefs.PRESETS:
            with self.subTest(preset):
                _context, label, placed = self.rendered[f"{preset}, online"]
                visible = self.visible_text(placed)
                self.assertTrue(label.auction.multi_location)
                self.assertIn(squeezed("Winner: Bartholomew Featherstonehaugh"), visible)
                self.assertGreaterEqual(label.name_lines, 1)
                self.assertIn(squeezed("Orange Venezuelan"), visible)
                self.assertGreaterEqual(label.location_lines, 1)
                self.assertIn(squeezed("Northeast Council"), visible)

    def test_centimetres_lay_out_the_same_as_inches(self):
        """A custom size saved in cm is the same label as that size saved in inches.

        It used to be multiplied by 2.54 instead of divided, so a label saved in cm printed 6.45 times
        too big.
        """
        request = RequestFactory().get("/")
        request.user = self.admin_user
        sizes = {
            "page_width": 8.5,
            "page_height": 11,
            "label_width": 2.5,
            "label_height": 1.0,
            "label_margin_right": 0.2,
            "label_margin_bottom": 0.1,
            "page_margin_top": 0.5,
            "page_margin_bottom": 0.5,
            "page_margin_left": 0.25,
            "page_margin_right": 0.25,
        }
        laid_out = {}
        for unit, per_inch in (("in", 1), ("cm", 2.54)):
            defaults = {"preset": "custom", "unit": unit, **{key: value * per_inch for key, value in sizes.items()}}
            UserLabelPrefs.objects.update_or_create(user=self.admin_user, defaults=defaults)
            context = build_label_view(self.label_lots["sold"], request, mark_printed=False).get_context_data()
            laid_out[unit] = {key: context[key] for key in (*sizes, "labels_per_page")}
        for key, inches in laid_out["in"].items():
            self.assertAlmostEqual(laid_out["cm"][key], inches, places=6, msg=key)
        # 2 across (8in of room, 2.7in a label) and 9 down (10in of room, 1.1in a label).
        self.assertEqual(laid_out["cm"]["labels_per_page"], 18)


class LabelUnitTests(SimpleTestCase):
    """The club's barcode labels read the same prefs, and convert them the same way."""

    def test_club_barcode_labels_convert_centimetres_to_inches(self):
        from auctions.views.club_admin import ClubBarcodeLabelsView

        prefs = UserLabelPrefs(preset="custom", unit="cm", label_width=6.35, label_height=2.54, page_width=21.59)
        dims = ClubBarcodeLabelsView._label_dim_context(prefs)
        self.assertAlmostEqual(dims["label_width"], 2.5)
        self.assertAlmostEqual(dims["label_height"], 1.0)
        self.assertAlmostEqual(dims["page_width"], 8.5)


class LabelMeasurementTests(SimpleTestCase):
    """The measuring behind plan_label, without rendering anything."""

    def test_width_is_measured_in_the_label_face(self):
        # DejaVu Serif, measured: 8.73em.
        self.assertAlmostEqual(text_width_pt("BAP/HAP/CARES", 10), 87.3, places=0)

    def test_a_tag_too_wide_for_one_line_goes_right_and_a_short_one_after_it_still_goes_left(self):
        left, right = split_label_tags(["QTY: 1", "BAP/HAP/CARES", "(D)"], width_pt=54, height_pt=100, font_size_pt=10)
        self.assertEqual(left, ["QTY: 1", "(D)"])
        self.assertEqual(right, ["BAP/HAP/CARES"])

    def test_tags_past_the_last_line_go_right(self):
        # Two 12pt lines in 30pt.
        left, right = split_label_tags(["(D)", "(B)", "QTY: 1"], width_pt=54, height_pt=30, font_size_pt=10)
        self.assertEqual(left, ["(D)", "(B)"])
        self.assertEqual(right, ["QTY: 1"])

    def test_no_room_at_all(self):
        self.assertEqual(split_label_tags(["(D)"], width_pt=54, height_pt=5, font_size_pt=10), ([], ["(D)"]))

    def test_wrapping_breaks_at_spaces_and_inside_a_word_too_long_for_a_line(self):
        self.assertEqual(wrapped_lines("", width_pt=100, font_size_pt=10), 0)
        self.assertEqual(wrapped_lines("QTY: 1", width_pt=100, font_size_pt=10), 1)
        # 8.73em at 10pt is 87pt: one per 90pt line.
        self.assertEqual(wrapped_lines("BAP/HAP/CARES BAP/HAP/CARES", width_pt=90, font_size_pt=10), 2)
        # After a slash, as WeasyPrint does: "BAP/HAP/" (51pt) and "CARES" (37pt) in 55pt lines.
        self.assertEqual(wrapped_lines("BAP/HAP/CARES", width_pt=55, font_size_pt=10), 2)
        # 9.89em, with nowhere to break: split across two 50pt lines, as overflow-wrap: anywhere does.
        self.assertEqual(wrapped_lines("Featherstonehaugh", width_pt=50, font_size_pt=10), 2)
        self.assertEqual(wrapped_lines("one\ntwo", width_pt=100, font_size_pt=10), 2)
        # A word that ends in a delimiter, or is nothing but delimiters, is still one word.
        self.assertEqual(wrapped_lines("BAP/HAP/", width_pt=90, font_size_pt=10), 1)
        self.assertEqual(wrapped_lines("-- //", width_pt=90, font_size_pt=10), 1)
