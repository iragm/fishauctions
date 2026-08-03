"""Every page on the site, as a thing the command palette's assistant can reach.

The palette's natural-language assist used to navigate by running the ordinary palette search and
taking the first Go-To hit, which meant it could only reach the handful of destinations someone had
thought to add as a ``CommandPalettePage``. This module is the other half: a registry of **every**
named URL in the site, either as a navigable :class:`Route` or as an entry in :data:`EXCLUDED` with
a written reason.

That "or" is enforced, not aspirational. ``auctions/test_palette_routes.py`` walks the real URLconf
and fails if a name is in neither table, so adding a URL to ``urls.py`` and forgetting the palette
breaks the build. There is no third state where a page quietly becomes unreachable.

**Why a registry rather than one skill per URL.** 300-odd separate skills would be 300 parameter
schemas for the model to choose between, and the choice is always the same shape: a destination plus
the object it applies to. So there is one navigation skill (``go_to_page`` in ``palette_actions``)
backed by this catalog. The model still sees every destination -- :func:`catalog_for_prompt` writes
them all into the system prompt -- it just doesn't need a separate tool definition for each.

**Scopes.** A route's ``scope`` says where its URL parameters come from, and resolving them is this
module's job, not the model's. ``scope="auction"`` means "this URL needs an auction slug"; the
resolver finds the auction from the user's hint, the page they're on, or their most recent one, and
always through :func:`~auctions.command_palette._joined_auctions`, so a hint can never reach an
object the user has no relationship with. The model never supplies a URL or a primary key.

**Permissions.** ``admin`` marks routes that need auction-admin, club-admin or superuser rights.
This is a pre-filter for a better error message ("only admins can..."), *not* the security boundary
-- every destination is a normal Django view that runs its own checks when the user lands on it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from django.urls import NoReverseMatch, Resolver404, get_resolver, resolve, reverse

logger = logging.getLogger(__name__)

# --- scopes ------------------------------------------------------------------

SCOPE_NONE = ""
SCOPE_AUCTION = "auction"
SCOPE_AUCTION_BIDDER = "auction_bidder"
SCOPE_AUCTION_USERNAME = "auction_username"
SCOPE_CLUB = "club"
SCOPE_CLUB_TAB = "club_tab"
SCOPE_LOT = "lot"
SCOPE_INVOICE = "invoice"
SCOPE_LOCATION = "location"
SCOPE_MEMBER = "member"
SCOPE_USER = "user"
SCOPE_BLOG = "blog"

# Which permission a route needs before it's worth offering.
ADMIN_NONE = ""
ADMIN_AUCTION = "auction"
ADMIN_CLUB = "club"
ADMIN_SUPERUSER = "superuser"


@dataclass(frozen=True)
class Route:
    """One navigable page.

    ``key`` is the URL name, which is also what the model is asked to send back -- so the model's
    answer is checked against the URLconf rather than against a string we invented.
    """

    key: str
    label: str
    section: str
    scope: str = SCOPE_NONE
    admin: str = ADMIN_NONE
    keywords: tuple[str, ...] = ()
    #: Extra kwargs baked into reverse() for routes whose remaining parameters aren't user-facing.
    fixed: dict[str, Any] = field(default_factory=dict)
    #: Overrides the kwarg name the scope's object is passed as, for the handful of URLs that spell
    #: it differently (``add_image`` takes ``lot``, ``auction_no_show`` takes ``tos``).
    param: str = ""

    @property
    def search_text(self) -> str:
        return " ".join([self.label, self.key.replace("_", " "), *self.keywords]).lower()


def _r(key, label, section, scope=SCOPE_NONE, admin=ADMIN_NONE, keywords=(), fixed=None, param=""):
    return Route(
        key=key,
        label=label,
        section=section,
        scope=scope,
        admin=admin,
        keywords=tuple(keywords),
        fixed=dict(fixed or {}),
        param=param,
    )


# --- the catalog -------------------------------------------------------------
#
# Grouped by section, because the section headings are what the model sees in the prompt and they
# do most of the work of narrowing a query down.

ROUTE_LIST: list[Route] = [
    # --- Browsing ---
    _r("home", "Home page", "Browsing", keywords=["front page", "start"]),
    _r("allLots", "Browse all lots for sale", "Browsing", keywords=["lots", "shop", "buy", "search lots"]),
    _r("auctions", "Browse all auctions", "Browsing", keywords=["upcoming auctions", "find an auction"]),
    _r("all_auctions", "Browse all auctions (full list)", "Browsing", keywords=["every auction"]),
    _r("clubs", "Map of clubs", "Browsing", keywords=["club map", "find a club near me"]),
    _r("leaderboard", "Leaderboard", "Browsing", keywords=["top sellers", "rankings"]),
    _r("my_last_auction_lots", "Lots in my most recent auction", "Browsing", keywords=["lots near me"]),
    _r("user_lots", "Lots grouped by seller", "Browsing", keywords=["sellers", "who is selling"]),
    _r("userpage", "A seller's public page", "Browsing", scope=SCOPE_USER, keywords=["store", "profile", "user page"]),
    _r("blog_post", "A blog post", "Browsing", scope=SCOPE_BLOG, keywords=["article", "news"]),
    _r("promo", "About this site", "Browsing", keywords=["about", "what is this", "marketing"]),
    _r("faq", "Frequently asked questions", "Browsing", keywords=["faq", "help", "how does this work"]),
    _r("tos", "Terms of service", "Browsing", keywords=["terms", "user agreement", "rules of the site"]),
    _r("privacy_policy", "Privacy policy", "Browsing", keywords=["privacy", "data"]),
    _r("feedback", "Leave feedback about the site", "Browsing", keywords=["suggest", "bug report", "contact"]),
    _r("add_to_calendar", "Add auctions to my calendar", "Browsing", keywords=["calendar", "ical", "subscribe"]),
    # --- My stuff ---
    _r("selling", "Lots I am selling", "My stuff", keywords=["my lots", "what am i selling"]),
    _r("watched", "Lots I am watching", "My stuff", keywords=["watchlist", "watched", "saved lots"]),
    _r("won_lots", "Lots I won", "My stuff", keywords=["bought", "purchases", "what did i win"]),
    _r("my_bids", "My bids", "My stuff", keywords=["bids i placed", "bidding history"]),
    _r("my_invoices", "My invoices", "My stuff", keywords=["invoice", "what do i owe", "bill", "receipt"]),
    _r("invoice_by_pk", "One specific invoice", "My stuff", scope=SCOPE_INVOICE, keywords=["invoice"]),
    _r("new_lot", "Add a single lot", "My stuff", keywords=["sell something", "list an item", "new lot"]),
    _r("messages", "Chat messages I follow", "My stuff", keywords=["messages", "chat", "notifications"]),
    _r("my_lot_report", "Download my lots as a spreadsheet", "My stuff", keywords=["csv", "export my lots"]),
    _r("my_won_lot_csv", "Download lots I won as a spreadsheet", "My stuff", keywords=["csv", "export purchases"]),
    _r("lot_by_pk", "A specific lot's page", "My stuff", scope=SCOPE_LOT, keywords=["view lot", "open lot"]),
    _r("edit_lot", "Edit a lot", "My stuff", scope=SCOPE_LOT, keywords=["change lot", "fix lot", "update lot"]),
    _r("delete_lot", "Delete a lot", "My stuff", scope=SCOPE_LOT, keywords=["remove lot", "cancel lot"]),
    _r(
        "add_image",
        "Add a photo to a lot",
        "My stuff",
        scope=SCOPE_LOT,
        keywords=["picture", "photo", "image"],
        param="lot",
    ),
    _r(
        "single_lot_label", "Print one lot's label", "My stuff", scope=SCOPE_LOT, keywords=["label", "print", "sticker"]
    ),
    _r("lot_by_pk_qr", "QR code for a lot", "My stuff", scope=SCOPE_LOT, keywords=["qr", "barcode"]),
    # --- Account ---
    _r("account", "My account", "Account", keywords=["profile", "settings", "my account"]),
    _r("preferences", "Notification and display preferences", "Account", keywords=["settings", "emails", "opt out"]),
    _r("contact_info", "My contact details and address", "Account", keywords=["address", "phone", "location"]),
    _r("change_username", "Change my username", "Account", keywords=["username", "rename"]),
    _r("ignore_categories", "Categories to hide", "Account", keywords=["ignore", "hide categories", "mute"]),
    _r("printing", "Label printing preferences", "Account", keywords=["printer", "label size", "thermal"]),
    _r("account_delete", "Delete my account", "Account", keywords=["close account", "delete me", "gdpr"]),
    _r("paypal_seller", "My PayPal payout settings", "Account", keywords=["paypal", "get paid", "payout"]),
    _r("paypal_connect", "Connect PayPal", "Account", keywords=["link paypal", "set up paypal"]),
    _r("paypal_seller_delete", "Disconnect PayPal", "Account", keywords=["unlink paypal", "remove paypal"]),
    _r("square_seller", "My Square payout settings", "Account", keywords=["square", "card reader", "tap to pay"]),
    _r("square_connect", "Connect Square", "Account", keywords=["link square", "set up square"]),
    _r("square_seller_delete", "Disconnect Square", "Account", keywords=["unlink square", "remove square"]),
    # --- In an auction (anyone) ---
    _r("auction_main", "An auction's front page", "Auction", scope=SCOPE_AUCTION, keywords=["auction", "details"]),
    _r("auction_lot_list", "All lots in an auction", "Auction", scope=SCOPE_AUCTION, keywords=["lots", "catalog"]),
    _r("my_auction_invoice", "My invoice for an auction", "Auction", scope=SCOPE_AUCTION, keywords=["invoice", "owe"]),
    _r("auction_help", "Auction help and rules", "Auction", scope=SCOPE_AUCTION, keywords=["rules", "how it works"]),
    _r("auction_chat", "Auction chat", "Auction", scope=SCOPE_AUCTION, keywords=["chat", "questions", "messages"]),
    _r("auction_stats", "Auction statistics", "Auction", scope=SCOPE_AUCTION, keywords=["stats", "numbers", "charts"]),
    _r("auction_lot_map", "Map of where lots are", "Auction", scope=SCOPE_AUCTION, keywords=["map", "tables", "where"]),
    _r("auction_volunteers", "Volunteer for a job", "Auction", scope=SCOPE_AUCTION, keywords=["volunteer", "help out"]),
    _r("auction_door_prizes", "Door prizes", "Auction", scope=SCOPE_AUCTION, keywords=["raffle", "prizes", "giveaway"]),
    _r(
        "auction_self_check_in", "Check myself in", "Auction", scope=SCOPE_AUCTION, keywords=["self check in", "arrive"]
    ),
    _r(
        "print_my_labels",
        "Print my labels for an auction",
        "Auction",
        scope=SCOPE_AUCTION,
        keywords=["labels", "print", "stickers"],
    ),
    _r(
        "print_my_unprinted_labels",
        "Print only my labels that haven't been printed",
        "Auction",
        scope=SCOPE_AUCTION,
        keywords=["unprinted", "new labels"],
    ),
    _r(
        "bulk_add_lots_for_myself",
        "Add several of my own lots at once",
        "Auction",
        scope=SCOPE_AUCTION,
        keywords=["bulk add", "add lots", "many lots"],
    ),
    _r(
        "bulk_add_lots_auto_for_myself",
        "Add my lots with automatic photos",
        "Auction",
        scope=SCOPE_AUCTION,
        keywords=["bulk add auto", "add lots with pictures"],
    ),
    _r("auction_confirm", "Confirm joining an auction", "Auction", keywords=["join auction", "sign up", "agree"]),
    _r("lot_list", "Download an auction's lot list", "Auction", scope=SCOPE_AUCTION, keywords=["csv", "export lots"]),
    # --- Running an auction (admins) ---
    _r("create_auction", "Create a new auction", "Running an auction", keywords=["new auction", "start an auction"]),
    _r(
        "edit_auction",
        "Auction settings",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["settings", "edit auction", "configure", "change the date", "rules"],
    ),
    _r(
        "edit_auction_custom_fields",
        "Auction custom fields",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["custom field", "extra field", "dropdown"],
    ),
    _r(
        "auction_tos_list",
        "People in an auction",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["users", "bidders", "participants", "who is coming"],
    ),
    _r(
        "auction_invoices",
        "All invoices for an auction",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["invoices", "who owes", "payments"],
    ),
    _r(
        "auction_lot_winners_dynamic",
        "Record sales as the auctioneer calls them",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["set winners", "sell lots", "auctioneer", "sold"],
    ),
    _r(
        "auction_quick_checkout",
        "Check people out",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["checkout", "take payment", "cash out"],
    ),
    _r(
        "auction_quick_check_in",
        "Check people in",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["check in", "arrivals", "front desk"],
    ),
    _r(
        "auction_lot_queue",
        "Lot queue for the auctioneer",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["queue", "running order", "what's next"],
    ),
    _r(
        "auction_lot_queue_kiosk",
        "Lot queue on a second screen",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["kiosk", "projector", "display"],
    ),
    _r(
        "auction_printing",
        "Print labels for everyone",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["bulk printing", "all labels"],
    ),
    _r(
        "auction_printing_pdf",
        "Download everyone's labels as a PDF",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["pdf labels"],
    ),
    _r(
        "auction_label_config",
        "Label layout for an auction",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["label setup", "label size", "printer setup"],
    ),
    _r(
        "bulk_add_users",
        "Add several people to an auction",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["add users", "add bidders", "import people"],
    ),
    _r(
        "import_from_google_drive",
        "Import people from Google Drive",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["google sheet", "import", "drive"],
    ),
    _r(
        "sync_google_drive",
        "Re-sync people from Google Drive",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["resync", "google sheet"],
    ),
    _r(
        "import_lots_from_csv",
        "Import lots from a spreadsheet",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["csv", "import lots", "upload lots"],
    ),
    _r(
        "compose_email_to_users",
        "Email everyone in an auction",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["email", "message everyone", "announcement"],
    ),
    _r(
        "auction_add_users_to_club",
        "Add auction attendees to the club",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["add to club", "make members"],
    ),
    _r(
        "user_list",
        "Auction report",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["report", "summary", "who came"],
    ),
    _r(
        "auction_history",
        "Auction change history",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["history", "audit", "who changed what"],
    ),
    _r(
        "auction_pickup_location",
        "Pickup locations",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["location", "venue", "where is it", "pickup"],
    ),
    _r(
        "create_auction_pickup_location",
        "Add a pickup location",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["new location", "add venue", "set the address"],
    ),
    _r(
        "auction_disable_bidding",
        "Turn bidding off for people who haven't paid",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["disable bidding", "block bidders"],
    ),
    _r(
        "auction_lot_map_clear",
        "Clear the lot map",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["reset map", "clear tables"],
    ),
    _r(
        "auction_voice_command_log",
        "Voice command log",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["voice", "speech log", "what did it hear"],
    ),
    _r(
        "auction_delete",
        "Delete an auction",
        "Running an auction",
        scope=SCOPE_AUCTION,
        admin=ADMIN_AUCTION,
        keywords=["delete auction", "cancel auction"],
    ),
    _r(
        "bulk_add_lots",
        "Add lots for one bidder",
        "Running an auction",
        scope=SCOPE_AUCTION_BIDDER,
        admin=ADMIN_AUCTION,
        keywords=["add lots for", "bulk add for bidder"],
    ),
    _r(
        "bulk_add_lots_auto",
        "Add lots for one bidder, with automatic photos",
        "Running an auction",
        scope=SCOPE_AUCTION_BIDDER,
        admin=ADMIN_AUCTION,
        keywords=["bulk add auto for bidder"],
    ),
    _r(
        "bulk_add_image",
        "Add photos for one bidder's lots",
        "Running an auction",
        scope=SCOPE_AUCTION_BIDDER,
        admin=ADMIN_AUCTION,
        keywords=["photos for bidder", "pictures"],
    ),
    _r(
        "print_labels_by_bidder_number",
        "Print one bidder's labels",
        "Running an auction",
        scope=SCOPE_AUCTION_BIDDER,
        admin=ADMIN_AUCTION,
        keywords=["labels for bidder", "print for"],
    ),
    _r(
        "print_unprinted_labels_by_bidder_number",
        "Print one bidder's unprinted labels",
        "Running an auction",
        scope=SCOPE_AUCTION_BIDDER,
        admin=ADMIN_AUCTION,
        keywords=["unprinted labels for bidder"],
    ),
    _r(
        "auction_no_show",
        "Deal with someone who didn't show up",
        "Running an auction",
        scope=SCOPE_AUCTION_BIDDER,
        admin=ADMIN_AUCTION,
        keywords=["no show", "didn't come", "absent"],
        param="tos",
    ),
    _r(
        "my_labels_by_username",
        "Print one user's labels",
        "Running an auction",
        scope=SCOPE_AUCTION_USERNAME,
        admin=ADMIN_AUCTION,
        keywords=["labels for user"],
    ),
    # --- Club ---
    _r("club_detail", "A club's page", "Club", scope=SCOPE_CLUB, keywords=["club", "club home"]),
    _r(
        "club_detail_tab",
        "A club's Breeder Award / culture pages",
        "Club",
        scope=SCOPE_CLUB_TAB,
        keywords=["bap", "hap", "culture", "my points", "breeder award"],
    ),
    _r("club_membership_pay", "Pay or renew my club membership", "Club", scope=SCOPE_CLUB, keywords=["renew", "dues"]),
    _r("club_events_ical", "Club calendar feed", "Club", scope=SCOPE_CLUB, keywords=["calendar", "ics", "events"]),
    _r("club_events_embed", "Embeddable club events", "Club", scope=SCOPE_CLUB, keywords=["embed", "widget"]),
    _r("bap_embed", "Embeddable Breeder Award list", "Club", scope=SCOPE_CLUB, keywords=["bap embed", "widget"]),
    _r(
        "club_admin",
        "Club member list",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["members", "membership list", "who is a member"],
    ),
    _r(
        "club_setup",
        "Club setup checklist",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["setup", "getting started", "checklist"],
    ),
    _r(
        "club_edit",
        "Club settings",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["edit club", "club details", "logo"],
    ),
    _r(
        "club_membership_settings",
        "Membership settings and pricing",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["dues", "membership price", "renewal"],
    ),
    _r(
        "club_email_settings",
        "Club email settings",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["email", "sender", "from address"],
    ),
    _r(
        "club_link_payment_account",
        "Link a payment account to the club",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["payments", "paypal", "square", "take money"],
    ),
    _r(
        "club_paypal_credentials",
        "Club PayPal credentials",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["paypal keys", "paypal setup"],
    ),
    _r(
        "club_history",
        "Club change history",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["history", "audit log"],
    ),
    _r(
        "club_stats",
        "Club statistics",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["stats", "growth", "numbers"],
    ),
    _r(
        "club_member_map",
        "Map of club members",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["map", "where members live"],
    ),
    _r(
        "club_member_import",
        "Import members from a spreadsheet",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["csv import", "upload members"],
    ),
    _r(
        "club_member_export",
        "Download the member list",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["csv export", "download members"],
    ),
    _r(
        "club_treasurer_report",
        "Treasurer report",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["treasurer", "money", "accounts", "finances"],
    ),
    _r(
        "club_treasurer_report_export",
        "Download the treasurer report",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["export finances"],
    ),
    _r(
        "club_money_add",
        "Record money in or out",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["expense", "income", "add transaction"],
    ),
    _r(
        "club_money_balance",
        "Club balance",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["balance", "how much money"],
    ),
    _r(
        "club_bap",
        "Breeder Award admin",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["bap", "breeder award", "spawns"],
    ),
    _r(
        "club_bap_lots",
        "Breeder Award lots",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["bap lots", "award lots"],
    ),
    _r(
        "club_bap_settings",
        "Breeder Award settings",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["bap points", "award rules"],
    ),
    _r(
        "club_bap_import",
        "Import Breeder Award records",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["bap import", "upload awards"],
    ),
    _r(
        "club_barcode_labels",
        "Print membership barcodes",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["barcodes", "member cards", "scan"],
    ),
    _r(
        "club_barcode_labels_pdf",
        "Download membership barcodes as a PDF",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["barcode pdf"],
    ),
    _r(
        "club_event_add",
        "Add a club event",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["event", "meeting", "add to calendar"],
    ),
    _r(
        "club_api_keys",
        "Club API keys",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["api", "integration", "token"],
    ),
    _r(
        "club_api_key_create",
        "Create a club API key",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["new api key"],
    ),
    _r(
        "club_mailchimp_config",
        "Mailchimp settings",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["mailchimp", "newsletter"],
    ),
    _r(
        "mailchimp_connect",
        "Connect Mailchimp",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["link mailchimp"],
    ),
    _r(
        "mailchimp_select_audience",
        "Choose a Mailchimp audience",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["mailchimp list"],
    ),
    _r(
        "mailchimp_sync_now",
        "Sync members to Mailchimp now",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["mailchimp sync"],
    ),
    _r(
        "mailchimp_disconnect",
        "Disconnect Mailchimp",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["unlink mailchimp"],
    ),
    _r(
        "club_brevo_config",
        "Brevo settings",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["brevo", "sendinblue", "newsletter"],
    ),
    _r("brevo_connect", "Connect Brevo", "Club admin", scope=SCOPE_CLUB, admin=ADMIN_CLUB, keywords=["link brevo"]),
    _r(
        "brevo_select_list",
        "Choose a Brevo list",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["brevo list"],
    ),
    _r(
        "brevo_sync_now",
        "Sync members to Brevo now",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["brevo sync"],
    ),
    _r(
        "brevo_disconnect",
        "Disconnect Brevo",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["unlink brevo"],
    ),
    _r(
        "club_google_calendar_config",
        "Google Calendar settings",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["google calendar", "calendar sync"],
    ),
    _r(
        "google_calendar_connect",
        "Connect Google Calendar",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["link google calendar"],
    ),
    _r(
        "google_calendar_sync_now",
        "Sync events to Google Calendar now",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["calendar sync"],
    ),
    _r(
        "google_calendar_disconnect",
        "Disconnect Google Calendar",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["unlink google calendar"],
    ),
    _r(
        "club_discord_config",
        "Discord settings",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["discord", "bot", "server"],
    ),
    _r(
        "club_discord_fetch_roles",
        "Refresh Discord roles",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["discord roles"],
    ),
    _r(
        "club_discord_send_join_message",
        "Post the Discord join message",
        "Club admin",
        scope=SCOPE_CLUB,
        admin=ADMIN_CLUB,
        keywords=["discord invite"],
    ),
    _r(
        "club_member_renew_page",
        "Set a member's expiry date",
        "Club admin",
        scope=SCOPE_MEMBER,
        admin=ADMIN_CLUB,
        keywords=["renew member", "extend membership", "expiry"],
    ),
    _r(
        "club_member_merge",
        "Merge two member records",
        "Club admin",
        scope=SCOPE_MEMBER,
        admin=ADMIN_CLUB,
        keywords=["merge", "duplicate member"],
    ),
    # --- Pickup locations ---
    _r(
        "edit_pickup",
        "Edit a pickup location",
        "Pickup locations",
        scope=SCOPE_LOCATION,
        admin=ADMIN_AUCTION,
        keywords=["edit location", "change venue"],
    ),
    _r(
        "delete_pickup",
        "Delete a pickup location",
        "Pickup locations",
        scope=SCOPE_LOCATION,
        admin=ADMIN_AUCTION,
        keywords=["remove location"],
    ),
    _r(
        "location_incoming",
        "Lots arriving at a location",
        "Pickup locations",
        scope=SCOPE_LOCATION,
        admin=ADMIN_AUCTION,
        keywords=["incoming lots", "drop off"],
    ),
    _r(
        "location_outgoing",
        "Lots leaving a location",
        "Pickup locations",
        scope=SCOPE_LOCATION,
        admin=ADMIN_AUCTION,
        keywords=["outgoing lots", "pick up"],
    ),
    # --- Site administration (superusers) ---
    _r(
        "admin_dashboard",
        "Site admin dashboard",
        "Site admin",
        admin=ADMIN_SUPERUSER,
        keywords=["dashboard", "site stats"],
    ),
    _r(
        "admin_setup_checklist",
        "Site setup checklist",
        "Site admin",
        admin=ADMIN_SUPERUSER,
        keywords=["setup", "env", "integrations", "configure the site"],
    ),
    _r(
        "command_palette_analytics",
        "Command palette analytics",
        "Site admin",
        admin=ADMIN_SUPERUSER,
        keywords=["palette", "what do people search for", "llm usage"],
    ),
    _r("admin_traffic", "Site traffic", "Site admin", admin=ADMIN_SUPERUSER, keywords=["traffic", "pageviews"]),
    _r("admin_referrers", "Where visitors come from", "Site admin", admin=ADMIN_SUPERUSER, keywords=["referrers"]),
    _r("admin_user_flow", "How visitors move around", "Site admin", admin=ADMIN_SUPERUSER, keywords=["user flow"]),
    _r("admin_user_map", "Map of users", "Site admin", admin=ADMIN_SUPERUSER, keywords=["user map", "where users are"]),
    _r("admin_user_signups", "New signups", "Site admin", admin=ADMIN_SUPERUSER, keywords=["signups", "new users"]),
    _r("admin_error", "Trigger a test error", "Site admin", admin=ADMIN_SUPERUSER, keywords=["test error", "500"]),
    _r("all_my_users", "Marketing list", "Site admin", admin=ADMIN_SUPERUSER, keywords=["marketing", "everyone"]),
]

ROUTES: dict[str, Route] = {route.key: route for route in ROUTE_LIST}


# --- deliberately not navigable ----------------------------------------------
#
# The reason strings are the point of this table: a URL is not allowed to be silently missing from
# the palette, so anything that isn't a destination has to say why here. The audit test reads it.

_API = "JSON/HTMX endpoint that returns a fragment, not a page a person can be sent to."
_WEBHOOK = "Webhook. Called by another server, never by a person."
_CALLBACK = "OAuth callback. Only ever reached by a redirect back from the provider."
_AUTOCOMPLETE = "Autocomplete feed for a form widget."
_TOKEN = "Reached only from an emailed link containing a token the user can't be asked for."
_INFRA = "Infrastructure route with no user-facing page."
_MOBILE = "Handled inside the mobile app's own flow."
_DUPLICATE = "Same page as another entry in the catalog."
_ACTION_ONLY = "POST-only action; the palette has a real skill for this instead of navigating."

EXCLUDED: dict[str, str] = {
    # Autocomplete feeds
    "auctiontos-autocomplete": _AUTOCOMPLETE,
    "lot-autocomplete": _AUTOCOMPLETE,
    "auction-autocomplete": _AUTOCOMPLETE,
    "club-member-autocomplete": _AUTOCOMPLETE,
    "club-member-merge-autocomplete": _AUTOCOMPLETE,
    "category-autocomplete": _AUTOCOMPLETE,
    "clubmember_validation": _AUTOCOMPLETE,
    "auctiontos_validation": _AUTOCOMPLETE,
    "check_username": _AUTOCOMPLETE,
    "guess_category": _AUTOCOMPLETE,
    "get_auction_info": _API,
    # JSON / HTMX fragments
    "get_ad": _API,
    "click_ad": _API,
    "pageview": _API,
    "htmx_lot": _API,
    "lot_bid": _API,
    "lot_push_test": _API,
    "enable_notifications": _API,
    "lot_chat_subscribe": _API,
    "delete_auction_chat": _API,
    "auctionlotadmin": _API,
    "lot_bap_points": _API,
    "auction_custom_dropdown_options": _API,
    "auctiontosadmin": _API,
    "auctiontosdelete": _API,
    "auctiontosmemo": _API,
    "auction_check_in": _ACTION_ONLY,
    "add_single_auctiontos_to_club": _API,
    "save_lot_ajax": _API,
    "auction_barcode_scan": _API,
    "auction_quick_checkout_htmx": _API,
    "auction_lot_map_data": _API,
    "auction_show_high_bidder": _API,
    "auto_image_available": _API,
    "auction_no_show_dialog": _API,
    "lot_refund": _API,
    "bulk_set_lots_won": _API,
    "auction_unsell_lot": _ACTION_ONLY,
    "auction_enable_bidding_for_all": _API,
    "auction_invoices_ready": _API,
    "auction_invoices_paid": _API,
    "invoice_renewal_toggle": _API,
    "create_paypal_order": _API,
    "create_square_payment_link": _API,
    "clubmember_admin": _API,
    "clubmember_permissions": _API,
    "clubmember_discord": _API,
    "clubmember_create": _API,
    "club_member_renew": _API,
    "club_member_membership_number": _API,
    "club_member_resend_card": _API,
    "club_member_delete": _API,
    "club_member_permanent_delete": _API,
    "club_member_reactivate": _API,
    "club_member_confirm": _API,
    "bapaward_create": _API,
    "bapaward_admin": _API,
    "bapaward_delete": _API,
    "club_bap_lot_category": _API,
    "club_bap_category_override_save": _API,
    "club_bap_category_override_delete": _API,
    "club_api_key_detail": _API,
    "club_api_key_revoke": _API,
    "club_api_key_mapping_add": _API,
    "club_api_key_mapping_delete": _API,
    "club_discord_edit_role": _API,
    "club_discord_set_default_role": _API,
    "club_event_edit": _API,
    "club_barcode": _API,
    "club_barcode_png": _API,
    "auction_volunteer_job": _API,
    "delete_image": _API,
    "edit_image": _API,
    "delete_bid": _API,
    "create_invoice": _API,
    "auction_funnel_chart": _API,
    "auction_lot_bidders": _API,
    "auction_lot_categories": _API,
    "auction_sell_prices": _API,
    "auction_stats_attrition": _API,
    "auction_stats_auctioneer": _API,
    "auction_stats_activity": _API,
    "auction_stats_pictures": _API,
    "auction_stats_distance_traveled": _API,
    "auction_stats_previous_auctions": _API,
    "auction_stats_lots_submitted": _API,
    "auction_stats_location_volume": _API,
    "auction_stats_feature_use": _API,
    "auction_stats_referrers": _API,
    "admin_traffic_json": _API,
    "admin_traffic_time_of_day_json": _API,
    "admin_user_signups_json": _API,
    "api_club_members": _API,
    "api_club_member_detail": _API,
    "api_club_member_renew": _API,
    "api_club_member_bap_awards": _API,
    "inbound_email_routing": _API,
    # Webhooks and machine-to-machine
    "paypal-webhook": _WEBHOOK,
    "club_paypal_subscription_webhook": _WEBHOOK,
    "square_webhook": _WEBHOOK,
    "mailchimp_webhook": _WEBHOOK,
    "brevo_webhook": _WEBHOOK,
    "handle-event-webhook": _WEBHOOK,
    "apple_server_notifications": _WEBHOOK,
    "discord_interactions": _WEBHOOK,
    "passkit_registration": _WEBHOOK,
    "passkit_device_registrations": _WEBHOOK,
    "passkit_pass": _WEBHOOK,
    "passkit_log": _WEBHOOK,
    # OAuth callbacks
    "paypal_callback": _CALLBACK,
    "square_callback": _CALLBACK,
    "mailchimp_callback": _CALLBACK,
    "google_calendar_callback": _CALLBACK,
    "paypal_success": _CALLBACK,
    "square_success": _CALLBACK,
    "square_payment_success": _CALLBACK,
    # Links from emails, containing a token or uuid nobody can type
    "invoice_no_login": _TOKEN,
    "club_member_by_uuid": _TOKEN,
    "club_member_by_number": _TOKEN,
    "club_member_unsubscribe": _TOKEN,
    "club_member_resubscribe": _TOKEN,
    "club_member_nocomm": _TOKEN,
    "club_member_contact_pref": _TOKEN,
    "club_member_apple_wallet": _TOKEN,
    "club_member_apple_wallet_by_uuid": _TOKEN,
    "account_deleted": "Shown once, after the account is already gone and the user is signed out.",
    # Infrastructure
    "site_webmanifest": _INFRA,
    "command_palette": _INFRA,
    "command_palette_log": _INFRA,
    "command_palette_assist": _INFRA,
    "command_palette_execute": _INFRA,
    "mobile_socialaccount_signup": _MOBILE,
    "mobile_socialaccount_connections": _MOBILE,
    "paypal_csv": "Needs a chunk number that only makes sense from the invoices page it's linked from.",
    # Duplicates of catalog entries
    "lot_by_pk_and_slug": _DUPLICATE,
    "lot_in_auction": _DUPLICATE,
    "lot_in_auction_with_slug": _DUPLICATE,
    "service_worker": _INFRA,
}

#: Whole families of routes excluded by prefix, with one reason for the family. Used where listing
#: every name would be noise -- the mobile API alone is 40-odd endpoints that are all the same kind
#: of thing. A prefix here still has to be a deliberate decision; it just isn't repeated 40 times.
EXCLUDED_PREFIXES: dict[str, str] = {
    "mobile-": "Mobile app JSON API. The app has its own screens; these return data, not pages.",
}


# --- audit -------------------------------------------------------------------


def _url_names() -> set[str]:
    """Every named route in the project's URLconf.

    ``reverse_dict`` is keyed by both the name and the view callable, so only the string keys are
    names. Namespaced includes are recorded as ``namespace:*`` and skipped by the audit -- they
    belong to third-party apps, which have their own entry in ``THIRD_PARTY_PREFIXES``.
    """
    resolver = get_resolver()
    names = {key for key in resolver.reverse_dict if isinstance(key, str)}
    names.update(f"{namespace}:*" for namespace in resolver.namespace_dict)
    return names


#: URL names owned by third-party apps (allauth's login flows, summernote, webpush). They're
#: outside this project's control and none of them are destinations a person would ask the palette
#: for, so they're skipped wholesale rather than listed one by one. Anything this module classifies
#: explicitly wins over these patterns, so the site's own ``account_delete`` isn't mistaken for one
#: of allauth's ``account_*`` views.
THIRD_PARTY_PREFIXES = (
    "account_",
    "socialaccount_",
    "webpush",
    "django-admindocs",
    "django_summernote",
    "google_",
    "apple_",
    "facebook_",
)
THIRD_PARTY_NAMES = {
    "set_language",
    "javascript-catalog",
    "robots.txt",
    "ads.txt",
    "unsubscribe",
    "save_webpush_info",
}


def excluded_reason(name: str) -> str:
    """Why ``name`` isn't a palette destination, or ``''`` if it is one (or unclassified)."""
    if name in EXCLUDED:
        return EXCLUDED[name]
    for prefix, reason in EXCLUDED_PREFIXES.items():
        if name.startswith(prefix):
            return reason
    return ""


