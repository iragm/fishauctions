"""The help guides at /help/: which guides exist, how they're grouped, and what each one has to cover.

A guide is a template in ``auctions/templates/help/guides/<slug>.html``. What it documents is read
out of that template, not listed here: ``{% page "url_name" %}`` names a page (and links it into the
reader's own auction when it can), ``{% rule "field" %}`` names an auction rule, ``{% ui "Button" %}``
names a button or label. So there is no second list to drift, and ``test_help`` can fail the build when
a page or a rule is documented nowhere, or a button a guide names is gone -- the same way
``palette_routes`` fails it for a URL nobody catalogued.

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
from functools import cached_property, lru_cache
from pathlib import Path

from django.conf import settings
from django.template.loader import get_template
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.html import strip_tags

ADMIN = "admin"
EVERYONE = "everyone"

ONLINE = "online"
IN_PERSON = "in_person"

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "help" / "guides"


@dataclass(frozen=True)
class Guide:
    """One guide. ``kind`` is the auction type it is written for, blank for both. A ``fish_only`` guide is
    only on a site whose ``WEBSITE_FOCUS`` is fish, the way the invoice's fish tips are.
    """

    slug: str
    title: str
    icon: str
    summary: str
    audience: str = EVERYONE
    kind: str = ""
    fish_only: bool = False

    @property
    def shown(self) -> bool:
        return not self.fish_only or is_fish_site()

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
                "payments",
                "Card payments",
                "bi-credit-card",
                "Taking cards with Square: pay buttons, checkout QR codes, Tap to Pay.",
                audience=ADMIN,
            ),
            Guide(
                "scanning",
                "Barcodes and scanners",
                "bi-upc-scan",
                "Scanning membership cards, paddles and lot labels with a phone camera or a USB scanner.",
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
            Guide(
                "bagging-fish",
                "Bagging fish",
                "bi-droplet",
                "Bagging fish so they arrive healthy, and settling new ones into your tank.",
                fish_only=True,
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
                "Finding a club, setting yours up, who can do what, and the club's auctions.",
            ),
            Guide(
                "club-membership",
                "Members and dues",
                "bi-person-vcard",
                "The member list, wallet membership cards, dues by card or cash, and automatic renewals.",
            ),
            Guide(
                "club-email",
                "Club email and announcements",
                "bi-envelope",
                "Where members' replies go, membership emails, announcements, Mailchimp, Brevo and Discord.",
            ),
            Guide(
                "club-events",
                "Events and your website",
                "bi-calendar-event",
                "The club calendar, Google Calendar, snippets for your own website, and API keys.",
            ),
            Guide(
                "breeder-award-programs",
                "Breeder award programs",
                "bi-award",
                "BAP, HAP and CAP points: earning them, the rules that award them, and approving them.",
            ),
            Guide(
                "club-donations",
                "Donations from vendors",
                "bi-gift",
                "Asking local businesses to donate, and keeping track of who said yes.",
            ),
            Guide(
                "club-money",
                "Club money",
                "bi-cash-coin",
                "The treasurer's report, recording money in and out, and the club's payment accounts.",
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
            Guide(
                "ai-agents",
                "AI agents",
                "bi-robot",
                "Connect Claude, ChatGPT or Grok, and have it add lots, check invoices, or read these guides for you.",
            ),
            Guide(
                "mobile-app",
                "The phone app",
                "bi-phone",
                "What the app does that the website can't: Tap to Pay, Bluetooth labels, lot scanning and offline mode.",
            ),
        ),
    ),
)

GUIDES: dict[str, Guide] = {guide.slug: guide for group in GROUPS for guide in group.guides}


def is_fish_site() -> bool:
    return settings.WEBSITE_FOCUS == "fish"


def shown_groups() -> list[Group]:
    """``GROUPS`` without the guides this site doesn't show, and without any group left empty."""
    groups = [Group(group.title, tuple(guide for guide in group.guides if guide.shown)) for group in GROUPS]
    return [group for group in groups if group.guides]


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
    """Who is reading and which auction they mean. Every field may be empty; the guides still read.

    The cached properties are what the guides quote back at the reader. Each is empty for a signed-out
    reader (or ``HelpContext()``, which is how search renders a guide), so a guide reads the same
    with every personal line gone.
    """

    auction: object = None
    is_admin: bool = False
    club: object = None
    is_club_admin: bool = False
    user: object = None

    @property
    def kind(self) -> str:
        return auction_kind(self.auction) if self.auction else ""

    @property
    def fish_site(self) -> bool:
        """For a link to a ``fish_only`` guide from another one."""
        return is_fish_site()

    @property
    def app_stores(self) -> list[dict]:
        """Where to get the phone app, from ``APP_STORE_URL`` and ``PLAY_STORE_URL``; empty before it's out."""
        stores = (
            ("App Store", settings.APP_STORE_URL, "bi-apple"),
            ("Google Play", settings.PLAY_STORE_URL, "bi-google-play"),
        )
        return [{"name": name, "url": url, "icon": icon} for name, url, icon in stores if url]

    @property
    def now(self):
        """For a guide comparing one of the auction's dates with now."""
        return timezone.now()

    @property
    def signed_in(self) -> bool:
        return bool(self.user and self.user.is_authenticated)

    @cached_property
    def site(self) -> dict:
        """Facts from every auction on the site (``help_stats.site_stats``)."""
        from auctions.help_stats import site_stats

        return site_stats()

    @cached_property
    def photos(self) -> dict:
        """Photo prices at big in-person auctions (``help_stats.in_person_photos``)."""
        from auctions.help_stats import in_person_photos

        return in_person_photos()

    @cached_property
    def rules_chart(self) -> dict:
        """Rules length against time spent reading them (``help_stats.rules_chart``)."""
        from auctions.help_stats import rules_chart

        return rules_chart()

    @cached_property
    def online_timing(self) -> dict:
        """Lots sold by the weekday an online auction ends, and how long it runs (``help_stats.online_timing``)."""
        from auctions.help_stats import online_timing

        return online_timing()

    @cached_property
    def seller_rank(self) -> dict:
        """How a seller's later lots do (``help_stats.seller_rank``)."""
        from auctions.help_stats import seller_rank

        return seller_rank()

    @cached_property
    def bid_amounts(self) -> dict:
        """How much people bid (``help_stats.bid_amounts``)."""
        from auctions.help_stats import bid_amounts

        return bid_amounts()

    @cached_property
    def bred_species(self) -> dict:
        """The species most often sold as bred by the seller (``help_stats.bred_species``)."""
        from auctions.help_stats import bred_species

        return bred_species()

    @cached_property
    def stats_auction(self):
        """The auction whose numbers the guides quote: this one if the reader ran it, else their latest."""
        from auctions.help_stats import stats_auction

        return stats_auction(self.user, self.auction, self.is_admin)

    @cached_property
    def stats(self) -> dict:
        from auctions.help_stats import auction_facts

        return auction_facts(self.stats_auction) if self.stats_auction else {}

    @cached_property
    def paypal_invoices(self) -> dict:
        """The PayPal export for the reader's auction or latest one (``help_stats.paypal_invoices``)."""
        from auctions.help_stats import paypal_invoices

        return paypal_invoices(self.user, self.auction, self.is_admin)

    @cached_property
    def paypal(self) -> bool:
        """Whether PayPal is on for the reader: their club takes it, or their account has it, or they linked one.

        The guides mention PayPal only when this is true; everyone else is pointed at Square.
        """
        club = self.club
        if club is not None and (club.allow_non_oauth_paypal or club.can_accept_paypal):
            return True
        if not self.signed_in:
            return False
        userdata = getattr(self.user, "userdata", None)
        if userdata is not None and userdata.paypal_enabled:
            return True
        from auctions.models import PayPalSeller

        return PayPalSeller.objects.filter(user=self.user).exists()

    @cached_property
    def tos(self):
        """The reader's place in the auction, with their pickup location, or None."""
        if not (self.auction and self.signed_in):
            return None
        from auctions.models import AuctionTOS

        return AuctionTOS.objects.filter(auction=self.auction, user=self.user).select_related("pickup_location").first()

    @cached_property
    def invoice(self):
        """The reader's invoice in the auction, or None."""
        if not self.tos:
            return None
        from auctions.models import Invoice

        return Invoice.objects.filter(auctiontos_user=self.tos).first()

    @cached_property
    def pay_now(self) -> bool:
        """Whether the auction's invoices get a Pay now button. ``Invoice.show_payment_button`` asked of an
        unsaved invoice that owes a dollar, so the answer is the auction's set-up, not the reader's balance.
        """
        if not self.auction:
            return False
        from decimal import Decimal

        from auctions.models import Invoice

        invoice = Invoice(auction=self.auction, status="DRAFT")
        invoice.__dict__["rounded_net_after_payments"] = Decimal(-1)
        return bool(invoice.show_payment_button)

    @cached_property
    def lot_fields(self) -> list[dict]:
        """The boxes after the name on the auction's lot form, in the form's words: ``{"label", "required"}``.
        The ones ``lot_form.html`` shows for the auction (``get_auction_info``).
        """
        auction = self.auction
        if not auction:
            return []
        from auctions.forms import CreateLotForm
        from auctions.models import AuctionDropdown

        fields = []

        def add(name, required=False, label=""):
            fields.append({"label": label or str(CreateLotForm.base_fields[name].label), "required": required})

        if auction.use_quantity_field:
            add("quantity")
        if auction.use_description:
            add("summernote_description")
        if auction.reserve_price != "disable":
            add("reserve_price", auction.reserve_price == "required")
        if auction.buy_now != "disable":
            add("buy_now_price", auction.buy_now == "required")
        if auction.use_i_bred_this_fish_field:
            add("i_bred_this_fish")
        if auction.use_reference_link:
            add("reference_link")
        if auction.use_donation_field:
            add("donation")
        if auction.use_custom_checkbox_field and auction.custom_checkbox_name:
            add("custom_checkbox", label=auction.custom_checkbox_name)
        if auction.custom_field_1 != "disable" and auction.custom_field_1_name:
            add("custom_field_1", auction.custom_field_1 == "required", auction.custom_field_1_name)
        if (
            auction.use_custom_dropdown_field != "disable"
            and auction.custom_dropdown_name
            and AuctionDropdown.objects.filter(auction=auction).count() >= 2
        ):
            add("custom_dropdown", auction.use_custom_dropdown_field == "required", auction.custom_dropdown_name)
        return fields

    @cached_property
    def has_ai_agent(self) -> bool:
        """Whether the reader has connected an AI agent: a live key, or a sign-in from one."""
        if not self.signed_in:
            return False
        from auctions.models import UserAPIKey

        if UserAPIKey.objects.filter(user=self.user, is_active=True).exists():
            return True
        from auctions.mcp import auth as mcp_auth

        if not mcp_auth.oauth_enabled():
            return False
        from oauth2_provider.models import get_refresh_token_model

        return get_refresh_token_model().objects.filter(user=self.user, revoked__isnull=True).exists()

    @cached_property
    def label_fields(self) -> list[str]:
        """What the auction's labels print, in the words its label settings page uses."""
        if not self.auction:
            return []
        from auctions.forms import LabelPrintFieldsForm

        chosen = set((self.auction.label_print_fields or "").split(","))
        form = LabelPrintFieldsForm(auction=self.auction)
        # A field the auction has turned off prints nothing, and its tooltip says so.
        return ["Lot number"] + [
            field["description"]
            for field in form.available_fields
            if field["value"] in chosen and "disabled in this auction" not in field["tooltip"]
        ]

    @cached_property
    def account(self) -> dict:
        """The reader's account, as far as the account guide cares: what's missing and what's set up."""
        if not self.signed_in:
            return {}
        from allauth.account.models import EmailAddress

        from auctions.models import AuctionTOS, ClubMember, MobileDevice, Watch

        user = self.user
        userdata = getattr(user, "userdata", None)
        return {
            "verified": EmailAddress.objects.filter(user=user, verified=True).exists(),
            "address": bool(userdata and (userdata.address or "").strip()),
            "phone": bool(userdata and (userdata.phone_number or "").strip()),
            "auctions": AuctionTOS.objects.filter(user=user).values("auction").distinct().count(),
            "clubs": ClubMember.objects.filter(user=user, is_deleted=False).count(),
            "watching": Watch.objects.filter(user=user).count(),
            "app": MobileDevice.objects.filter(user=user).exists(),
            "app_push": bool(userdata and userdata.has_app_push),
            "username_visible": bool(userdata and userdata.username_visible),
            "lot_alerts": bool(userdata and userdata.push_notifications_when_lots_sell),
            "push_instead_of_email": bool(userdata and userdata.push_notifications_instead_of_email),
        }


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
    return HelpContext(auction=auction, is_admin=is_admin, club=club, is_club_admin=is_club_admin, user=user)


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
_UI_TAG = re.compile(r"""\{%\s*ui\s+"([^"]+)"\s*%\}""")


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


