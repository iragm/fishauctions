"""The help guides at /help/: which guides exist, how they're grouped, and what each one has to cover.

A guide is a template in ``auctions/templates/help/guides/<slug>.html``. What it documents is read
out of that template, not listed here: ``{% page "url_name" %}`` names a page (and links it into the
reader's own auction when it can), ``{% rule "field" %}`` names an auction rule. So there is no second
list to drift, and ``test_help`` can fail the build when a page or a rule is documented nowhere --
the same way ``palette_routes`` fails it for a URL nobody catalogued.

``NOT_YET_DOCUMENTED`` and ``RULES_NOT_YET_DOCUMENTED`` are the backlog. They only shrink: a name
there that a guide now covers fails the test until it is taken off, and a new page or rule has to be
written up (or excused in ``NOT_IN_HELP``) before the build passes.

The guides are public and indexed. What makes them personal is ``HelpContext``: the auction the reader
last used (or ``?auction=``), whether they run it, and its club. Every personal sentence goes in a
``help-tip`` div, so the page reads the same to Google and to a signed-out visitor with the tips gone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from django.template.loader import get_template
from django.urls import NoReverseMatch, reverse
from django.utils.html import strip_tags

ADMIN = "admin"
EVERYONE = "everyone"

ONLINE = "online"
IN_PERSON = "in_person"

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "help" / "guides"


@dataclass(frozen=True)
class Guide:
    """One guide. ``kind`` is the auction type it is written for, blank for both."""

    slug: str
    title: str
    icon: str
    summary: str
    audience: str = EVERYONE
    kind: str = ""

    @property
    def template_name(self) -> str:
        return f"help/guides/{self.slug}.html"

    @property
    def url(self) -> str:
        return reverse("help_guide", kwargs={"slug": self.slug})


@dataclass(frozen=True)
class Group:
    title: str
    guides: tuple[Guide, ...]


GROUPS = (
    Group(
        "Running an auction",
        (
            Guide(
                "run-an-online-auction",
                "Running an online auction",
                "bi-laptop",
                "Set up an online auction for your club, from the first lot to the last invoice.",
                audience=ADMIN,
                kind=ONLINE,
            ),
            Guide(
                "run-an-in-person-auction",
                "Running an in-person auction",
                "bi-megaphone",
                "Run an in-person club auction: check-in, lots, the auctioneer's table and checkout.",
                audience=ADMIN,
                kind=IN_PERSON,
            ),
            Guide(
                "auction-rules",
                "What the auction rules do",
                "bi-sliders",
                "Every setting on an auction's rules page, with an example of when you'd change it.",
                audience=ADMIN,
            ),
            Guide(
                "labels",
                "Lot labels",
                "bi-tag",
                "Printing lot labels: sheets, thermal printers, what goes on a label, and who prints them.",
            ),
        ),
    ),
    Group(
        "Buying and selling",
        (
            Guide(
                "online-auctions",
                "Taking part in an online auction",
                "bi-hand-index",
                "Join an online club auction, add lots, bid, pay, and pick up what you won.",
                kind=ONLINE,
            ),
            Guide(
                "in-person-auctions",
                "Taking part in an in-person auction",
                "bi-people",
                "What to expect at an in-person club auction, whether you're bringing fish or buying them.",
                kind=IN_PERSON,
            ),
        ),
    ),
    Group(
        "Clubs",
        (
            Guide(
                "clubs",
                "Clubs",
                "bi-house-heart",
                "Members, dues, events, announcements, breeder points and everything else a club can do here.",
            ),
        ),
    ),
    Group(
        "You",
        (
            Guide(
                "your-account",
                "Your account",
                "bi-person-gear",
                "Preferences, notifications, getting paid, and the rest of your account settings.",
            ),
        ),
    ),
)

GUIDES: dict[str, Guide] = {guide.slug: guide for group in GROUPS for guide in group.guides}


# --- what each auction type's reader should be reading ------------------------

#: The guide for running (admin) or taking part in (everyone) each kind of auction.
GUIDE_FOR = {
    (ADMIN, ONLINE): "run-an-online-auction",
    (ADMIN, IN_PERSON): "run-an-in-person-auction",
    (EVERYONE, ONLINE): "online-auctions",
    (EVERYONE, IN_PERSON): "in-person-auctions",
}


def auction_kind(auction) -> str:
    return ONLINE if auction.is_online else IN_PERSON


def guide_for_auction(auction, is_admin: bool) -> Guide:
    """The guide somebody looking at ``auction`` wants first."""
    return GUIDES[GUIDE_FOR[(ADMIN if is_admin else EVERYONE, auction_kind(auction))]]


# --- the reader ---------------------------------------------------------------


@dataclass
class HelpContext:
    """Who is reading and which auction they mean. Every field may be empty; the guides still read."""

    auction: object = None
    is_admin: bool = False
    club: object = None
    is_club_admin: bool = False

    @property
    def kind(self) -> str:
        return auction_kind(self.auction) if self.auction else ""


def help_context(request) -> HelpContext:
    """``?auction=<slug>`` if given, otherwise the auction the reader last used."""
    from auctions.models import Auction

    user = request.user
    auction = None
    slug = (request.GET.get("auction") or "").strip()
    if slug:
        auction = Auction.objects.filter(slug=slug, is_deleted=False).select_related("club").first()
    if auction is None and user.is_authenticated:
        userdata = getattr(user, "userdata", None)
        auction = getattr(userdata, "last_auction_used", None) if userdata else None
        if auction is not None and auction.is_deleted:
            auction = None
    is_admin = bool(auction and user.is_authenticated and auction.permission_check(user))
    club = getattr(auction, "club", None) if auction else None
    if club is None and user.is_authenticated:
        userdata = getattr(user, "userdata", None)
        club = getattr(userdata, "last_club_used", None) if userdata else None
    is_club_admin = False
    if club is not None and user.is_authenticated:
        from auctions.views.base import check_club_permission

        is_club_admin = bool(check_club_permission(user, club, "permission_admin"))
    return HelpContext(auction=auction, is_admin=is_admin, club=club, is_club_admin=is_club_admin)


def tips_for(guide: Guide, ctx: HelpContext) -> list[dict]:
    """Personal pointers at the top of a guide: "you're reading the wrong one, try this".

    Each is ``{"text", "guide"}``; the template words the link. Nothing here without an auction.
    """
    if not ctx.auction:
        return []
    tips = []
    better = guide_for_auction(ctx.auction, ctx.is_admin)
    if guide.kind and guide.kind != ctx.kind:
        kind_name = "an online" if ctx.kind == ONLINE else "an in-person"
        tips.append({"text": f"{ctx.auction.title} is {kind_name} auction.", "guide": better})
    elif guide.audience == ADMIN and not ctx.is_admin:
        tips.append({"text": "This one is mostly for the people running the auction.", "guide": better})
    elif guide.audience == EVERYONE and guide.kind and ctx.is_admin:
        tips.append({"text": f"You run {ctx.auction.title}.", "guide": better})
    return tips


# --- what the guides cover: read out of the templates -------------------------

_PAGE_TAG = re.compile(r"""\{%\s*page\s+["']([\w:.-]+)["']""")
_RULE_TAG = re.compile(r"""\{%\s*rule\s+["']([\w]+)["']""")


def _sources() -> dict[str, str]:
    return {slug: (TEMPLATE_DIR / f"{slug}.html").read_text(encoding="utf-8") for slug in GUIDES}


def documented_pages() -> dict[str, set[str]]:
    """URL name -> the guides that name it with ``{% page %}``."""
    found: dict[str, set[str]] = {}
    for slug, source in _sources().items():
        for name in _PAGE_TAG.findall(source):
            found.setdefault(name, set()).add(slug)
    return found


def documented_rules() -> dict[str, set[str]]:
    """Auction rule field -> the guides that explain it with ``{% rule %}``."""
    found: dict[str, set[str]] = {}
    for slug, source in _sources().items():
        for name in _RULE_TAG.findall(source):
            found.setdefault(name, set()).add(slug)
    return found


def rule_fields() -> list[str]:
    """Every setting on an auction's rules pages, in form order."""
    from auctions.forms import AuctionCustomFieldsForm, AuctionEditForm

    fields = list(AuctionEditForm.base_fields)
    fields += [name for name in AuctionCustomFieldsForm.base_fields if name not in fields]
    return fields


#: Pages that are deliberately in no guide.
NOT_IN_HELP: dict[str, str] = {
    "home": "The front page is where people start, not something to explain.",
    "promo": "The about page is itself an explanation of the site.",
    "tos": "Legal text; it says what it says.",
    "privacy_policy": "Legal text; it says what it says.",
    "dmca": "Legal text for rightsholders, linked from the footer.",
    "dmca_notice": "A form for rightsholders, reached from the DMCA page.",
    "support": "The contact page is where help ends, not a topic.",
    "faq": "Being replaced by these guides.",
    "auction_help": "Redirects into these guides.",
    "blog_post": "Posts are announcements, read on their own.",
    "admin_dashboard": "Site operator only.",
    "admin_setup_checklist": "Site operator only.",
    "command_palette_analytics": "Site operator only.",
    "assistant_skill_requests": "Site operator only.",
    "species_gaps": "Site operator only.",
    "species_create": "Site operator only.",
    "species_name_create": "Site operator only.",
    "admin_traffic": "Site operator only.",
    "admin_referrers": "Site operator only.",
    "admin_usability": "Site operator only.",
    "admin_club_health": "Site operator only.",
    "admin_unlinked_auctions": "Site operator only.",
    "admin_lifecycle": "Site operator only.",
    "admin_session_replay": "Site operator only.",
    "admin_user_map": "Site operator only.",
    "admin_user_signups": "Site operator only.",
    "admin_error": "Site operator only.",
    "all_my_users": "Site operator only.",
    "help": "This is the help.",
}

#: Pages no guide covers yet. Only ever shrinks -- see the module docstring.
NOT_YET_DOCUMENTED = frozenset(
    {
        # Browsing
        "allLots",
        "auctions",
        "all_auctions",
        "clubs",
        "leaderboard",
        "my_last_auction_lots",
        "user_lots",
        "userpage",
        "speaker_list",
        "speaker_detail",
        "speaker_add",
        "feedback",
        "add_to_calendar",
        # My stuff
        "selling",
        "watched",
        "won_lots",
        "my_bids",
        "my_invoices",
        "invoice_by_pk",
        "new_lot",
        "messages",
        "my_lot_report",
        "my_won_lot_csv",
        "lot_by_pk",
        "report_lot",
        "edit_lot",
        "delete_lot",
        "add_image",
        "single_lot_label",
        "lot_by_pk_qr",
        # Account
        "account",
        "account_setup",
        "preferences",
        "notification_preferences",
        "contact_info",
        "change_username",
        "ignore_categories",
        "printing",
        "user_api_keys",
        "account_data_export",
        "account_delete",
        "paypal_seller",
        "paypal_connect",
        "paypal_seller_delete",
        "square_seller",
        "square_connect",
        "square_seller_delete",
        # Auction
        "auction_main",
        "auction_lot_list",
        "my_auction_invoice",
        "auction_chat",
        "auction_stats",
        "auction_lot_map",
        "auction_volunteers",
        "auction_door_prizes",
        "auction_self_check_in",
        "print_my_labels",
        "print_my_unprinted_labels",
        "bulk_add_lots_for_myself",
        "bulk_add_lots_auto_for_myself",
        "auction_confirm",
        "lot_list",
        # Running an auction
        "create_auction",
        "edit_auction",
        "edit_auction_custom_fields",
        "auction_tos_list",
        "auction_invoices",
        "auction_lot_winners_dynamic",
        "auction_quick_checkout",
        "auction_quick_check_in",
        "auction_lot_queue",
        "auction_lot_queue_current_lot",
        "auction_printing",
        "auction_printable_lot_list",
        "auction_printing_pdf",
        "auction_label_config",
        "bulk_add_users",
        "import_from_google_drive",
        "sync_google_drive",
        "import_lots_from_csv",
        "compose_email_to_users",
        "auction_add_users_to_club",
        "user_list",
        "auction_history",
        "auction_pickup_location",
        "create_auction_pickup_location",
        "auction_disable_bidding",
        "auction_lot_map_clear",
        "auction_voice_command_log",
        "auction_delete",
        "bulk_add_lots",
        "bulk_add_lots_auto",
        "bulk_add_image",
        "print_labels_by_bidder_number",
        "print_unprinted_labels_by_bidder_number",
        "auction_no_show",
        "my_labels_by_username",
        # Pickup locations
        "edit_pickup",
        "delete_pickup",
        "location_incoming",
        "location_outgoing",
        # Club
        "club_detail",
        "club_detail_tab",
        "club_membership_pay",
        "club_events_ical",
        "club_events_embed",
        "club_past_events_embed",
        "bap_embed",
        "club_announcements_embed",
        "club_auction_embed",
        # Club admin
        "club_admin",
        "club_setup",
        "club_announcements",
        "club_website_integration",
        "club_edit",
        "club_membership_settings",
        "club_email_settings",
        "club_donation_vendors",
        "club_donation_settings",
        "club_link_payment_account",
        "club_paypal_credentials",
        "club_history",
        "club_stats",
        "club_member_map",
        "club_member_import",
        "club_member_export",
        "club_treasurer_report",
        "club_treasurer_report_export",
        "club_money_add",
        "club_money_balance",
        "club_bap",
        "club_bap_lots",
        "club_bap_settings",
        "club_bap_import",
        "club_barcode_labels",
        "club_barcode_labels_pdf",
        "club_event_add",
        "club_api_keys",
        "club_api_key_create",
        "club_mailchimp_config",
        "mailchimp_connect",
        "mailchimp_select_audience",
        "mailchimp_sync_now",
        "mailchimp_disconnect",
        "club_brevo_config",
        "brevo_connect",
        "brevo_select_list",
        "brevo_sync_now",
        "brevo_disconnect",
        "club_google_calendar_config",
        "google_calendar_connect",
        "google_calendar_sync_now",
        "google_calendar_disconnect",
        "club_discord_config",
        "club_discord_fetch_roles",
        "club_discord_send_join_message",
        "club_member_renew_page",
        "club_member_merge",
    }
)

#: Auction rules no guide explains yet. Only ever shrinks.
RULES_NOT_YET_DOCUMENTED = frozenset(
    {
        "summernote_description",
        "lot_entry_fee",
        "registration_fee",
        "unsold_lot_fee",
        "winning_bid_percent_to_club",
        "date_start",
        "date_end",
        "lot_submission_start_date",
        "lot_submission_end_date",
        "promote_this_auction",
        "max_lots_per_user",
        "allow_additional_lots_as_donation",
        "email_users_when_invoices_ready",
        "add_membership_fee_to_invoices_for_expired_members",
        "pre_register_lot_discount_percent",
        "only_approved_sellers",
        "only_approved_bidders",
        "invoice_payment_instructions",
        "invoice_rounding",
        "only_whole_dollar_bids",
        "minimum_bid",
        "winning_bid_percent_to_club_for_club_members",
        "lot_entry_fee_for_club_members",
        "registration_fee_for_club_members",
        "club_member_discount",
        "force_donation_threshold",
        "require_phone_number",
        "tax",
        "online_bidding",
        "date_online_bidding_ends",
        "date_online_bidding_starts",
        "allow_deleting_bids",
        "auto_add_images",
        "message_users_when_lots_sell",
        "copy_users_when_copying_this_auction",
        "use_seller_dash_lot_numbering",
        "enable_online_payments",
        "enable_square_payments",
        "club",
        "manage_users_through_club",
        "allow_self_checkin",
        "user_cut",
        "club_member_cut",
        # The extra-fields page
        "allow_bulk_adding_lots",
        "reserve_price",
        "buy_now",
        "use_categories",
        "use_scientific_name",
        "use_quantity_field",
        "use_donation_field",
        "use_i_bred_this_fish_field",
        "use_reference_link",
        "use_description",
        "custom_field_1",
        "custom_field_1_name",
        "use_custom_checkbox_field",
        "custom_checkbox_name",
        "use_custom_dropdown_field",
        "custom_dropdown_name",
        "use_custom_random_field",
        "custom_random_name",
    }
)


# --- search -------------------------------------------------------------------


@cache
def guide_text(slug: str) -> str:
    """A guide as plain text, rendered for nobody in particular: no tips, no links into an auction."""
    html = get_template(GUIDES[slug].template_name).render({"help": HelpContext(), "guide": GUIDES[slug]})
    return re.sub(r"\s+", " ", strip_tags(html)).strip()


def _sections(slug: str) -> list[tuple[str, str, str]]:
    """``(anchor, heading, text)`` for each ``<h2 id=...>`` section of a guide, rendered for nobody."""
    html = get_template(GUIDES[slug].template_name).render({"help": HelpContext(), "guide": GUIDES[slug]})
    parts = re.split(r"<h2[^>]*\bid=\"([^\"]+)\"[^>]*>(.*?)</h2>", html, flags=re.DOTALL)
    sections = [("", GUIDES[slug].title, parts[0])]
    for i in range(1, len(parts), 3):
        sections.append((parts[i], strip_tags(parts[i + 1]).strip(), parts[i + 2]))
    return [(anchor, heading, re.sub(r"\s+", " ", strip_tags(body)).strip()) for anchor, heading, body in sections]


@cache
def all_sections() -> tuple[tuple[str, str, str, str], ...]:
    """``(slug, anchor, heading, text)`` for every section of every guide."""
    return tuple((slug, anchor, heading, text) for slug in GUIDES for anchor, heading, text in _sections(slug))


def search(query: str, limit: int = 8) -> list[dict]:
    """Sections whose heading or text contains the query's words, best first.

    A heading hit counts triple. ``{"guide", "heading", "url", "excerpt", "text"}`` per result.
    """
    words = re.findall(r"[a-z0-9']{3,}", query.lower())[:8]
    if not words:
        return []
    scored = []
    for slug, anchor, heading, text in all_sections():
        lowered_heading, lowered_text = heading.lower(), text.lower()
        score = sum(3 * lowered_heading.count(w) + lowered_text.count(w) for w in words)
        if not score or not text:
            continue
        scored.append((score, slug, anchor, heading, text))
    scored.sort(key=lambda row: -row[0])
    results = []
    for _score, slug, anchor, heading, text in scored[:limit]:
        guide = GUIDES[slug]
        results.append(
            {
                "guide": guide.title,
                "heading": heading,
                "url": guide.url + (f"#{anchor}" if anchor else ""),
                "excerpt": _excerpt(text, words),
                "text": text,
            }
        )
    return results


def _excerpt(text: str, words: list[str], width: int = 220) -> str:
    lowered = text.lower()
    hits = [lowered.find(w) for w in words if lowered.find(w) >= 0]
    start = max(0, min(hits) - 60) if hits else 0
    snippet = text[start : start + width]
    return ("…" if start else "") + snippet + ("…" if start + width < len(text) else "")


# --- the sidebar --------------------------------------------------------------


def groups_for(active: str = "") -> list[dict]:
    """The help menu in ``account_sidebar_nav.html``'s shape."""
    drawn = [
        {
            "title": "",
            "rows": [{"label": "All help", "icon": "bi-life-preserver", "url": reverse("help"), "active": not active}],
        }
    ]
    for group in GROUPS:
        drawn.append(
            {
                "title": group.title,
                "rows": [
                    {"label": guide.title, "icon": guide.icon, "url": guide.url, "active": guide.slug == active}
                    for guide in group.guides
                ],
            }
        )
    return drawn


def guide_url(slug: str, auction=None) -> str:
    """Absolute-path link to a guide, carrying the auction so its tips are about that auction.

    For emails and pages that know which auction they're about.
    """
    try:
        url = GUIDES[slug].url
    except (KeyError, NoReverseMatch):
        return reverse("help")
    return f"{url}?auction={auction.slug}" if auction is not None else url