def is_third_party(name: str) -> bool:
    if name in ROUTES or name in EXCLUDED:
        return False
    return name.startswith(THIRD_PARTY_PREFIXES) or name in THIRD_PARTY_NAMES or ":" in name


def audit() -> dict[str, list[str]]:
    """Compare the live URLconf against this module. Used by the route audit test.

    ``uncovered`` is the one that matters: a name there is a page the assistant can't reach and
    nobody has said why. ``stale`` catches the opposite -- a route we still describe after the URL
    it points at has been renamed or deleted, which would fail at reverse() time in front of a user.
    """
    live = {name for name in _url_names() if not is_third_party(name) and not name.endswith(":*")}
    known = set(ROUTES) | set(EXCLUDED)
    return {
        "covered": sorted(name for name in live if name in ROUTES),
        "excluded": sorted(name for name in live if excluded_reason(name)),
        "uncovered": sorted(name for name in live if name not in ROUTES and not excluded_reason(name)),
        "stale": sorted(known - live),
    }


# --- prompt catalog ----------------------------------------------------------


def catalog_for_prompt(user=None) -> str:
    """The destination list written into the system prompt, grouped by section.

    Filtered to what this user could plausibly use, so an ordinary bidder isn't offered club
    administration and the model doesn't spend its attention on pages it will only be refused.
    """
    from . import command_palette

    can_admin_auction = True
    can_admin_club = True
    is_superuser = bool(user and getattr(user, "is_superuser", False))
    if user is not None:
        can_admin_club = bool(command_palette._admin_clubs(user))
        can_admin_auction = bool(command_palette._admin_auction_ids(user))

    sections: dict[str, list[str]] = {}
    for route in ROUTE_LIST:
        if route.admin == ADMIN_SUPERUSER and not is_superuser:
            continue
        if route.admin == ADMIN_AUCTION and not can_admin_auction:
            continue
        if route.admin == ADMIN_CLUB and not can_admin_club:
            continue
        sections.setdefault(route.section, []).append(f"  {route.key}: {route.label}")
    lines = []
    for section, entries in sections.items():
        lines.append(f"{section}:")
        lines.extend(entries)
    return "\n".join(lines)