def documented_ui() -> dict[str, set[str]]:
    """Button or label -> the guides that name it with ``{% ui %}``."""
    found: dict[str, set[str]] = {}
    for slug, source in _sources().items():
        for label in _UI_TAG.findall(source):
            found.setdefault(label, set()).add(slug)
    return found


def _site_wording() -> str:
    """Everything the site says outside the help: templates, code and scripts, plus every model field's and
    form field's label, which Django makes from names that appear nowhere as written.
    """
    import inspect

    from django import forms
    from django.apps import apps
    from django.utils.text import capfirst

    from auctions import forms as auction_forms

    app_dir = Path(__file__).resolve().parent
    parts = []
    for path in app_dir.rglob("*"):
        relative = path.relative_to(app_dir).as_posix()
        if (
            path.suffix not in (".html", ".py", ".js", ".txt")
            or relative.startswith(("templates/help/", "migrations/"))
            or "/vendor/" in relative
            or path.name.startswith("test")
            or ".min." in path.name
        ):
            continue
        parts.append(path.read_text(encoding="utf-8", errors="ignore"))
    for model in apps.get_app_config("auctions").get_models():
        parts.extend(
            str(capfirst(field.verbose_name)) for field in model._meta.get_fields() if hasattr(field, "verbose_name")
        )
    for _name, form in inspect.getmembers(auction_forms, inspect.isclass):
        if issubclass(form, forms.BaseForm):
            parts.extend(str(field.label) for field in form.base_fields.values() if field.label)
            parts.extend(str(label) for label in getattr(form, "LABELS", {}).values())
    return "\n".join(parts)