# --- matching ----------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]+")

#: Words that carry no signal when matching a query against a destination label.
_STOPWORDS = frozenset(
    """a an and are as at be by can could do does for from get go going have how i in into is it its
    me my need of on or our page please put show take that the their them then there these this to
    up us want was what when where which who will with would you your""".split()
)


def _tokens(text: str) -> list[str]:
    return [word for word in _WORD.findall(text.lower()) if word not in _STOPWORDS]


def match_routes(query: str, user=None, limit: int = 5) -> list[Route]:
    """Rank destinations against a free-text query.

    Deliberately simple token overlap rather than anything clever: this is the safety net for when
    the model sends a description instead of a key, and a wrong guess here costs a clarify, not a
    wrong action.
    """
    words = _tokens(query)
    if not words:
        return []
    scored: list[tuple[float, int, Route]] = []
    for index, route in enumerate(ROUTE_LIST):
        haystack = route.search_text
        hay_tokens = set(_tokens(haystack))
        score = 0.0
        for word in words:
            if word in hay_tokens:
                score += 2.0
            elif len(word) > 3 and word in haystack:
                score += 1.0
        if route.label.lower() in query.lower():
            score += 3.0
        if score:
            # Index keeps the ordering stable and biases towards the earlier, more common entries.
            scored.append((score, -index, route))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [route for _, _, route in scored[:limit]]


def get_route(key: str) -> Route | None:
    if not isinstance(key, str):
        return None
    return ROUTES.get(key.strip().lower())


# --- resolving a route to a URL ----------------------------------------------


def _club_from_hint(user, hint: str):
    """Find a club the user belongs to or administers, by name, abbreviation or slug."""
    from . import command_palette

    clubs = list(command_palette._admin_clubs(user))
    from .models import ClubMember

    for member in ClubMember.objects.filter(user=user, is_deleted=False).select_related("club"):
        if member.club and member.club not in clubs:
            clubs.append(member.club)
    if not hint:
        preferred = command_palette._palette_club(user)
        if preferred:
            return preferred
        return clubs[0] if len(clubs) == 1 else None
    lowered = hint.lower()
    for attribute in ("slug", "name", "abbreviation"):
        for club in clubs:
            value = (getattr(club, attribute, "") or "").lower()
            if value and value == lowered:
                return club
    for club in clubs:
        if lowered in (club.name or "").lower():
            return club
    return None


def _lot_from_hint(user, hint: str, auction=None):
    """Find a lot by database id, lot number, or name, within what the user can see."""
    from django.db.models import Q

    from . import command_palette
    from .models import Lot

    hint = (hint or "").strip()
    if not hint:
        return None
    lots = Lot.objects.filter(is_deleted=False)
    if not user.is_superuser:
        lots = lots.filter(Q(user=user) | Q(auction__in=command_palette._joined_auctions(user)))
    if auction:
        lots = lots.filter(auction=auction)
    if hint.isdigit():
        by_pk = lots.filter(pk=int(hint)).first()
        if by_pk:
            return by_pk
    by_number = lots.filter(custom_lot_number__iexact=hint).first()
    if by_number:
        return by_number
    return lots.filter(lot_name__icontains=hint).first()