def missing_ui_labels() -> dict[str, set[str]]:
    """``{% ui %}`` labels the site no longer has anywhere, with the guides still naming them."""
    wording = _site_wording()
    return {label: slugs for label, slugs in documented_ui().items() if label not in wording}


def rule_fields() -> list[str]:
    """Every setting on an auction's rules pages, in form order."""
    from auctions.forms import AuctionCustomFieldsForm, AuctionEditForm

    fields = list(AuctionEditForm.base_fields)
    fields += [name for name in AuctionCustomFieldsForm.base_fields if name not in fields]
    return fields


def club_setting_labels(club) -> dict[str, str]:
    """Every setting on a club's settings pages, as ``{field: label}`` in the words that page shows ``club``.

    Many labels are set when the form is built, so the forms are built, not read. A club setting is
    covered by ``{% ui "Label" %}`` in a guide.
    """
    from auctions import forms

    built = (
        forms.ClubEditForm(instance=club),
        forms.ClubMembershipSettingsForm(instance=club),
        forms.ClubEmailSettingsForm(instance=club),
        forms.ClubEmailSettingsForm(instance=club, show_email_routing=False),
        forms.ClubBapSettingsForm(instance=club),
        forms.ClubDonationSettingsForm(instance=club),
        forms.ClubAnnouncementForm(club=club),
    )
    return {
        name: str(field.label) for form in built for name, field in form.fields.items() if not field.widget.is_hidden
    }