def _denied(message: str) -> dict[str, Any]:
    """A permission refusal.

    Flagged so ``go_to_page`` can tell it apart from "I couldn't work out what you meant". Those
    two must not be handled the same way: guessing another page after a refusal would quietly take
    the user somewhere they didn't ask for and hide the fact that they aren't allowed in.
    """
    return {"error": message, "denied": True}


def resolve_route(request, route: Route, params: dict[str, Any]) -> dict[str, Any]:
    """Turn a route plus the model's parameters into ``{"url": ...}`` or a problem.

    Returns the same result shapes the action resolvers use, so the caller doesn't need to know
    whether an action navigated or acted.
    """
    from . import palette_actions

    user = request.user
    kwargs: dict[str, Any] = dict(route.fixed)
    hint = str(params.get("target") or "").strip()

    page = palette_actions._page(request)
    if route.scope in (SCOPE_AUCTION, SCOPE_AUCTION_BIDDER, SCOPE_AUCTION_USERNAME):
        auction, error = palette_actions.resolve_auction(user, hint if route.scope == SCOPE_AUCTION else "", page)
        if error:
            return {"error": error}
        if route.admin == ADMIN_AUCTION and not palette_actions._is_auction_admin(user, auction):
            return _denied(f"Only admins of {auction.title} can open that page.")
        kwargs["slug"] = auction.slug
        if route.scope == SCOPE_AUCTION_BIDDER:
            tos, problem = palette_actions.resolve_person(user, auction, hint)
            if problem:
                return problem
            if not tos.bidder_number:
                return {"error": f"{tos.name or 'That person'} doesn't have a bidder number yet."}
            kwargs[route.param or "bidder_number"] = tos.bidder_number
        if route.scope == SCOPE_AUCTION_USERNAME:
            tos, problem = palette_actions.resolve_person(user, auction, hint)
            if problem:
                return problem
            if not (tos.user and tos.user.username):
                return {"error": f"{tos.name or 'That person'} doesn't have an account, so they have no user page."}
            kwargs["username"] = tos.user.username

    elif route.scope in (SCOPE_CLUB, SCOPE_CLUB_TAB):
        club = _club_from_hint(user, hint or (page.get("club") or ""))
        if not club:
            return {"error": "I couldn't work out which club you mean."}
        if route.admin == ADMIN_CLUB:
            from . import command_palette

            if not command_palette._can_manage_members(user, club):
                return _denied(f"Only admins of {club.name} can open that page.")
        kwargs["slug"] = club.slug
        if route.scope == SCOPE_CLUB_TAB:
            tab = str(params.get("tab") or "bap").strip().lower()
            if tab not in {"bap", "hap", "culture", "my-points"}:
                tab = "bap"
            kwargs["tab"] = tab

    elif route.scope == SCOPE_LOT:
        # "print this label" while looking at a lot page means *that* lot.
        lot = _lot_from_hint(user, hint or str(params.get("lot_id") or "") or str(page.get("lot_id") or ""))
        if not lot:
            return {"error": "I couldn't work out which lot you mean — give me a lot number or name."}
        kwargs[route.param or "pk"] = lot.pk

    elif route.scope == SCOPE_INVOICE:
        from . import command_palette

        invoices = command_palette._recent_invoices(user, limit=1)
        if not invoices:
            return {"error": "You don't have any invoices yet."}
        kwargs["pk"] = invoices[0].pk

    elif route.scope == SCOPE_LOCATION:
        from .models import PickupLocation

        auction, error = palette_actions.resolve_auction(user, "")
        if error:
            return {"error": error}
        if not palette_actions._is_auction_admin(user, auction):
            return _denied(f"Only admins of {auction.title} can open that page.")
        locations = PickupLocation.objects.filter(auction=auction)
        location = locations.filter(name__icontains=hint).first() if hint else locations.first()
        if not location:
            return {"error": f"I couldn't find a pickup location for {auction.title}."}
        kwargs["pk"] = location.pk

    elif route.scope == SCOPE_MEMBER:
        from .models import ClubMember

        club = _club_from_hint(user, str(params.get("club") or ""))
        if not club:
            return {"error": "I couldn't work out which club you mean."}
        from . import command_palette

        if not command_palette._can_manage_members(user, club):
            return _denied(f"Only admins of {club.name} can open that page.")
        member = ClubMember.objects.filter(club=club, is_deleted=False, name__icontains=hint).first() if hint else None
        if not member:
            return {"error": f"I couldn't find a member called “{hint}” in {club.name}."}
        kwargs["slug"] = club.slug
        kwargs["pk"] = member.pk

    elif route.scope == SCOPE_USER:
        from django.contrib.auth.models import User

        target = User.objects.filter(username__iexact=hint).first() if hint else user
        if not target:
            return {"error": f"I couldn't find a user called “{hint}”."}
        kwargs["slug"] = target.username

    elif route.scope == SCOPE_BLOG:
        from .models import BlogPost

        post = BlogPost.objects.filter(title__icontains=hint).first() if hint else None
        if not post:
            return {"error": "I couldn't find a blog post like that."}
        kwargs["slug"] = post.slug

    if route.admin == ADMIN_SUPERUSER and not user.is_superuser:
        return _denied("That page is only for site administrators.")

    try:
        url = reverse(route.key, kwargs=kwargs) if kwargs else reverse(route.key)
    except NoReverseMatch:
        logger.exception("Palette route %s could not be reversed with %s", route.key, kwargs)
        return {"error": "I know that page but couldn't work out the link to it."}
    # ``route`` travels back so the caller can record *which* destination was chosen, not just that
    # a navigation happened. That is the ground truth the shortcut miner runs on: a query that
    # resolves to the same route every time is one the model never needs to be asked about again.
    return {"ok": True, "url": url, "summary": f"Opening {route.label}.", "title": route.label, "route": route.key}


# --- where the user currently is ---------------------------------------------


def page_context_from_path(user, path: str) -> dict[str, Any]:
    """Work out what the user is looking at from the URL they're on.

    Resolved through Django's own URLconf rather than trusted from the client: the browser sends a
    path, and every object it names is looked up again here, scoped to what this user may see. The
    worst a forged path can do is name an auction they're already part of.

    This is what makes "add a lot" mean *this* auction rather than whichever one they last touched.
    """
    data: dict[str, Any] = {}
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 500:
        return data
    path = path.split("?")[0].split("#")[0]
    try:
        match = resolve(path)
    except (Resolver404, Exception):  # noqa: BLE001 - a bad path is never worth an error page
        return data
    data["page"] = match.url_name or ""
    route = get_route(match.url_name or "")
    if route:
        data["page_label"] = route.label
    slug = match.kwargs.get("slug")

    from . import command_palette
    from .models import Club, Lot

    if slug:
        auction = command_palette._joined_auctions(user).filter(slug=slug).first()
        if auction:
            data["auction"] = auction.slug
            data["auction_title"] = auction.title
        else:
            club = Club.objects.filter(slug=slug).first()
            if club:
                data["club"] = club.slug
                data["club_name"] = club.name

    pk = match.kwargs.get("pk")
    if pk and (match.url_name or "").startswith("lot"):
        lot = Lot.objects.filter(pk=pk, is_deleted=False).first()
        if lot:
            data["lot_id"] = lot.pk
            data["lot_name"] = lot.lot_name
            if lot.auction and "auction" not in data:
                data["auction"] = lot.auction.slug
                data["auction_title"] = lot.auction.title
    custom_lot_number = match.kwargs.get("custom_lot_number")
    if custom_lot_number and data.get("auction"):
        lot = Lot.objects.filter(
            auction__slug=data["auction"], custom_lot_number=custom_lot_number, is_deleted=False
        ).first()
        if lot:
            data["lot_id"] = lot.pk
            data["lot_name"] = lot.lot_name
    return data