#: Club settings no guide names, and why.
CLUB_SETTINGS_NOT_IN_HELP: dict[str, str] = dict.fromkeys(
    (
        "welcome_opening",
        "welcome_closing",
        "renewal_opening",
        "renewal_closing",
        "expiring_soon_opening",
        "expiring_soon_closing",
    ),
    "Typed into the email preview, which the guide describes as a whole.",
)


#: Pages that are deliberately in no guide.
NOT_IN_HELP: dict[str, str] = {
    "home": "The front page is where people start, not something to explain.",
    "tos": "Legal text; it says what it says.",
    "privacy_policy": "Legal text; it says what it says.",
    "dmca": "Legal text for rightsholders, linked from the footer.",
    "dmca_notice": "A form for rightsholders, reached from the DMCA page.",
    "support": "The contact page is where help ends, not a topic.",
    "faq": "Being replaced by these guides.",
    "auction_help": "Redirects into these guides.",
    "auction_survey": "One question, reached from its buttons in an email; the question is the whole page.",
    "print_my_unprinted_labels": "Reached from the buy-now sale email, which says what it prints.",
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
    "admin_early_adds": "Site operator only.",
    "admin_free_text": "Site operator only.",
    "admin_session_replay": "Site operator only.",
    "admin_user_map": "Site operator only.",
    "admin_user_signups": "Site operator only.",
    "admin_error": "Site operator only.",
    "all_my_users": "Site operator only.",
    "help": "This is the help.",
    "leaderboard": "No link on the site leads to it any more.",
    "auction_disable_bidding": "Unfinished: its button was taken off the users page (see the view's TODO).",
    "bulk_add_lots": "The older add-lots form; only the command palette opens it. The guides describe the grid every button opens.",
    "bulk_add_lots_for_myself": "The older add-lots form; only the command palette opens it. The guides describe the grid every button opens.",
    "user_api_keys": "Redirects into the AI agents guide, and takes that guide's forms.",
    "paypal_seller": "Redirects into the Card payments guide.",
    "square_seller": "Redirects into the Card payments guide.",
    "square_connect": "The connect button in the Card payments guide is drawn by help_tags.square_account, only for someone Square will take.",
    "paypal_connect": "PayPal is set up for a few clubs with their own credentials; the guides don't offer the older connect-your-own-account flow.",
    "auction_printable_lot_list": "Every lot on one page, under More; the page says what it is.",
    "bulk_add_image": "Reached from the Quick add images button on a person's Actions menu; the page steps through their lots.",
    "lot_by_pk_qr": "The address inside a label's QR code; people scan it, nobody navigates to it.",
    "all_auctions": "The same list as Auctions, at another address.",
    "invoice_by_pk": "One invoice. The guides link the reader's own; My Invoice and Invoices reach it.",
    "my_labels_by_username": "One person's labels by username; nothing links it. print_labels_by_bidder_number is the same page.",
}

#: Pages no guide covers yet. Only ever shrinks -- see the module docstring. Empty since every page was
#: written up; a new page goes in a guide or in NOT_IN_HELP, not here.
NOT_YET_DOCUMENTED: frozenset[str] = frozenset()

#: Auction rules no guide explains yet. Only ever shrinks, and is empty for the same reason.
RULES_NOT_YET_DOCUMENTED: frozenset[str] = frozenset()


# --- search -------------------------------------------------------------------


def _sections(slug: str) -> list[tuple[str, str, str]]:
    """``(anchor, heading, text)`` for each ``<h2 id=...>`` section of a guide, rendered for nobody."""
    html = get_template(GUIDES[slug].template_name).render({"help": HelpContext(), "guide": GUIDES[slug]})
    html = re.sub(r"<(script|svg)\b.*?</\1>", "", html, flags=re.DOTALL)
    parts = re.split(r"<h2[^>]*\bid=\"([^\"]+)\"[^>]*>(.*?)</h2>", html, flags=re.DOTALL)
    sections = [("", GUIDES[slug].title, parts[0])]
    for i in range(1, len(parts), 3):
        sections.append((parts[i], strip_tags(parts[i + 1]).strip(), parts[i + 2]))
    return [(anchor, heading, re.sub(r"\s+", " ", strip_tags(body)).strip()) for anchor, heading, body in sections]


def all_sections() -> tuple[tuple[str, str, str, str], ...]:
    """``(slug, anchor, heading, text)`` for every section of every guide. Rendered once a day in each process,
    since the guides quote numbers that are counted daily (``help_stats``).
    """
    return _all_sections(timezone.localdate())


@lru_cache(maxsize=1)
def _all_sections(_day) -> tuple[tuple[str, str, str, str], ...]:
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
        if not GUIDES[slug].shown:
            continue
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
    for group in shown_groups():
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
