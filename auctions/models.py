"""The database: 80 models, mostly in one file because 29 of them form a single dependency cycle
(Auction, Lot, Club, ClubMember, AuctionTOS, Invoice, UserData, Species, and 21 more) referenced as
class objects, so splitting means converting every reference and risking a broken FK.

Roughly in order: site furniture; Club and what hangs off it; API keys; the auction cycle (Auction,
AuctionTOS, PickupLocation, Lot, Bid, Invoice); Species; then UserData, Watch, PageView, ads,
speakers, volunteers, printing, mobile, voice.

A `.delay()` from a signal must go inside transaction.on_commit.
"""

import datetime
import logging
import re
import secrets
import uuid as uuid_module
from datetime import time
from decimal import ROUND_HALF_UP, Decimal
from random import randint
from urllib.parse import quote_plus

import channels.layers
import pytz
from asgiref.sync import async_to_sync
from autoslug import AutoSlugField
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models import (
    BooleanField,
    Case,
    Count,
    DecimalField,
    Exists,
    ExpressionWrapper,
    F,
    IntegerField,
    Max,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.expressions import RawSQL
from django.db.models.functions import Cast, Coalesce
from django.db.models.query import QuerySet
from django.urls import NoReverseMatch, reverse
from django.utils import html, timezone
from django.utils.functional import cached_property
from django.utils.safestring import mark_safe
from easy_thumbnails.fields import ThumbnailerImageField
from easy_thumbnails.files import get_thumbnailer
from encrypted_model_fields.fields import EncryptedCharField
from location_field.models.plain import PlainLocationField
from markdownfield.models import MarkdownField, RenderedMarkdownField
from markdownfield.validators import VALIDATOR_STANDARD
from post_office import mail
from pytz import timezone as pytz_timezone
from webpush.models import PushInformation

from . import cloudflare_images, history, printer_programs, voice
from .club_health import ClubHealth, ClubLadderSnapshot  # noqa: F401
from .club_matching import derived_abbreviation
from .email_routing import (
    admin_routing_email,
    build_routed_sender_address,
    email_routing_domain,
    email_routing_enabled,
    sender_with_display_name,
)
from .friction_models import FormFailure  # noqa: F401
from .helper_functions import bin_data, get_currency_symbol
from .html_sanitize import sanitize_summernote_html
from .model_caching import CachedPropertiesMixin, InvalidatesRelatedCache

# Moderation models live in their own module (string FKs, so no cycle); imported for Django and callers.
from .moderation_models import (  # noqa: F401
    ContentReport,
    CopyrightNotice,
    CopyrightStrike,
)

logger = logging.getLogger(__name__)

CUSTOM_DROPDOWN_MAX_LENGTH = 15

# The privacy policy is a BlogPost so it can be edited without a deploy; /privacy/, /blog/privacy/
# and the app's sign-up link all use this slug.
PRIVACY_POLICY_SLUG = "privacy"


def median_value(queryset, term):
    """Median of `term` across `queryset`. Raises IndexError on an empty queryset (callers rely on this)."""
    count = queryset.count()
    if not count:
        msg = "median_value() called on an empty queryset"
        raise IndexError(msg)
    values = queryset.values_list(term, flat=True).order_by(term)
    middle = count // 2
    if count % 2:
        return values[middle]
    lower, upper = values[middle - 1 : middle + 1]
    return (lower + upper) / 2


def add_price_info(qs):
    """Add fields `pre_register_discount`, `your_cut` and `club_cut` to a given Lot queryset."""
    if not (isinstance(qs, QuerySet) and qs.model == Lot):
        msg = "must be passed a queryset of the Lot model"
        raise TypeError(msg)
    money_field = DecimalField(max_digits=10, decimal_places=2)
    return qs.annotate(
        pre_register_discount=Case(
            When(auctiontos_seller__isnull=True, then=Value(Decimal(0))),
            When(
                added_by=F("user"),
                then=Cast(F("auctiontos_seller__auction__pre_register_lot_discount_percent"), money_field),
            ),
            default=Value(Decimal(0)),
            output_field=money_field,
        ),
        your_cut=ExpressionWrapper(
            Case(
                # Banned lots are never charged -- see the `banned` field's help text.
                When(banned=True, then=Value(Decimal(0))),
                When(
                    Q(auctiontos_seller__isnull=True, winning_price__isnull=False),
                    then=F("winning_price"),
                ),
                When(
                    Q(auctiontos_seller__isnull=True, winning_price__isnull=True),
                    then=Value(Decimal(0)),
                ),
                When(donation=True, then=Value(Decimal(0))),
                When(
                    # buy_now_used lots stay active until endauctions; credit the seller now.
                    Q(winning_price__isnull=False) & (Q(active=False) | Q(buy_now_used=True)),
                    then=(
                        (
                            F("winning_price")
                            * Case(
                                When(
                                    Q(auctiontos_seller__is_club_member=True)
                                    & ~Q(auctiontos_seller__auction__alternate_split_mode="off"),
                                    then=(
                                        (
                                            100
                                            - Cast(
                                                F(
                                                    "auctiontos_seller__auction__winning_bid_percent_to_club_for_club_members"
                                                ),
                                                money_field,
                                            )
                                            + F("pre_register_discount")
                                        )
                                        / 100
                                    ),
                                ),
                                default=(
                                    (
                                        100
                                        - Cast(
                                            F("auctiontos_seller__auction__winning_bid_percent_to_club"),
                                            money_field,
                                        )
                                        + F("pre_register_discount")
                                    )
                                    / 100
                                ),
                                output_field=money_field,
                            )
                        )
                        - Cast(
                            Case(
                                When(
                                    Q(auctiontos_seller__is_club_member=True)
                                    & ~Q(auctiontos_seller__auction__alternate_split_mode="off"),
                                    then=F("auction__lot_entry_fee_for_club_members"),
                                ),
                                default=F("auction__lot_entry_fee"),
                                output_field=money_field,
                            ),
                            money_field,
                        )
                    )
                    * (100 - Cast(F("partial_refund_percent"), money_field))
                    / 100,
                ),
                When(
                    Q(winning_price__isnull=True, active=False),
                    then=Case(
                        When(donation=True, then=Value(Decimal(0))),
                        default=Value(Decimal(0)) - Cast(F("auctiontos_seller__auction__unsold_lot_fee"), money_field),
                    ),
                ),
                default=Value(Decimal(0)),
                output_field=money_field,
            ),
            output_field=money_field,
        ),
        club_cut=ExpressionWrapper(
            Case(
                # Banned lots earn the club nothing either.
                When(banned=True, then=Value(Decimal(0))),
                When(Q(active=False, winning_price__isnull=True), then=Value(Decimal(0))),
                When(winning_price__isnull=True, then=Value(Decimal(0))),
                default=(F("winning_price") * (100 - Cast(F("partial_refund_percent"), money_field)) / 100)
                - F("your_cut"),
            ),
            output_field=money_field,
        ),
    )


def find_image(name, user, auction):
    """Find an image from the most recent lot with a given name (one query, not two)."""
    qs = LotImage.objects.filter(
        (Q(lot_number__user__userdata__share_lot_images=True) | Q(lot_number__user__isnull=True)),
        lot_number__lot_name=name,
        lot_number__is_deleted=False,
        lot_number__banned=False,
        is_primary=True,
        lot_number__auction__created_by__pk__in=auction.auction_admins_pks,
    ).order_by("-lot_number__date_posted")
    if user:
        # The user's own image first, then newest, in one ORDER BY.
        qs = qs.annotate(
            not_from_this_user=Case(
                When(lot_number__user=user, then=Value(0)), default=Value(1), output_field=IntegerField()
            )
        ).order_by("not_from_this_user", "-lot_number__date_posted")
    return qs.first()


def distance_to(
    latitude,
    longitude,
    unit="miles",
    lat_field_name="latitude",
    lng_field_name="longitude",
    approximate_distance_to=10,
):
    """Raw-SQL distance annotation (GeoDjango/MySQL Point support is unreliable).

    Model needs `latitude`/`longitude` fields; use as
    `qs.annotate(distance=distance_to(lat, lng)).order_by('distance')`.
    """
    if unit == "miles":
        correction = 0.6213712  # close enough
    else:
        correction = 1  # km
    try:
        latitude = float(latitude)
        longitude = float(longitude)
        approximate_distance_to = float(approximate_distance_to)
    except (TypeError, ValueError):
        msg = "invalid character passed to distance_to, possible sql injection risk"
        raise TypeError(msg) from None
    if approximate_distance_to <= 0:
        msg = "approximate_distance_to must be > 0"
        raise TypeError(msg)
    # Allow both simple identifiers and backtick-qualified table.column names.
    sql_identifier = r"(?:[A-Za-z_][A-Za-z0-9_]*|`[A-Za-z_][A-Za-z0-9_]*`)"
    field_name_pattern = re.compile(rf"^{sql_identifier}(?:\.{sql_identifier})*$")
    for field_name in [lat_field_name, lng_field_name]:
        if not field_name_pattern.fullmatch(str(field_name)):
            msg = "invalid character passed to distance_to, possible sql injection risk"
            raise TypeError(msg)
    # CEILING so distances can't be used to triangulate a location.
    gcd_formula = f"CEILING( 6371 * acos(least(greatest( \
        cos(radians({latitude})) * cos(radians({lat_field_name})) \
        * cos(radians({lng_field_name}) - radians({longitude})) + \
        sin(radians({latitude})) * sin(radians({lat_field_name})) \
        , -1), 1)) * {correction} / {approximate_distance_to}) * {approximate_distance_to}"
    distance_raw_sql = RawSQL(gcd_formula, ())
    return distance_raw_sql


def guess_category(text):
    """Guess a lot's category from the categories used by similarly-named lots."""
    keywords = []
    words = re.findall("[A-Z|a-z]{3,}", text.lower())
    for word in words:
        if word not in settings.IGNORE_WORDS:
            keywords.append(word)

    if not keywords:
        return None
    lot_qs = (
        Lot.objects.exclude(is_deleted=True)
        .filter(
            category_automatically_added=False,
            species_category__isnull=False,
            is_deleted=False,
        )
        .exclude(species_category__name="Uncategorized")
        .exclude(auction__promote_this_auction=False)
    )
    q_objects = Q()
    for keyword in keywords:
        q_objects |= Q(lot_name__iregex=rf"\b{re.escape(keyword)}\b")

    lot_qs = lot_qs.filter(q_objects)

    categories = {}
    for lot in lot_qs:
        matches = 0
        for keyword in keywords:
            if keyword in lot.lot_name.lower():
                matches += 1
        category_total = categories.get(lot.species_category.pk, 0)
        categories[lot.species_category.pk] = category_total + matches
    sorted_categories = sorted(categories.items(), key=lambda x: x[1], reverse=True)
    for key, value in sorted_categories:
        logger.debug("%s, %s", Category.objects.filter(pk=key).first(), value)
        return Category.objects.filter(pk=key).first()
    return None


def normalize_email(value):
    """Strip and lowercase an email; empty input returns "" (not None) to match field convention."""
    return (value or "").strip().lower()


def _default_membership_number():
    """Random 10-digit membership number; rare collisions are retried by _pick_unique_membership_number."""
    return randint(1_000_000_000, 9_999_999_999)


def _pick_unique_membership_number():
    """Random membership number not already in use; retries up to 20 times then raises RuntimeError."""
    from django.apps import apps  # ClubMember is defined later in this module

    ClubMemberCls = apps.get_model("auctions", "ClubMember")
    for _ in range(20):
        candidate = _default_membership_number()
        if not ClubMemberCls.objects.filter(membership_number=candidate).exists():
            return candidate
    msg = "Could not generate a unique membership number after 20 attempts."
    raise RuntimeError(msg)


class CloudflareImageMixin(models.Model):
    """Mixin for models whose image can be mirrored to Cloudflare Images. ``cloudflare_image_id`` is set by
    migrate_to_cloudflare_images; replacing the file clears it. Set IMAGE_FIELD_NAME if not ``image``.
    """

    IMAGE_FIELD_NAME = "image"
    cloudflare_image_id = models.CharField(max_length=100, blank=True, default="", editable=False, db_index=True)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if self.pk and self.cloudflare_image_id and (update_fields is None or self.IMAGE_FIELD_NAME in update_fields):
            old = type(self).objects.filter(pk=self.pk).values(self.IMAGE_FIELD_NAME, "cloudflare_image_id").first()
            if (
                old
                and old[self.IMAGE_FIELD_NAME] != getattr(self, self.IMAGE_FIELD_NAME).name
                and old["cloudflare_image_id"] == self.cloudflare_image_id
            ):
                # New file, old id: clear it.
                self.cloudflare_image_id = ""
                if update_fields is not None:
                    kwargs["update_fields"] = set(update_fields) | {"cloudflare_image_id"}
        super().save(*args, **kwargs)


class BlogPost(models.Model):
    """A simple markdown blog."""

    title = models.CharField(max_length=255)
    slug = AutoSlugField(populate_from="title", unique=True)
    body = MarkdownField(
        rendered_field="body_rendered",
        validator=VALIDATOR_STANDARD,
        blank=True,
        null=True,
    )
    body_rendered = RenderedMarkdownField(blank=True, null=True)
    date_posted = models.DateTimeField(auto_now_add=True)
    extra_js = models.TextField(max_length=16000, null=True, blank=True)

    def __str__(self):
        return self.title


class Location(models.Model):
    """A region -- USA, Canada, South America, etc."""

    name = models.CharField(max_length=255)

    def __str__(self):
        return str(self.name)


class GeneralInterest(models.Model):
    """Clubs and products belong to a general interest"""

    name = models.CharField(max_length=255)

    def __str__(self):
        return str(self.name)


class ClubQuerySet(models.QuerySet):
    """The one place that knows which clubs this site is willing to name in public."""

    def listed(self):
        """Listed clubs only (active and outreach_stage LISTED): what the map, search and dropdowns show."""
        return self.filter(active=True, outreach_stage=Club.LISTED)


class Club(CloudflareImageMixin, models.Model):
    """Users can self-select which club they belong to"""

    IMAGE_FIELD_NAME = "icon"
    name = models.CharField(max_length=255, db_index=True)
    abbreviation = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    homepage = models.CharField(max_length=255, blank=True, null=True)
    facebook_page = models.CharField(max_length=255, blank=True, null=True)
    discord_invite_link = models.CharField(max_length=255, blank=True, null=True)
    contact_email = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name="Membership email address",
        help_text="Replies to membership inquiries will be sent to this email",
    )
    EMAIL = "email"
    WEBFORM = "webform"
    FACEBOOK = "facebook"
    CONTACT_METHOD_CHOICES = (
        ("", "Not known"),
        (EMAIL, "Email"),
        (WEBFORM, "Form on their website"),
        (FACEBOOK, "Facebook only"),
    )
    contact_method = models.CharField(max_length=20, choices=CONTACT_METHOD_CHOICES, blank=True, default="")
    contact_method.help_text = (
        "Which door to knock on. Outreach is a person working a queue one club at a time, and a "
        "club reachable only through Facebook takes a different afternoon from one with an address."
    )
    date_contacted = models.DateTimeField(blank=True, null=True)
    date_contacted_for_in_person_auctions = models.DateTimeField(blank=True, null=True)
    PROSPECT = "prospect"
    CONTACTED = "contacted"
    LISTED = "listed"
    OUTREACH_STAGE_CHOICES = (
        (PROSPECT, "Found, not approved"),
        (CONTACTED, "Contacted, no reply yet"),
        # the only value that publishes a club
        (LISTED, "Approved and listed"),
    )
    outreach_stage = models.CharField(max_length=20, choices=OUTREACH_STAGE_CHOICES, default=PROSPECT, db_index=True)
    outreach_stage.help_text = (
        "The half of a club's progress that no query can answer. Only 'Approved and listed' puts a "
        "club on the map, in club search and in the dropdowns -- ClubHealth derives everything after "
        "that from rows and never writes here."
    )
    STALL_REASON_CHOICES = (
        ("", "Not known"),
        ("no_reply", "Never replied"),
        ("no_auction", "No auction coming up"),
        ("uses_other", "Uses something else"),
        ("paper", "Paper works fine"),
        ("cost", "Cost"),
        ("not_interested", "Not interested"),
        ("folded", "Club has folded"),
    )
    stall_reason = models.CharField(max_length=20, choices=STALL_REASON_CHOICES, blank=True, default="")
    stall_reason.help_text = (
        "Why this club stopped where it did, in a word that can be counted. Set it when somebody "
        "answers; notes are free text and cannot say which objection is worth fixing."
    )
    notes = models.CharField(max_length=300, blank=True, null=True)
    notes.help_text = "Only visible in the admin site, never made public"
    interests = models.ManyToManyField(GeneralInterest, blank=True)
    active = models.BooleanField(default=True)
    latitude = models.FloatField(blank=True, null=True)
    longitude = models.FloatField(blank=True, null=True)
    location = models.CharField(max_length=500, blank=True, null=True)
    location.help_text = "Search Google maps with this address"
    location_coordinates = PlainLocationField(based_fields=["location"], blank=True, null=True, verbose_name="Map")
    MEMBERSHIP_SYSTEM_CHOICES = (
        ("none", "No membership fees"),
        ("january_first", "January 1st renewal"),
        ("rolling", "Rolling annual membership"),
    )
    membership_system = models.CharField(max_length=20, choices=MEMBERSHIP_SYSTEM_CHOICES, default="none")
    membership_annual_fee = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    show_member_barcode = models.BooleanField(
        default=True,
        verbose_name="Show member barcodes",
        help_text=(
            "When checked, members receive a 10-digit barcode that can be scanned "
            "at auctions, added to Google/Apple Wallet, and included in emails."
        ),
    )
    use_site_paypal_account = models.BooleanField(
        default=False,
        verbose_name="Use site PayPal account",
        help_text=(
            "When checked, PayPal payments for this club use the site's own merchant account "
            "(PAYPAL_CLIENT_ID / PAYPAL_SECRET from settings) instead of any linked PayPalSeller. "
            "Only useful for site admins; Square has no platform-account equivalent."
        ),
    )
    allow_non_oauth_paypal = models.BooleanField(
        default=False,
        verbose_name="Allow non-OAuth PayPal (admin only)",
        help_text=(
            "When checked, this club skips PayPal OAuth and instead enters its own PayPal REST API "
            "client ID and secret on the membership settings page. Those credentials are used for the "
            "club's auctions and membership payments exactly the way the site's PAYPAL_CLIENT_ID / "
            "PAYPAL_SECRET are: payments go straight to that PayPal account with no platform fee. "
            "Set this here in the Django admin only."
        ),
    )
    paypal_client_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="PayPal client ID",
        help_text="REST API client ID from this club's own PayPal app. Only used when non-OAuth PayPal is allowed.",
    )
    paypal_secret = EncryptedCharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name="PayPal secret",
        help_text=(
            "REST API secret from this club's own PayPal app (stored encrypted). "
            "Only used when non-OAuth PayPal is allowed."
        ),
    )
    paypal_webhook_id = models.CharField(
        max_length=100,
        blank=True,
        default="",
        verbose_name="PayPal subscription webhook ID",
        help_text=(
            "Webhook ID from this club's PayPal dashboard, used to verify incoming membership "
            "subscription webhooks. Create a webhook pointing at /clubs/paypal/webhook subscribed to "
            "BILLING.SUBSCRIPTION events, then paste its ID here."
        ),
    )
    auction_email_member = models.ForeignKey(
        "ClubMember",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="club_auction_email_destinations",
        help_text="Incoming mail for club-slug-auctions@your-domain is forwarded to this member.",
    )
    contact_email_member = models.ForeignKey(
        "ClubMember",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="club_contact_email_destinations",
        help_text="Incoming mail for club-slug-contact@your-domain is forwarded to this member.",
    )
    send_membership_expiration_reminders = models.BooleanField(
        default=False,
        help_text="Reminders include a link to pay directly on this site, users don't need to have an account to renew their membership.  Reminders are only sent if the user has paid for their membership at least once.  This option is probably not a great idea as users will get an email from this site asking them to pay for their membership, which may cause confusion.",
    )
    send_membership_expiration_reminders_30_days = models.BooleanField(default=False)
    send_membership_renewal_confirmation = models.BooleanField(default=False)
    send_welcome_email_to_new_members = models.BooleanField(default=False)
    membership_email_template = models.TextField(blank=True, default="")
    include_next_auction_in_emails = models.BooleanField(
        default=True,
        verbose_name="Include the next event in membership emails",
        help_text="Default for email types that don't have their own setting.",
    )
    welcome_opening = models.TextField(
        blank=True,
        default="Thanks for joining!\n\nYou can view your membership below:",
        verbose_name="Welcome email opening text",
    )
    welcome_closing = models.TextField(
        blank=True, default="See you there!\n\nBest wishes,", verbose_name="Welcome email closing text"
    )
    # Covers any next calendar event, not just auctions; see tasks.next_event_fragment.
    welcome_include_auction = models.BooleanField(
        default=True,
        verbose_name="Also include information about the next event",
        help_text="Your next auction, meeting, or anything else on the club calendar.",
    )
    renewal_opening = models.TextField(
        blank=True,
        default="Thanks for being a club member, and we'll see you at our next meeting.",
        verbose_name="Renewal email opening text",
    )
    renewal_closing = models.TextField(
        blank=True, default="See you there!\n\nBest wishes,", verbose_name="Renewal email closing text"
    )
    renewal_include_auction = models.BooleanField(
        default=True,
        verbose_name="Also include information about the next event",
        help_text="Your next auction, meeting, or anything else on the club calendar.",
    )
    expiring_soon_opening = models.TextField(
        blank=True,
        default="It's time to renew your membership!  You can pay at this link:",
        verbose_name="Expiring soon email opening text",
    )
    expiring_soon_closing = models.TextField(
        blank=True, default="See you there!\n\nBest wishes,", verbose_name="Expiring soon email closing text"
    )
    expiring_soon_include_auction = models.BooleanField(
        default=True,
        verbose_name="Also include information about the next event",
        help_text="Your next auction, meeting, or anything else on the club calendar.",
    )
    discord_server_id = models.CharField(max_length=100, blank=True, null=True)
    auction_channel_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Discord channel ID for auction announcements. Set via /auctions_here.",
    )
    announcement_channel_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Discord channel ID for club announcements. Set via /announcements_here.",
    )
    create_events_for_auctions = models.BooleanField(
        default=False,
        help_text="Automatically create a Discord scheduled event for each promoted auction.",
    )
    uuid = models.UUIDField(default=uuid_module.uuid4, unique=True, editable=False, db_index=True)
    is_nec_club = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name="NEC member club",
        help_text=(
            "This club belongs to the Northeast Council of Aquarium Societies.  Anyone with a "
            "permission in an NEC club can use the speaker directory.  Set this here in the "
            "Django admin only — it is deliberately absent from the club settings page so clubs "
            "can't opt themselves in."
        ),
    )
    slug = AutoSlugField(populate_from="name", unique=True, always_update=True)
    icon = ThumbnailerImageField(
        upload_to="club_icons/",
        blank=True,
        null=True,
        help_text="Square logo shown beside the club name and on Google/Apple Wallet membership cards.",
    )
    google_wallet_class_created = models.BooleanField(
        default=False,
        help_text="Set to True once the Wallet GenericClass has been confirmed on Google's side.",
    )
    allow_joining = models.BooleanField(default=False)
    current_auction = models.ForeignKey(
        "Auction",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="The auction whose admin links are surfaced in the club sidebar.",
    )
    description = models.TextField(verbose_name="About this club", default="", blank=True)
    enable_breeder_award_program = models.BooleanField(
        default=False,
        help_text="Track when users breed fish and show a leaderboard of top breeders.",
    )
    bap_ytd_reset_year = models.PositiveIntegerField(
        null=True,
        blank=True,
        editable=False,
        help_text=(
            "The last year this club's year-to-date award counters were zeroed.  Written by "
            "tasks.reset_yearly_bap_counters, which is what makes the reset a fact about the club "
            "rather than something that only happens if a nightly task lands on January 1."
        ),
    )
    enable_membership = models.BooleanField(
        default=False,
        verbose_name="Enable membership",
        help_text="Enable membership tracking, dues collection, and expiration reminders.",
    )
    days_between_same_name_lots = models.IntegerField(
        default=0,
        help_text="Minimum days between awarding BAP points for lots with the same name. Leave at 0 to allow points every time.",
    )
    days_between_same_species_lots = models.IntegerField(
        default=0,
        verbose_name="Days between same species lots",
        help_text=(
            "Minimum days between awarding BAP points for lots with the same scientific name. Leave at 0 to "
            "allow points every time. Stricter than the rule above, because it sees through what the lot was "
            "called: “Yellow labs” and “Labidochromis caeruleus” are the same fish. A named strain counts as "
            "its own species, so blue and red cherry shrimp both earn points."
        ),
    )
    points_per_lot = models.IntegerField(
        null=True,
        blank=True,
        default=None,
        help_text=(
            "Default BAP points for every lot. Override these with per-category points below. "
            "Leave blank to use the per-category points instead; set it to 0 to award nothing by default."
        ),
    )
    separate_hap = models.BooleanField(
        default=False,
        help_text="Track HAP (Horticultural Award Program) points separately from BAP.",
        verbose_name="Separate Horticultural Award Program (HAP)",
    )
    separate_cap = models.BooleanField(
        default=False,
        help_text="Track CAP (Culture Award Program) points separately from BAP.",
        verbose_name="Separate Live Food Culture Award Program (CAP)",
    )
    auto_add_points = models.BooleanField(
        default=True,
        help_text="Automatically award BAP points when a lot sells. Uncheck to require admin approval before awarding points.",
    )
    only_active_members_can_participate = models.BooleanField(
        default=False,
        help_text="Only club members with an active (paid) membership can earn BAP points.",
    )
    min_quantity = models.IntegerField(
        default=5,
        help_text="Minimum quantity in a lot to be eligible for BAP points.",
    )
    points_for_custom_checkbox = models.IntegerField(
        default=0,
        help_text="Bonus BAP points awarded when the custom checkbox is checked on a lot. Leave at 0 to disable.",
    )
    only_donation_lots = models.BooleanField(
        default=False,
        help_text="Require all BAP lots to be a donation.",
    )
    only_sold_lots = models.BooleanField(
        default=False,
        help_text=(
            "Require lots to be sold for points to be awarded. "
            "Uncheck to give points for submitted and unsold lots. "
            "If automatically award points is on, they will only be automatically awarded to sold lots."
        ),
        verbose_name="Only sold lots",
    )
    no_min_bids = models.BooleanField(
        default=False,
        help_text=(
            "Lots with a minimum bid set are disqualified. "
            "You can still set an auction-wide minimum bid, but any lots that set their own will not be awarded points."
        ),
        verbose_name="No minimum bids",
    )
    last_bap_recalculation = models.DateTimeField(null=True, blank=True)
    next_bap_recalculation = models.DateTimeField(null=True, blank=True)

    # Mailchimp: one-way OAuth sync to the club's account (auctions/mailchimp.py).
    mailchimp_access_token = EncryptedCharField(
        max_length=500, blank=True, null=True, help_text="OAuth access token (does not expire)."
    )
    mailchimp_server_prefix = models.CharField(
        max_length=10, blank=True, help_text="Mailchimp data-center prefix (e.g. us19) from the OAuth metadata."
    )
    mailchimp_audience_id = models.CharField(
        max_length=50,
        blank=True,
        help_text="The Mailchimp audience (list) id members are synced into. Safe across renames in Mailchimp.",
    )
    mailchimp_audience_name = models.CharField(
        max_length=255, blank=True, help_text="Display name of the connected audience at connection time."
    )
    mailchimp_connected_on = models.DateTimeField(null=True, blank=True)
    mailchimp_connected_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="The club admin who connected Mailchimp.",
    )
    mailchimp_webhook_secret = models.CharField(
        max_length=64, blank=True, help_text="Secret embedded in the Mailchimp webhook URL to verify callbacks."
    )
    mailchimp_last_sync = models.DateTimeField(null=True, blank=True)
    mailchimp_last_error = models.TextField(blank=True)

    # Brevo: one-way sync with a pasted API key (Brevo OAuth isn't available). See auctions/brevo.py.
    brevo_api_key = EncryptedCharField(
        max_length=500, blank=True, null=True, help_text="The club's Brevo API v3 key (stored encrypted)."
    )
    brevo_list_id = models.CharField(
        max_length=50,
        blank=True,
        help_text="The Brevo contact list id members are synced into. Safe across renames in Brevo.",
    )
    brevo_list_name = models.CharField(
        max_length=255, blank=True, help_text="Display name of the connected list at connection time."
    )
    brevo_folder_id = models.CharField(
        max_length=50,
        blank=True,
        help_text="Brevo folder that holds the club's list (Brevo requires lists to live in a folder).",
    )
    brevo_sender_id = models.CharField(
        max_length=50,
        blank=True,
        help_text=(
            "Which of the account's verified senders announcement emails go out as. Blank means "
            "the first active one, which is the only one most accounts have."
        ),
    )
    brevo_connected_on = models.DateTimeField(null=True, blank=True)
    brevo_connected_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="The club admin who connected Brevo.",
    )
    brevo_webhook_secret = models.CharField(
        max_length=64, blank=True, help_text="Secret embedded in the Brevo webhook URL to verify callbacks."
    )
    brevo_webhook_id = models.CharField(
        max_length=50, blank=True, help_text="Brevo webhook id, stored so we don't register duplicates."
    )
    brevo_last_sync = models.DateTimeField(null=True, blank=True)
    brevo_last_error = models.TextField(blank=True)

    # Two-way sync with a secondary calendar in the club's Google account (auctions/google_calendar.py).
    google_calendar_refresh_token = EncryptedCharField(
        max_length=500,
        blank=True,
        null=True,
        help_text="OAuth refresh token for the club's Google account (stored encrypted).",
    )
    google_calendar_access_token = EncryptedCharField(
        max_length=1000,
        blank=True,
        null=True,
        help_text="Short-lived OAuth access token, cached until google_calendar_token_expires.",
    )
    google_calendar_token_expires = models.DateTimeField(null=True, blank=True)
    google_calendar_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="Calendar id of the secondary calendar we created for this club.",
    )
    google_calendar_account_email = models.CharField(
        max_length=255, blank=True, help_text="The Google account that authorized us, shown on the settings page."
    )
    google_calendar_connected_on = models.DateTimeField(null=True, blank=True)
    google_calendar_connected_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="The club admin who connected Google Calendar.",
    )
    google_calendar_sync_token = models.CharField(
        max_length=500,
        blank=True,
        help_text="Google's incremental sync token, so each pull only fetches what changed.",
    )
    google_calendar_is_public = models.BooleanField(
        default=False,
        verbose_name="This calendar is shared publicly",
        help_text=(
            "Whether the calendar is shared publicly in Google Calendar. Derived, not typed: we "
            "can't change sharing ourselves — that needs a scope granting access to all of their "
            "calendars — but a shared calendar has a public iCal feed, so every sync asks for it "
            "the way a member would. It only controls whether we advertise the Google links."
        ),
    )
    google_calendar_public_checked = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When we last asked Google whether this calendar is shared. See google_calendar.refresh_public_flag.",
    )
    google_calendar_last_sync = models.DateTimeField(null=True, blank=True)
    google_calendar_last_error = models.TextField(blank=True)
    add_auctions_to_calendar = models.BooleanField(
        default=True,
        verbose_name="Add auctions to the calendar",
        help_text="Automatically add each promoted auction to this club's events and Google Calendar.",
    )
    create_discord_events_for_club_events = models.BooleanField(
        default=True,
        verbose_name="Create Discord events for calendar events",
        help_text=(
            "Create a Discord scheduled event for each club event. Auctions are handled by the "
            "Discord auction event setting instead, so they are never doubled up."
        ),
    )

    # Events embed renders on the club's own site (not our club page).
    events_website_views = models.PositiveIntegerField(
        default=0,
        help_text=(
            "How many times this club's events embed has been rendered on a website. An "
            "impression, not a read, and evidence the snippet is installed somewhere."
        ),
    )
    events_website_last_view = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When the events embed was last asked for. A club that took the snippet down stops "
            "counting, so this is what separates 'embeds our events' from 'tried it once in 2024'."
        ),
    )

    # Donation vendor tracking; see auctions/donations.py.
    enable_donation_tracking = models.BooleanField(
        default=False,
        verbose_name="Enable donation tracking",
        help_text="Track which vendors you've asked for donations, and what they said.",
    )
    DONATION_EMAIL_MODE_ROUTED = "routed"
    DONATION_EMAIL_MODE_COPY = "copy"
    DONATION_EMAIL_MODE_CHOICES = (
        (DONATION_EMAIL_MODE_ROUTED, "Send mail from this site"),
        (DONATION_EMAIL_MODE_COPY, "Copy/paste to my email"),
    )
    donation_email_mode = models.CharField(
        max_length=20,
        choices=DONATION_EMAIL_MODE_CHOICES,
        default=DONATION_EMAIL_MODE_ROUTED,
        verbose_name="How to send donation emails",
    )
    donation_email_member = models.ForeignKey(
        "ClubMember",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="club_donation_email_destinations",
        verbose_name="Donation contact",
        help_text="Incoming mail for club-slug-donations-*@your-domain is forwarded to this member.",
    )
    donation_context = models.TextField(
        blank=True,
        default="",
        verbose_name="Club information for donation emails",
        help_text=(
            "Passed to the language model with every donation email it writes, so it doesn't have "
            "to be retyped for each vendor."
        ),
    )
    donation_mailing_address = models.TextField(
        blank=True,
        default="",
        verbose_name="Donation mailing address",
        help_text="Where vendors should send physical donations. Included in donation emails.",
    )
    DONATION_FOLLOWUP_CHOICES = (
        (1, "1 day"),
        (3, "3 days"),
        (7, "1 week"),
        (14, "2 weeks"),
        (30, "1 month"),
    )
    donation_followup_days = models.PositiveSmallIntegerField(
        default=7,
        choices=DONATION_FOLLOWUP_CHOICES,
        verbose_name="Follow up after",
        help_text="How long to wait for a reply before a vendor shows up as due for a follow-up.",
    )

    objects = ClubQuerySet.as_manager()

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return str(self.name)

    @property
    def is_listed(self):
        """Whether this club is published (the map gate)."""
        return self.active and self.outreach_stage == self.LISTED

    def find_member(self, name="", email="", exclude_pk=None):
        """Duplicate lookup for a club member: normalized email first, then exact or nickname name. Non-deleted
        members of this club only. Returns the oldest match or None.
        """
        from .filters import rhyming_name_q

        email = normalize_email(email)
        qs = ClubMember.objects.filter(club=self, is_deleted=False)
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        if not name and not email:
            return None
        if email:
            email_search = qs.filter(email__iexact=email).order_by("createdon", "pk").first()
            if email_search:
                return email_search
        if name:
            name_search = (
                qs.filter(Q(name__iexact=name.strip()) | rhyming_name_q(name)).order_by("createdon", "pk").first()
            )
            if name_search:
                return name_search
        return None

    @property
    def mailchimp_connected(self):
        """True when Mailchimp is connected with an audience selected."""
        return bool(self.mailchimp_access_token and self.mailchimp_audience_id and self.mailchimp_server_prefix)

    @property
    def brevo_connected(self):
        """True when Brevo is connected with a list selected."""
        return bool(self.brevo_api_key and self.brevo_list_id)

    @property
    def google_calendar_connected(self):
        """True when Google Calendar is authorized and the calendar provisioned."""
        return bool(self.google_calendar_refresh_token and self.google_calendar_id)

    @property
    def google_calendar_public_url(self):
        """Google's public 'add this calendar' link, or empty when not shareable."""
        if not (self.google_calendar_connected and self.google_calendar_is_public):
            return ""
        return "https://calendar.google.com/calendar/render?cid=" + quote_plus(self.google_calendar_id)

    @property
    def google_calendar_ical_url_candidate(self):
        """Where the public iCal feed would be, shared or not; fetched anonymously to test sharing."""
        if not self.google_calendar_connected:
            return ""
        return f"https://calendar.google.com/calendar/ical/{quote_plus(self.google_calendar_id)}/public/basic.ics"

    @property
    def google_calendar_ical_url(self):
        """Google's public iCal feed, or "" when not shared."""
        if not self.google_calendar_is_public:
            return ""
        return self.google_calendar_ical_url_candidate

    def _own_ical_url(self, domain):
        """This site's own iCal feed for the club."""
        return f"https://{domain}{reverse('club_events_ical', kwargs={'slug': self.slug})}"

    def calendar_subscribe_url(self, domain):
        """The link to give a member who wants these events in their calendar: the club's shared Google
        calendar, else our feed as webcal:// (an https .ics is a one-time download).
        """
        if self.google_calendar_public_url:
            return self.google_calendar_public_url
        return re.sub(r"^https?://", "webcal://", self._own_ical_url(domain))

    def calendar_feed_url(self, domain):
        """The raw iCal feed behind calendar_subscribe_url."""
        return self.google_calendar_ical_url or self._own_ical_url(domain)

    # Days since the last embed render that still count as installed.
    EVENTS_EMBED_ACTIVE_DAYS = 90

    @property
    def embeds_events_on_website(self):
        """True when the club's website has rendered our events embed recently."""
        if not self.events_website_views or not self.events_website_last_view:
            return False
        return self.events_website_last_view >= timezone.now() - datetime.timedelta(days=self.EVENTS_EMBED_ACTIVE_DAYS)

    @property
    def icon_display_url(self):
        """Full-size icon URL; from Cloudflare when migrated, else the local file"""
        return cloudflare_images.image_url(self.icon, self.cloudflare_image_id)

    @property
    def icon_thumbnail_url(self):
        """Small square icon (the club_icon alias/variant), shown beside the club name"""
        return cloudflare_images.image_url(self.icon, self.cloudflare_image_id, "club_icon")

    def save(self, *args, **kwargs):
        if not self.abbreviation and self.name:
            # club_matching owns the rule, so is_hand_written() recognises its output.
            self.abbreviation = derived_abbreviation(self.name)
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and "abbreviation" not in update_fields:
                kwargs["update_fields"] = list(update_fields) + ["abbreviation"]
        self.description = sanitize_summernote_html(self.description)
        super().save(*args, **kwargs)

    def _first_email_member_by_priority(self, specific_permission):
        """Oldest active member with an email, preferring a non-admin holder of specific_permission over the
        oldest admin, or None.
        """
        email_filter = (Q(email__isnull=False) & ~Q(email="")) | (Q(user__email__isnull=False) & ~Q(user__email=""))
        qs = self.members.filter(is_deleted=False).filter(email_filter)
        result = qs.filter(specific_permission & Q(permission_admin=False)).order_by("pk").first()
        if result:
            return result
        return qs.filter(permission_admin=True).order_by("pk").first()

    @property
    def auction_email_recipient(self):
        member = self.auction_email_member
        if (
            member
            and member.club_id == self.pk
            and member.routing_email
            and not member.is_deleted
            and (member.permission_admin or member.permission_manage_auctions)
        ):
            return member
        return self._first_email_member_by_priority(Q(permission_manage_auctions=True))

    @property
    def contact_email_recipient(self):
        member = self.contact_email_member
        if (
            member
            and member.club_id == self.pk
            and member.routing_email
            and not member.is_deleted
            and (member.permission_admin or member.permission_add_edit)
        ):
            return member
        return self._first_email_member_by_priority(Q(permission_add_edit=True))

    @property
    def auction_routing_email(self):
        recipient = self.auction_email_recipient
        return recipient.routing_email if recipient and recipient.routing_email else admin_routing_email()

    @property
    def contact_routing_email(self):
        """The routing email for club contact messages, or None to drop them."""
        recipient = self.contact_email_recipient
        if recipient and recipient.routing_email:
            return recipient.routing_email
        return None

    @property
    def donation_email_recipient(self):
        """The member donation replies are forwarded to, or None to keep them on the site. No fallback: with
        nobody named, replies aren't forwarded to an inbox we can't see.
        """
        member = self.donation_email_member
        if (
            member
            and member.club_id == self.pk
            and member.routing_email
            and not member.is_deleted
            and (member.permission_admin or member.permission_manage_donations)
        ):
            return member
        return None

    @property
    def donation_routing_email(self):
        """Where to forward donation replies, or None."""
        recipient = self.donation_email_recipient
        return recipient.routing_email if recipient else None

    @property
    def auction_sender_email(self):
        return build_routed_sender_address(f"{self.slug}-auctions")

    @property
    def contact_sender_email(self):
        return build_routed_sender_address(f"{self.slug}-contact")

    @property
    def contact_sender_email_with_name(self):
        """The From line for club mail: the club's name over the club's own address."""
        return sender_with_display_name(self.name, self.contact_sender_email)

    @property
    def donation_tracking_enabled(self):
        """True when this club may use donation tracking at all."""
        return bool(self.enable_donation_tracking)

    @property
    def sends_donation_email(self):
        """True when donation emails go out from this site (routing must actually be enabled, or Send can only fail)."""
        return (
            self.enable_donation_tracking
            and self.donation_email_mode == self.DONATION_EMAIL_MODE_ROUTED
            and email_routing_enabled()
        )

    @property
    def effective_paypal_seller(self):
        """The club's linked PayPalSeller, if any. Doesn't consider use_site_paypal_account."""
        return getattr(self, "paypal_seller", None)

    @property
    def effective_square_seller(self):
        """The SquareSeller linked to this club, if any."""
        return getattr(self, "square_seller", None)

    @property
    def uses_site_paypal(self):
        """True when this club uses the site's PayPal merchant account."""
        return bool(
            self.use_site_paypal_account
            and getattr(settings, "PAYPAL_CLIENT_ID", "")
            and getattr(settings, "PAYPAL_SECRET", "")
        )

    @property
    def uses_own_paypal_credentials(self):
        """True when this club pays through its own PayPal app credentials (non-OAuth)."""
        return bool(self.allow_non_oauth_paypal and self.paypal_client_id and self.paypal_secret)

    @property
    def paypal_credentials(self):
        """(client_id, secret) for the club's own PayPal app, or None."""
        if self.uses_own_paypal_credentials:
            return self.paypal_client_id, self.paypal_secret
        return None

    @property
    def can_accept_paypal(self):
        """True when any PayPal route is configured."""
        if self.uses_site_paypal or self.uses_own_paypal_credentials:
            return True
        seller = self.effective_paypal_seller
        return bool(seller and seller.paypal_merchant_id)

    @property
    def can_accept_square(self):
        """True when this club has a Square seller linked with an active merchant id."""
        seller = self.effective_square_seller
        return bool(seller and seller.square_merchant_id)

    @property
    def supports_paypal_subscriptions(self):
        """True when membership subscription webhooks can be verified: site account or own credentials. An
        OAuth-linked club has no usable secret.
        """
        return self.uses_site_paypal or self.uses_own_paypal_credentials

    @property
    def membership_payment_emails_enabled(self):
        return bool((self.membership_annual_fee or 0) > 0 and (self.can_accept_paypal or self.can_accept_square))


class ClubDiscordRole(models.Model):
    """Discord roles associated with a club"""

    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="discord_roles")
    role_id = models.CharField(max_length=20, blank=True, help_text="Discord role snowflake ID")
    role_name = models.CharField(max_length=100)
    is_default = models.BooleanField(default=False, help_text="Assign this role to users who register via Discord")
    bap_points_for_role = models.PositiveIntegerField(
        default=0, help_text="Assign this role when a member reaches this many BAP points (0 = not used)"
    )
    hap_points_for_role = models.PositiveIntegerField(
        default=0, help_text="Assign this role when a member reaches this many HAP points (0 = not used)"
    )
    is_unpaid_role = models.BooleanField(
        default=False, help_text="Assign this role to members with an expired membership"
    )
    is_paid_role = models.BooleanField(
        default=False, help_text="Assign this role to members with a current paid membership"
    )
    bot_can_manage = models.BooleanField(
        default=True,
        help_text="False when this role is at or above the bot's own role in the Discord hierarchy; the bot cannot assign or remove roles at its own level or higher",
    )
    createdon = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.role_name}"


class ContactRecord(models.Model):
    """Abstract contact record shared by AuctionTOS and ClubMember."""

    email = models.EmailField(null=True, blank=True, db_index=True)
    EMAIL_ADDRESS_STATUSES = (
        ("BAD", "Invalid"),
        ("UNKNOWN", "Unknown"),
        ("VALID", "Verified"),
    )
    email_address_status = models.CharField(
        max_length=20, choices=EMAIL_ADDRESS_STATUSES, default="UNKNOWN", blank=True
    )
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    address = models.CharField(max_length=500, blank=True)
    memo = models.TextField(blank=True)

    @property
    def phone_as_string(self):
        """Add proper dashes to phone"""
        if not self.phone_number:
            return ""
        n = re.sub(r"\D", "", self.phone_number)
        if len(n) == 10:
            return f"{n[:3]}-{n[3:6]}-{n[6:]}"
        return n

    class Meta:
        abstract = True


def _generate_unique_bidder_number(*, is_taken, preferred=None, phone=None, address=None, last_used=None):
    """A bidder number, retrying on collision. ``is_taken(number)`` checks the caller's scope.

    Reuses ``last_used`` if free; else seeds from the last 3 digits of phone, address, then
    ``preferred``, skipping 13-19 (look like ages); up to 6000 random tries in [1, 999], else "ERROR".
    """
    dont_use_these = ["13", "14", "15", "16", "17", "18", "19"]
    if last_used and not is_taken(last_used):
        return last_used
    search = None
    if phone:
        search = re.search(r"([\d]{3}$)|$", phone).group()
    if (not search or str(search) in dont_use_these) and address:
        search = re.search(r"([\d]{3}$)|$", address).group()
    if preferred:
        search = preferred
    try:
        if str(search)[0] == "0":
            search = search[1:]
        if str(search)[0] == "0":
            search = search[1:]
    except Exception:
        pass
    failsafe = 0
    while failsafe < 6000:
        search = str(search)
        if search[:-2] not in dont_use_these and search != "None":
            if not is_taken(search):
                return search
        search = randint(1, 999)
        failsafe += 1
    return "ERROR"


class ClubMember(CachedPropertiesMixin, ContactRecord):
    """A member of a club. Similar to AuctionTOS but for club membership."""

    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="club_memberships")
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="members")
    name = models.CharField(max_length=200, blank=True)
    discord_id = models.CharField(max_length=100, blank=True, null=True)
    discord_username = models.CharField(
        max_length=100, blank=True, null=True, help_text="Discord username (e.g. cooluser)"
    )
    discord_role_auto_managed = models.BooleanField(
        default=True,
        verbose_name="Automatically manage Discord role",
        help_text="When checked, the Discord role is assigned automatically based on membership status and BAP/HAP points.",
    )
    discord_role_override = models.ForeignKey(
        "ClubDiscordRole",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="manually_assigned_members",
        verbose_name="Discord role (manual override)",
        help_text="Only used when automatic role management is disabled.",
    )
    last_discord_role_assigned = models.ForeignKey(
        "ClubDiscordRole",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Last role auto-assigned via the Discord API; used to detect when the role needs to change.",
    )
    bap_points = models.PositiveIntegerField(default=0)
    hap_points = models.PositiveIntegerField(default=0)
    culture_points = models.PositiveIntegerField(default=0, help_text="Culture Award Program points.")
    bap_points_ytd = models.PositiveIntegerField(default=0, help_text="BAP points earned this calendar year.")
    hap_points_ytd = models.PositiveIntegerField(default=0, help_text="HAP points earned this calendar year.")
    culture_points_ytd = models.PositiveIntegerField(default=0, help_text="Culture points earned this calendar year.")
    membership_last_paid = models.DateField(null=True, blank=True)
    membership_expiration_date = models.DateField(null=True, blank=True)
    paypal_subscription_id = models.CharField(
        max_length=50,
        blank=True,
        default="",
        db_index=True,
        help_text=(
            "PayPal recurring subscription ID (e.g. I-XXXXXXXX). Set/cleared by the PayPal subscription "
            "webhook. While set, the member auto-renews via PayPal: expiring-soon reminders are skipped "
            "and the invoice membership-renewal box is disabled."
        ),
    )
    membership_number = models.BigIntegerField(
        default=_default_membership_number,
        unique=True,
        help_text="Unique membership number assigned to this member.",
    )
    uuid = models.UUIDField(default=uuid_module.uuid4, unique=True, editable=False, db_index=True)
    membership_expiration_reminder_due = models.DateTimeField(null=True, blank=True)
    membership_expiration_reminder_30_days_due = models.DateTimeField(null=True, blank=True)
    send_welcome_email = models.BooleanField(default=True)
    welcome_email_sent = models.BooleanField(default=False)
    createdon = models.DateTimeField(auto_now_add=True, verbose_name="date joined")
    added_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="added_club_members"
    )
    CONTACT_STATUS_CHOICES = (
        ("contact", "Contact normally"),
        ("non_essential", "No non-essential emails"),
        ("do_not_contact", "Do not contact"),
    )
    contact_status = models.CharField(max_length=20, choices=CONTACT_STATUS_CHOICES, default="contact")
    discord_roles = models.TextField(blank=True)
    is_deleted = models.BooleanField(default=False, db_index=True)
    source = models.CharField(max_length=200, default="manually_added")
    admin_edited = models.BooleanField(
        default=True,
        help_text=(
            "A club admin created or has edited this record, so the club owns it: deleting the "
            "person's site account only removes the account link and leaves the club's copy of "
            "their details alone.  Unchecked only for rows a member made about themselves and no "
            "admin has touched since — those are deleted with the account."
        ),
    )
    permission_admin = models.BooleanField(
        default=False, help_text="Full admin access — grants all other permissions.  Use only if absolutely necessary."
    )
    permission_view = models.BooleanField(
        default=False, help_text="View the member list.  Other permissions implicitly grant this."
    )
    permission_export = models.BooleanField(default=False, help_text="Import and export member data as CSV.")
    permission_add_edit = models.BooleanField(default=False, help_text="Add and edit members.")
    permission_edit_club = models.BooleanField(
        default=False,
        help_text="Change club setup, Discord, and API keys.  Nearly as dangerous as admin.",
    )
    permission_money = models.BooleanField(
        default=False,
        help_text="Manage membership/payment settings and view the treasurer's report.",
    )
    permission_manage_auctions = models.BooleanField(default=False, help_text="Manage auctions for this club.")
    permission_manage_bap = models.BooleanField(default=False, help_text="Manage BAP/HAP points.")
    permission_manage_donations = models.BooleanField(
        default=False,
        help_text="Add and email donation vendors.  Separate from member management: it sends mail in the club's name.",
    )
    permission_send_announcements = models.BooleanField(
        default=False,
        help_text=(
            "Write club announcements.  Its own permission because an announcement goes straight "
            "to Discord, members' phones and the club's mailing list with nobody in between."
        ),
    )
    possible_duplicate = models.ForeignKey(
        "ClubMember",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="duplicate_of",
        help_text="Another club member with the same name; may be a duplicate",
    )
    bidder_number = models.CharField(
        max_length=20,
        default="",
        blank=True,
        db_index=True,
        help_text="Used when the club manages auction participants directly. Must be unique within this club.",
    )
    bidding_allowed = models.BooleanField(
        default=True,
        help_text="When the club manages auction participants, controls whether this member can place bids.",
    )
    selling_allowed = models.BooleanField(
        default=True,
        help_text="When the club manages auction participants, controls whether this member can submit lots.",
    )
    lat = models.FloatField(null=True, blank=True, help_text="Latitude geocoded from member's address")
    lng = models.FloatField(null=True, blank=True, help_text="Longitude geocoded from member's address")
    last_club_activity = models.DateTimeField(null=True, blank=True, help_text="Last recorded activity in this club")
    # Denormalized from UserData totals (which loop every lot) for Mailchimp tags and filtering.
    cached_total_sold = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    cached_total_bought = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    MAILCHIMP_STATUS_CHOICES = (
        ("", "Not synced"),
        ("subscribed", "Subscribed"),
        ("unsubscribed", "Unsubscribed"),
        ("cleaned", "Cleaned (bounced)"),
        ("pending", "Pending"),
        ("archived", "Archived"),
    )
    mailchimp_status = models.CharField(
        max_length=20,
        choices=MAILCHIMP_STATUS_CHOICES,
        default="",
        blank=True,
        db_index=True,
        help_text="Last known Mailchimp subscription status; set by sync and by the unsubscribe webhook.",
    )
    mailchimp_web_id = models.CharField(
        max_length=50, blank=True, help_text="Mailchimp internal web_id, used to build the 'View in Mailchimp' link."
    )
    mailchimp_last_synced = models.DateTimeField(null=True, blank=True)
    # Brevo status bookkeeping, mirroring Mailchimp's.
    BREVO_STATUS_CHOICES = (
        ("", "Not synced"),
        ("subscribed", "Subscribed"),
        ("unsubscribed", "Unsubscribed"),
        ("cleaned", "Cleaned (bounced/spam)"),
        ("archived", "Archived"),
    )
    brevo_status = models.CharField(
        max_length=20,
        choices=BREVO_STATUS_CHOICES,
        default="",
        blank=True,
        db_index=True,
        help_text="Last known Brevo subscription status; set by sync and by the unsubscribe webhook.",
    )
    brevo_contact_id = models.CharField(
        max_length=50, blank=True, help_text="Brevo internal contact id, used to build the 'View in Brevo' link."
    )
    brevo_last_synced = models.DateTimeField(null=True, blank=True)
    # Apple Wallet: the shared secret baked into the .pkpass (``Authorization: ApplePass <token>``),
    # generated when a pass is first built.
    apple_pass_auth_token = models.CharField(max_length=64, blank=True, default="", editable=False)
    # Bumped when pass content changes; drives Last-Modified and passesUpdatedSince.
    apple_pass_updated = models.DateTimeField(default=timezone.now, editable=False)

    @property
    def has_any_permission(self):
        return any(
            [
                self.permission_admin,
                self.permission_view,
                self.permission_export,
                self.permission_add_edit,
                self.permission_edit_club,
                self.permission_money,
                self.permission_manage_auctions,
                self.permission_manage_bap,
                self.permission_manage_donations,
                self.permission_send_announcements,
            ]
        )

    def __str__(self):
        if self.name:
            return self.name
        if self.email:
            return self.email
        return f"Member #{self.pk}"

    @property
    def routing_email(self):
        if self.email:
            return self.email
        if self.user and self.user.email:
            return self.user.email
        return ""

    @property
    def has_paypal_subscription(self) -> bool:
        """True when this member auto-renews through a PayPal recurring subscription."""
        return bool(self.paypal_subscription_id)

    @property
    def is_paid_member(self) -> bool:
        """True when dues are current. The single source of truth for UI gates and wallet passes."""
        today = timezone.now().date()
        if self.membership_expiration_date:
            return self.membership_expiration_date >= today
        if self.membership_last_paid:
            if self.club.membership_system == "january_first":
                return self.membership_last_paid >= datetime.date(today.year, 1, 1)
            return self.membership_last_paid >= today - datetime.timedelta(days=365)
        return False

    @property
    def effective_expiration_date(self):
        """The date this membership is valid through, or None (no memberships or no payment). Mirrors
        is_paid_member.
        """
        if self.club.membership_system == "none":
            return None
        if self.membership_expiration_date:
            return self.membership_expiration_date
        if self.membership_last_paid:
            paid = self.membership_last_paid
            if self.club.membership_system == "january_first":
                return datetime.date(paid.year + 1, 1, 1)
            return paid + datetime.timedelta(days=365)
        return None

    @property
    def wallet_status_text(self):
        """Wallet pass status line ("Expired 1 Jan 2025" / "Valid through 1 Jan 2025"), or None. Text only,
        never a programmatic wallet expiry, which would archive the card.
        """
        if self.club.membership_system == "none":
            return None
        expiration = self.effective_expiration_date
        if not self.is_paid_member:
            if expiration:
                return f"Expired {expiration.strftime('%-d %b %Y')}"
            return "Unpaid/expired"
        if expiration:
            return f"Valid through {expiration.strftime('%-d %b %Y')}"
        return "Valid"

    @property
    def wallet_status_is_expired(self):
        """True when the wallet pass should render its expired styling (red)."""
        return self.club.membership_system != "none" and not self.is_paid_member

    @property
    def wallet_header_text(self):
        """Wallet pass type line: "Active Paid Membership"/"Unpaid Membership" for clubs with dues, else
        "Membership".
        """
        if self.club.membership_system != "none" and (self.club.membership_annual_fee or 0) > 0:
            return "Active Paid Membership" if self.is_paid_member else "Unpaid Membership"
        return "Membership"

    @cached_property
    def discord_role(self):
        """The ClubDiscordRole this member should have: manual override > None if unconfigured > unpaid role
        if expired > highest BAP/HAP threshold met > paid role > unpaid role > None.
        """
        if not self.discord_role_auto_managed:
            return self.discord_role_override

        if not self.club.discord_server_id:
            return None

        roles_qs = list(self.club.discord_roles.all())
        if not roles_qs:
            return None

        today = timezone.now().date()
        if self.membership_expiration_date:
            membership_valid = self.membership_expiration_date >= today
        elif self.membership_last_paid:
            club = self.club
            if club.membership_system == "january_first":
                membership_valid = self.membership_last_paid >= datetime.date(today.year, 1, 1)
            else:
                membership_valid = self.membership_last_paid >= today - datetime.timedelta(days=365)
        else:
            membership_valid = False

        if not membership_valid:
            unpaid_role = next((r for r in roles_qs if r.is_unpaid_role), None)
            if unpaid_role:
                return unpaid_role
            return None

        bap_roles = [
            (r, r.bap_points_for_role)
            for r in roles_qs
            if r.bap_points_for_role > 0 and r.bap_points_for_role <= self.bap_points
        ]
        hap_roles = [
            (r, r.hap_points_for_role)
            for r in roles_qs
            if r.hap_points_for_role > 0 and r.hap_points_for_role <= self.hap_points
        ]
        point_roles = bap_roles + hap_roles
        if point_roles:
            return max(point_roles, key=lambda pair: pair[1])[0]

        paid_role = next(
            (r for r in roles_qs if r.is_paid_role and r.bap_points_for_role == 0 and r.hap_points_for_role == 0),
            None,
        )
        if paid_role:
            return paid_role

        # Every member gets some role even with incomplete config.
        return next((r for r in roles_qs if r.is_unpaid_role), None)

    def maybe_assign_discord_role(self):
        """Assign the correct Discord role and remove other auto-managed club roles."""
        import requests as _requests
        from django.conf import settings as _settings

        if not self.discord_id or not self.club.discord_server_id:
            return
        bot_token = getattr(_settings, "DISCORD_BOT_TOKEN", "")
        if not bot_token:
            return

        role = self.discord_role
        guild_id = self.club.discord_server_id
        user_id = self.discord_id
        headers = {"Authorization": f"Bot {bot_token}"}
        base_url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}/roles"

        role_sync_succeeded = True

        for club_role in self.club.discord_roles.all():
            if not club_role.bot_can_manage:
                continue
            if role is None or club_role.pk != role.pk:
                try:
                    response = _requests.delete(f"{base_url}/{club_role.role_id}", headers=headers, timeout=10)
                    if response.status_code not in [200, 204, 404]:
                        role_sync_succeeded = False
                except Exception:
                    role_sync_succeeded = False

        if role:
            if not role.bot_can_manage:
                # Recorded even if unassignable, so the daily task doesn't retry forever.
                if role_sync_succeeded:
                    ClubMember.objects.filter(pk=self.pk).update(last_discord_role_assigned=role)
                return
            try:
                response = _requests.put(f"{base_url}/{role.role_id}", headers=headers, timeout=10)
                if response.status_code not in [200, 201, 204]:
                    role_sync_succeeded = False
            except Exception:
                role_sync_succeeded = False

        if role_sync_succeeded:
            ClubMember.objects.filter(pk=self.pk).update(last_discord_role_assigned=role)

    @property
    def display_name(self):
        """Name for display — always non-empty."""
        return str(self)

    @property
    def member_page_url(self):
        """Relative URL for this member's wallet/identity page (UUID-keyed)."""
        from django.urls import reverse

        return reverse("club_member_by_uuid", kwargs={"slug": self.club.slug, "uuid": self.uuid})

    @cached_property
    def wallet_link(self):
        """Absolute URL for adding this membership to a wallet."""
        from django.contrib.sites.models import Site

        try:
            current_site = Site.objects.get_current()
            domain = current_site.domain
        except Site.DoesNotExist:
            # Fallback for test environments or missing Site
            domain = "localhost"
        return f"https://{domain}{self.member_page_url}"

    @cached_property
    def simple_membership_link(self):
        """Absolute URL for the member-number page."""
        from django.contrib.sites.models import Site
        from django.urls import reverse

        try:
            current_site = Site.objects.get_current()
            domain = current_site.domain
        except Site.DoesNotExist:
            # Fallback for test environments or missing Site
            domain = "localhost"
        path = reverse(
            "club_member_by_number",
            kwargs={"slug": self.club.slug, "number": self.membership_number},
        )
        return f"https://{domain}{path}"

    @cached_property
    def barcode_image_link(self):
        """Absolute URL to an SVG barcode for the membership number, or ""."""
        if not self.membership_number:
            return ""
        from django.contrib.sites.models import Site
        from django.urls import reverse

        try:
            current_site = Site.objects.get_current()
            domain = current_site.domain
        except Site.DoesNotExist:
            domain = "localhost"
        path = reverse(
            "club_barcode",
            kwargs={"slug": self.club.slug, "value": int(self.membership_number)},
        )
        return f"https://{domain}{path}"

    @cached_property
    def barcode_image_link_png(self):
        """Absolute URL to a PNG barcode (better in email clients), or ""."""
        if not self.membership_number:
            return ""
        from django.contrib.sites.models import Site
        from django.urls import reverse

        try:
            current_site = Site.objects.get_current()
            domain = current_site.domain
        except Site.DoesNotExist:
            domain = "localhost"
        path = reverse(
            "club_barcode_png",
            kwargs={"slug": self.club.slug, "value": int(self.membership_number)},
        )
        return f"https://{domain}{path}"

    def _distance_to_club_miles(self):
        """Return distance in miles from this member to the club location, or None."""
        annotated = getattr(self, "distance_to", None)
        if annotated is not None:
            return float(annotated)
        if not (self.lat and self.lng and self.club.latitude and self.club.longitude):
            return None
        import math

        lat1 = math.radians(self.club.latitude)
        lon1 = math.radians(self.club.longitude)
        lat2 = math.radians(self.lat)
        lon2 = math.radians(self.lng)
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return 6371 * 0.6213712 * 2 * math.asin(math.sqrt(a))

    @property
    def less_than_10_miles(self):
        d = self._distance_to_club_miles()
        return d is not None and d < 10

    @property
    def less_than_30_miles(self):
        d = self._distance_to_club_miles()
        return d is not None and d < 30

    @property
    def more_than_30_miles(self):
        d = self._distance_to_club_miles()
        return d is not None and d >= 30

    # The full tag vocabulary, shared by sync (removes inactive tags) and segment creation.
    MAILCHIMP_TAGS = (
        "expiring-soon",
        "expired",
        "long-term-member",
        "new-member",
        "admin",
        "nearby",
        "medium-distance",
        "long-distance",
        "power-seller",
        "power-buyer",
        "auction-checkin",
        "discord-connected",
        "probably-inactive",
    )
    POWER_USER_THRESHOLD = 1000

    @property
    def first_name(self):
        """First token of the member's name (for the Mailchimp FNAME merge field)."""
        return (self.name or "").strip().split(" ", 1)[0] if (self.name or "").strip() else ""

    @property
    def last_name(self):
        """Everything after the first token of the member's name (Mailchimp LNAME)."""
        parts = (self.name or "").strip().split(" ", 1)
        return parts[1].strip() if len(parts) > 1 else ""

    @property
    def is_expired(self):
        if self.membership_expiration_date:
            return self.membership_expiration_date < timezone.now().date()
        return bool(self.club.membership_annual_fee) and not self.is_paid_member

    @property
    def is_expiring_soon(self):
        """Membership expires within the next 30 days (and is not already expired)."""
        if not self.membership_expiration_date:
            return False
        today = timezone.now().date()
        return today <= self.membership_expiration_date <= today + datetime.timedelta(days=30)

    @property
    def is_long_term_member(self):
        if not self.createdon:
            return False
        return self.createdon <= timezone.now() - datetime.timedelta(days=365 * 5)

    @property
    def is_new_member(self):
        if not self.createdon:
            return False
        return self.createdon >= timezone.now() - datetime.timedelta(days=182)

    @property
    def is_probably_inactive(self):
        """No recorded club activity in the last 6 months (ignores brand-new members)."""
        cutoff = timezone.now() - datetime.timedelta(days=182)
        if self.last_club_activity:
            return self.last_club_activity < cutoff
        # No activity ever: inactive only once past the window.
        return bool(self.createdon and self.createdon < cutoff)

    @cached_property
    def has_auction_checkin(self):
        return self.auction_tos_records.filter(checked_in__isnull=False).exists()

    @property
    def is_discord_connected(self):
        return bool(self.discord_id)

    @property
    def is_power_seller(self):
        return bool(self.cached_total_sold and self.cached_total_sold > self.POWER_USER_THRESHOLD)

    @property
    def is_power_buyer(self):
        return bool(self.cached_total_bought and self.cached_total_bought > self.POWER_USER_THRESHOLD)

    def refresh_cached_totals(self, save=True):
        """Recompute the site-wide sold/bought totals from the linked user. True if either changed; save=True
        persists just those columns.
        """
        if not self.user_id:
            return False
        userdata = getattr(self.user, "userdata", None)
        if userdata is None:
            return False
        new_sold = Decimal(str(userdata.total_sold or 0))
        new_bought = Decimal(str(userdata.total_spent or 0))
        changed = new_sold != (self.cached_total_sold or 0) or new_bought != (self.cached_total_bought or 0)
        self.cached_total_sold = new_sold
        self.cached_total_bought = new_bought
        if changed and save:
            ClubMember.objects.filter(pk=self.pk).update(cached_total_sold=new_sold, cached_total_bought=new_bought)
        return changed

    def compute_mailchimp_tags(self):
        """{tag_name: is_active} for every tag, so sync can add and remove."""
        return {
            "expiring-soon": self.is_expiring_soon,
            "expired": self.is_expired,
            "long-term-member": self.is_long_term_member,
            "new-member": self.is_new_member,
            "admin": self.has_any_permission,
            "nearby": self.less_than_10_miles,
            "medium-distance": self.less_than_30_miles and not self.less_than_10_miles,
            "long-distance": self.more_than_30_miles,
            "power-seller": self.is_power_seller,
            "power-buyer": self.is_power_buyer,
            "auction-checkin": self.has_auction_checkin,
            "discord-connected": self.is_discord_connected,
            "probably-inactive": self.is_probably_inactive,
        }

    def update_last_club_activity(self):
        ClubMember.objects.filter(pk=self.pk).update(last_club_activity=timezone.now())

    def calculate_membership_expiration_reminder_due(self, days_before=1):
        send_reminder = (
            self.club.send_membership_expiration_reminders_30_days
            if days_before == 30
            else self.club.send_membership_expiration_reminders
        )
        if not send_reminder or not self.club.membership_payment_emails_enabled:
            return None
        expiration_date = self.membership_expiration_date
        if not expiration_date and self.membership_last_paid:
            paid = self.membership_last_paid
            if self.club.membership_system == "january_first":
                expiration_date = datetime.date(paid.year + 1, 1, 1)
            else:
                expiration_date = paid + datetime.timedelta(days=365)
        if not expiration_date:
            return None
        reminder_date = expiration_date - datetime.timedelta(days=days_before)
        return timezone.make_aware(datetime.datetime.combine(reminder_date, datetime.time(hour=12)))

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["club", "bidder_number"],
                condition=~Q(bidder_number=""),
                name="unique_bidder_number_per_club",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.email:
            self.email = normalize_email(self.email)
        # Repick a colliding random membership_number before INSERT.
        _discord_watched = ("discord_id", "discord_role_override_id", "discord_role_auto_managed")
        _is_new = not self.pk
        if not _is_new:
            _discord_old = ClubMember.objects.filter(pk=self.pk).values(*_discord_watched).first()
            _discord_changed = _discord_old and any(getattr(self, f) != _discord_old[f] for f in _discord_watched)
        else:
            _discord_changed = False
        if self.membership_number and not self.pk:
            if ClubMember.objects.filter(membership_number=self.membership_number).exists():
                self.membership_number = _pick_unique_membership_number()
        if not self.user_id and self.email:
            self.user = User.objects.filter(email__iexact=self.email).order_by("pk").first()
        previous_membership_last_paid = None
        previous_expiration_date = None
        previous_email = None
        previous_reminder_due = None
        previous_reminder_30_days_due = None
        if self.pk:
            prev = (
                ClubMember.objects.filter(pk=self.pk)
                .values(
                    "membership_last_paid",
                    "membership_expiration_date",
                    "email",
                    "membership_expiration_reminder_due",
                    "membership_expiration_reminder_30_days_due",
                )
                .first()
            )
            if prev:
                previous_membership_last_paid = prev["membership_last_paid"]
                previous_expiration_date = prev["membership_expiration_date"]
                previous_email = prev["email"]
                previous_reminder_due = prev["membership_expiration_reminder_due"]
                previous_reminder_30_days_due = prev["membership_expiration_reminder_30_days_due"]
        expiration_changed = (
            self.membership_last_paid != previous_membership_last_paid
            or self.membership_expiration_date != previous_expiration_date
        )
        if expiration_changed and not getattr(self, "_preserve_membership_email_schedule", False):
            new_reminder = self.calculate_membership_expiration_reminder_due(days_before=1)
            new_reminder_30_days = self.calculate_membership_expiration_reminder_due(days_before=30)
            min_reminder = timezone.now() + datetime.timedelta(days=30)
            if new_reminder is not None and (previous_reminder_due is None or new_reminder < previous_reminder_due):
                new_reminder = max(new_reminder, min_reminder)
            if new_reminder_30_days is not None and (
                previous_reminder_30_days_due is None or new_reminder_30_days < previous_reminder_30_days_due
            ):
                new_reminder_30_days = max(new_reminder_30_days, min_reminder)
            self.membership_expiration_reminder_due = new_reminder
            self.membership_expiration_reminder_30_days_due = new_reminder_30_days
        if self.email and self.email != previous_email:
            self.email_address_status = "UNKNOWN"
        if self.email and self.email_address_status == "UNKNOWN":
            existing = (
                ClubMember.objects.exclude(pk=self.pk or 0)
                .exclude(email_address_status="UNKNOWN")
                .filter(email=self.email, is_deleted=False)
                .order_by("-createdon")
                .first()
            )
            if existing:
                self.email_address_status = existing.email_address_status
            else:
                existing_tos = (
                    AuctionTOS.objects.exclude(email_address_status="UNKNOWN")
                    .filter(email=self.email, auction__club=self.club)
                    .order_by("-createdon")
                    .first()
                )
                if existing_tos:
                    self.email_address_status = existing_tos.email_address_status
        if self.is_deleted:
            # Clear duplicate links both ways; our own column needs update() since callers soft-delete
            # with update_fields=["is_deleted"].
            if self.possible_duplicate_id:
                ClubMember.objects.filter(pk=self.possible_duplicate_id).update(possible_duplicate=None)
                ClubMember.objects.filter(pk=self.pk).update(possible_duplicate=None)
                self.possible_duplicate_id = None
            ClubMember.objects.filter(possible_duplicate_id=self.pk).update(possible_duplicate=None)
            super().save(*args, **kwargs)
            if _discord_changed or (_is_new and self.discord_id):
                self.maybe_assign_discord_role()
            return
        super().save(*args, **kwargs)
        if _discord_changed or (_is_new and self.discord_id):
            self.maybe_assign_discord_role()
        if self.name:
            # Exact or nickname name match, as AuctionTOS.save.
            duplicate = self.club.find_member(name=self.name, exclude_pk=self.pk)
            if duplicate:
                ClubMember.objects.filter(pk=self.pk).update(possible_duplicate=duplicate.pk)
                ClubMember.objects.filter(pk=duplicate.pk).update(possible_duplicate=self.pk)
                # Keep in sync with update() above.
                self.possible_duplicate = duplicate
            else:
                if self.possible_duplicate_id:
                    ClubMember.objects.filter(pk=self.possible_duplicate_id).update(possible_duplicate=None)
                ClubMember.objects.filter(pk=self.pk).update(possible_duplicate=None)
                self.possible_duplicate = None

    def generate_bidder_number(self, save=True):
        """Assign and return a unique club-scoped bidder_number. Doesn't write userdata.preferred_bidder_number."""
        preferred = None
        if self.user_id:
            try:
                preferred = self.user.userdata.preferred_bidder_number or None
            except Exception:
                preferred = None
        self.bidder_number = _generate_unique_bidder_number(
            is_taken=lambda n: (
                ClubMember.objects.filter(club_id=self.club_id, bidder_number=n).exclude(pk=self.pk or 0).exists()
            ),
            preferred=preferred,
            phone=self.phone_number,
            address=self.address,
        )
        if save:
            ClubMember.objects.filter(pk=self.pk).update(bidder_number=self.bidder_number)
        return self.bidder_number


class AppleDeviceRegistration(models.Model):
    """A device holding a member's Apple Wallet pass; push_token is notified when the pass changes."""

    member = models.ForeignKey(ClubMember, on_delete=models.CASCADE, related_name="apple_device_registrations")
    device_library_identifier = models.CharField(max_length=255, db_index=True)
    push_token = models.CharField(max_length=255)
    createdon = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["member", "device_library_identifier"], name="unique_member_device_registration"
            )
        ]

    def __str__(self):
        return f"{self.device_library_identifier} for {self.member}"


class ClubHistory(models.Model):
    """Changelog of changes made to a club"""

    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="history")
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=800, blank=True, null=True)
    # See AuctionHistory.changed_fields.
    changed_fields = models.JSONField(default=dict, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    applies_to = models.CharField(
        max_length=20,
        choices=(
            ("RULES", "Rules"),
            ("MEMBERS", "Members"),
            ("MEMBERSHIP", "Membership"),
            ("SETTINGS", "Settings"),
            ("BAP", "BAP"),
            ("DONATIONS", "Donations"),
            ("ANNOUNCEMENTS", "Announcements"),
        ),
        blank=True,
        null=True,
    )

    def __str__(self):
        if self.user:
            return f"{self.user.first_name} {self.user.last_name} {self.action}"
        return f"System {self.action}"

    class Meta:
        ordering = ["-timestamp"]
        verbose_name_plural = "Club history"


def _default_donation_routing_key():
    """Random 10-digit key in ``<club-slug>-donations-<key>@<domain>``. Digits only, globally unique, so a
    reply stays tied to one vendor if the slug changes.
    """
    for _attempt in range(20):
        key = str(secrets.randbelow(9_000_000_000) + 1_000_000_000)
        if not DonationVendor.objects.filter(routing_key=key).exists():
            return key
    return str(secrets.randbelow(9_000_000_000) + 1_000_000_000)


class DonationUnsubscribe(models.Model):
    """A vendor who asked never to be contacted about donations. Keyed on email site-wide, so re-adding
    them doesn't restart mail. No UI undo, on purpose.
    """

    email = models.CharField(max_length=255, unique=True, db_index=True)
    createdon = models.DateTimeField(auto_now_add=True)
    club = models.ForeignKey(
        Club,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="The club whose email carried the unsubscribe link that was clicked.",
    )

    class Meta:
        verbose_name = "Donation unsubscribe"
        verbose_name_plural = "Donation unsubscribes"

    def __str__(self):
        return str(self.email)

    @classmethod
    def is_unsubscribed(cls, email):
        email = normalize_email(email)
        if not email:
            return False
        return cls.objects.filter(email=email).exists()


class DonationVendor(models.Model):
    """A business a club is asking to donate, and where that conversation has got to."""

    STATUS_NEW = "new"
    STATUS_EMAIL_SENT = "sent"
    STATUS_INTERESTED = "interested"
    STATUS_PROMISED = "promised"
    STATUS_RECEIVED = "received"
    STATUS_NOT_INTERESTED = "not_interested"
    STATUS_DO_NOT_CONTACT = "do_not_contact"
    STATUS_CHOICES = (
        (STATUS_NEW, "New"),
        (STATUS_EMAIL_SENT, "Initial email sent"),
        (STATUS_INTERESTED, "Interested"),
        (STATUS_PROMISED, "Donation promised"),
        (STATUS_RECEIVED, "Donation received"),
        (STATUS_NOT_INTERESTED, "Not interested"),
        (STATUS_DO_NOT_CONTACT, "Do not contact"),
    )
    # Statuses the LLM may set. "Received" needs a human; "Do not contact" only via unsubscribe.
    LLM_ASSIGNABLE_STATUSES = (STATUS_INTERESTED, STATUS_PROMISED, STATUS_NOT_INTERESTED)

    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="donation_vendors")
    name = models.CharField(max_length=255, verbose_name="Vendor name")
    contact_name = models.CharField(max_length=255, blank=True, default="")
    email = models.CharField(max_length=255, blank=True, default="", db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_NEW, db_index=True)
    last_contact = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this vendor was last emailed, or last replied.",
    )
    followup_due = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When to chase this vendor again if they haven't replied.",
    )
    context = models.TextField(
        blank=True,
        default="",
        help_text="What this vendor does, any history of donations, and anything else worth telling the model.",
    )
    routing_key = models.CharField(
        max_length=10,
        unique=True,
        db_index=True,
        default=_default_donation_routing_key,
        editable=False,
        help_text="Identifies this vendor in the reply-to address so their replies land on their row.",
    )
    uuid = models.UUIDField(default=uuid_module.uuid4, unique=True, editable=False, db_index=True)
    unsubscribed = models.BooleanField(
        default=False,
        help_text="The vendor used the unsubscribe link. Permanent, and applies to every club on this site.",
    )
    createdon = models.DateTimeField(auto_now_add=True)
    createdby = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    is_deleted = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["name"]
        # No DB uniqueness on (club, email): it would need to be conditional (blank emails), and
        # MariaDB silently skips those (W036). DonationVendorForm.clean_email checks instead.
        indexes = [models.Index(fields=["club", "status"])]

    def __str__(self):
        return str(self.name)

    def save(self, *args, **kwargs):
        self.email = normalize_email(self.email)
        # Unsubscribed anywhere on the site is unsubscribed here.
        if self.email and not self.unsubscribed and DonationUnsubscribe.is_unsubscribed(self.email):
            self.unsubscribed = True
            if "update_fields" in kwargs and kwargs["update_fields"] is not None:
                kwargs["update_fields"] = list(kwargs["update_fields"]) + ["unsubscribed"]
        if self.unsubscribed and self.status != self.STATUS_DO_NOT_CONTACT:
            self.status = self.STATUS_DO_NOT_CONTACT
            if "update_fields" in kwargs and kwargs["update_fields"] is not None:
                kwargs["update_fields"] = list(kwargs["update_fields"]) + ["status"]
        super().save(*args, **kwargs)

    @property
    def can_be_contacted(self):
        """Whether we're allowed to write to this vendor at all."""
        if self.is_deleted or not self.email:
            return False
        if self.unsubscribed or self.status == self.STATUS_DO_NOT_CONTACT:
            return False
        return not DonationUnsubscribe.is_unsubscribed(self.email)

    @property
    def cannot_contact_reason(self):
        """Why the Contact button is unavailable, or "". Shown as a tooltip."""
        if not self.email:
            return "Add an email address for this vendor first"
        if self.unsubscribed:
            return "This vendor unsubscribed and cannot be contacted again"
        if self.status == self.STATUS_DO_NOT_CONTACT:
            return "This vendor is marked do not contact"
        if DonationUnsubscribe.is_unsubscribed(self.email):
            return "This email address unsubscribed from donation requests"
        return ""

    @property
    def reply_to_address(self):
        """The per-vendor reply address, or None when routing is off."""
        return build_routed_sender_address(f"{self.club.slug}-donations-{self.routing_key}")

    @property
    def unsubscribe_url(self):
        return reverse("donation_unsubscribe", kwargs={"uuid": self.uuid})

    @property
    def is_followup_due(self):
        return bool(self.followup_due and self.followup_due <= timezone.now())

    def schedule_followup(self, *, from_time=None):
        """Set the follow-up date to the club's configured interval from now."""
        base = from_time or timezone.now()
        self.followup_due = base + datetime.timedelta(days=self.club.donation_followup_days or 7)
        return self.followup_due


class DonationEmail(models.Model):
    """One message to or from a donation vendor: a record, not a mail client. Outgoing rows on send or copy;
    incoming from the inbound webhook. Plain text, images stripped.
    """

    DIRECTION_INCOMING = "in"
    DIRECTION_OUTGOING = "out"
    DIRECTION_CHOICES = (
        (DIRECTION_INCOMING, "Incoming"),
        (DIRECTION_OUTGOING, "Outgoing"),
    )

    vendor = models.ForeignKey(DonationVendor, on_delete=models.CASCADE, related_name="emails")
    direction = models.CharField(max_length=3, choices=DIRECTION_CHOICES, db_index=True)
    sender = models.CharField(max_length=255, blank=True, default="")
    recipients = models.CharField(max_length=1000, blank=True, default="")
    subject = models.CharField(max_length=500, blank=True, default="")
    body = models.TextField(blank=True, default="")
    summary = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text="One-line summary, written by the language model for incoming mail.",
    )
    date = models.DateTimeField(default=timezone.now, db_index=True)
    message_id = models.CharField(
        max_length=500,
        blank=True,
        default="",
        db_index=True,
        help_text="The Message-ID header, used to drop duplicates when a message is delivered twice.",
    )
    bounced = models.BooleanField(
        default=False,
        help_text="Set when a bounce notification comes back for this message.",
    )
    sent_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        return f"{self.get_direction_display()} {self.subject or '(no subject)'}"

    @property
    def is_incoming(self):
        return self.direction == self.DIRECTION_INCOMING


class ClubEvent(models.Model):
    """Something on a club's calendar: meeting, swap, talk, or auction.

    The source behind the club page list, Google Calendar and Discord events. ``manual`` (typed),
    ``auction`` / ``pickup`` (generated, not hand-editable), or ``google`` (pulled). Pushes are
    idempotent via google_event_id/discord_event_id.
    """

    SOURCE_MANUAL = "manual"
    SOURCE_AUCTION = "auction"
    SOURCE_PICKUP = "pickup"
    SOURCE_GOOGLE = "google"
    SOURCE_CHOICES = (
        (SOURCE_MANUAL, "Created on this site"),
        (SOURCE_AUCTION, "From an auction"),
        (SOURCE_PICKUP, "From an auction pickup time"),
        (SOURCE_GOOGLE, "From Google Calendar"),
    )
    # Generated sources, not hand-editable.
    AUTOMATIC_SOURCES = (SOURCE_AUCTION, SOURCE_PICKUP)

    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="events")
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    location = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text="Where is it? A street address works best — it turns into a map link.",
    )
    date_start = models.DateTimeField("Starts")
    date_end = models.DateTimeField("Ends", null=True, blank=True)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    auction = models.ForeignKey(
        "Auction",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="calendar_events",
        help_text="Set for the event mirroring an auction's bidding window.",
    )
    pickup_location = models.ForeignKey(
        "PickupLocation",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="calendar_events",
        help_text="Set for events mirroring an online auction's pickup times.",
    )
    pickup_slot = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="1 for the location's pickup time, 2 for its second pickup time.",
    )
    google_event_id = models.CharField(
        max_length=1024, blank=True, help_text="Event id in the club's Google Calendar, once pushed."
    )
    discord_event_id = models.CharField(
        max_length=100, blank=True, help_text="Discord scheduled event id, once created."
    )
    needs_google_sync = models.BooleanField(
        default=True,
        help_text="Set when the event changes on our side and is waiting to be pushed to Google.",
    )
    needs_discord_sync = models.BooleanField(
        default=True,
        help_text=(
            "Set when the event changes and is waiting to reach Discord. Cleared once we've tried, "
            "success or not, so a permanent failure isn't retried every run — the next edit re-arms it."
        ),
    )
    all_day = models.BooleanField(
        default=False,
        help_text="An all-day event. date_end is then the exclusive end, the way Google and iCal write it.",
    )
    recurrence = models.TextField(
        blank=True,
        default="",
        help_text=(
            "The repeat rule from Google, one RRULE/EXDATE/RDATE line per row. One event stands "
            "for the whole series; date_start holds the occurrence that's on now, or next."
        ),
    )
    recurrence_start = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Where the series is anchored — the first occurrence. Only set for repeating events.",
    )
    title_is_custom = models.BooleanField(
        default=False,
        help_text=(
            "Set when a club admin typed this event's title by hand. Only means anything on a "
            "generated event, where it stops sync_one_auction_event overwriting it again."
        ),
    )
    description_is_custom = models.BooleanField(
        default=False,
        help_text="The same, for the description. Cleared, the auction's own blurb comes back.",
    )
    cancelled = models.BooleanField(
        default=False,
        verbose_name="This event is cancelled",
        help_text=(
            "Keeps the event visible, struck through, instead of removing it. Subscribers are told "
            "it's off rather than watching it vanish."
        ),
    )
    is_deleted = models.BooleanField(default=False)
    uuid = models.UUIDField(default=uuid_module.uuid4, unique=True, editable=False, db_index=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["date_start"]
        indexes = [
            models.Index(fields=["club", "is_deleted", "date_start"]),
        ]
        constraints = [
            # One mirrored event per auction and per pickup slot. NULLs are exempt in MariaDB unique
            # indexes, so other sources are unaffected. Not conditional constraints, which MariaDB drops (W036).
            models.UniqueConstraint(fields=["auction"], name="unique_auction_event"),
            models.UniqueConstraint(fields=["pickup_location", "pickup_slot"], name="unique_pickup_event"),
        ]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        """Auction and pickup events link to the auction; others to the club page."""
        related_auction = self.related_auction
        if related_auction:
            return related_auction.get_absolute_url()
        return reverse("club_detail", kwargs={"slug": self.club.slug}) + f"#event-{self.pk}"

    @property
    def related_auction(self):
        """The auction behind this event, bidding window or pickup."""
        if self.auction_id:
            return self.auction
        if self.pickup_location_id and self.pickup_location:
            return self.pickup_location.auction
        return None

    @property
    def is_editable(self):
        """False for a generated event: dates, location and existence belong to the auction. Wording doesn't
        (details_are_editable).
        """
        return self.source not in self.AUTOMATIC_SOURCES

    @property
    def details_are_editable(self):
        """Title and description are always editable, generated or not."""
        return True

    @property
    def is_automatic(self):
        """True when the site generates this event."""
        return self.source in self.AUTOMATIC_SOURCES

    @property
    def effective_end(self):
        """A usable end time; Google and Discord need one."""
        if self.date_end and self.date_end > self.date_start:
            return self.date_end
        return self.date_start + datetime.timedelta(hours=2)

    @property
    def when_display(self):
        """One line saying when, in the viewer's timezone. The end is shown only when informative. all_day
        events store an exclusive end, so the last day shown is date_end minus one.
        """
        from django.template.defaultfilters import date as date_filter

        start = timezone.localtime(self.date_start)
        end = timezone.localtime(self.date_end) if self.date_end else None
        if self.all_day:
            last_day = (end - datetime.timedelta(days=1)) if end else None
            if last_day and last_day.date() > start.date():
                return f"{date_filter(start, 'D, N j')} – {date_filter(last_day, 'D, N j, Y')} — all day"
            return f"{date_filter(start, 'D, N j, Y')} — all day"
        when = f"{date_filter(start, 'D, N j, Y')} at {date_filter(start, 'g:i A')}"
        if not end or end <= start:
            return when
        if end.date() == start.date():
            return f"{when} – {date_filter(end, 'g:i A')}"
        return f"{when} – {date_filter(end, 'D, N j, Y')} at {date_filter(end, 'g:i A')}"

    @property
    def is_recurring(self):
        """True for a series; one row stands for all of it (auctions/recurrence.py)."""
        return bool(self.recurrence and self.recurrence_start)

    @property
    def recurrence_lines(self):
        from auctions import recurrence

        return recurrence.from_text(self.recurrence)

    @property
    def recurrence_summary(self):
        """ "Repeats monthly on the first Tuesday", or ""."""
        from auctions import recurrence

        return recurrence.describe(self.recurrence_lines) if self.is_recurring else ""

    @property
    def occurrence_length(self):
        """How long one occurrence runs. Constant across the series."""
        return self.effective_end - self.date_start

    def next_occurrence(self, now=None):
        """When this event next happens, or None if the rule can't be read."""
        from auctions import recurrence

        if not self.is_recurring:
            return self.date_start
        return recurrence.current_or_next(
            self.recurrence_start, self.recurrence_lines, self.occurrence_length, now or timezone.now()
        )

    def refresh_occurrence(self):
        """Move a series to its current or next occurrence. True when it moved."""
        if not self.is_recurring:
            return False
        occurrence = self.next_occurrence()
        if not occurrence or occurrence == self.date_start:
            return False
        length = self.occurrence_length
        self.date_start = occurrence
        self.date_end = occurrence + length
        # Google generates the series itself; Discord holds one date.
        self.needs_discord_sync = True
        self.save(update_fields=["date_start", "date_end", "needs_discord_sync"])
        return True

    @property
    def is_over(self):
        return self.effective_end < timezone.now()

    @property
    def map_url(self):
        """Google Maps search link for the location, or ""."""
        if not self.location:
            return ""
        return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(self.location)


class ClubAnnouncement(models.Model):
    """One short message a club sent to its members, and the channels chosen at send time (so disconnecting
    a channel later doesn't rewrite history).
    """

    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="announcements")
    text = models.TextField(
        verbose_name="Announcement",
        help_text="A sentence or two — this is read on a lock screen.",
    )
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    uuid = models.UUIDField(default=uuid_module.uuid4, unique=True, editable=False, db_index=True)
    scheduled_for = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Send later",
        help_text="When it goes out. Blank on the form means a few seconds from now.",
    )
    sent_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="When it actually went out. Null means scheduled and not sent yet -- nothing may show it.",
    )

    subject = models.CharField(
        max_length=150,
        blank=True,
        default="",
        verbose_name="Email subject",
        help_text="Legacy. Subjects are now always '<Club> announcement' -- see email_subject.",
    )

    send_to_discord = models.BooleanField(default=False)
    send_to_push = models.BooleanField(default=False)
    show_on_website = models.BooleanField(default=True)
    # Two flags record which provider carried it; only one may be set (ClubAnnouncementForm.clean).
    send_to_mailchimp = models.BooleanField(default=False)
    send_to_brevo = models.BooleanField(default=False)

    discord_sent = models.BooleanField(
        default=False,
        help_text="Discord accepted the message. False with send_to_discord set means it failed.",
    )
    discord_message_id = models.CharField(max_length=100, blank=True, default="")
    push_recipients = models.PositiveIntegerField(
        default=0,
        help_text="How many members the push was handed to. Delivery, not readership — see the view table.",
    )
    mailchimp_campaign_id = models.CharField(max_length=100, blank=True, default="")
    brevo_campaign_id = models.CharField(max_length=100, blank=True, default="")
    email_opens = models.PositiveIntegerField(
        default=0,
        help_text="Unique opens reported by the email provider. The only real read receipt any channel has.",
    )
    email_error = models.CharField(
        max_length=300,
        blank=True,
        default="",
        help_text="Why the email didn't go out. Set means the campaign failed; the club needs to see it.",
    )
    website_views = models.PositiveIntegerField(
        default=0,
        help_text=(
            "How many times this was rendered on a website -- the club's own page here, or one of "
            "its embeds. An impression, not a read: it says the announcement was put in front of "
            "somebody, which is a weaker claim than the email provider's open count."
        ),
    )
    is_deleted = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["club", "is_deleted", "-created_at"]),
            # The beat task's only query: due, not sent, not deleted.
            models.Index(fields=["sent_at", "scheduled_for"]),
        ]

    def __str__(self):
        return f"{self.club}: {self.short_text}"

    @property
    def short_text(self):
        """The first line, for a table row or a push notification title."""
        first = (self.text or "").strip().splitlines()
        first = first[0] if first else ""
        return first if len(first) <= 80 else first[:77] + "…"

    @property
    def email_subject(self):
        """Always "<Club> announcement". The ``subject`` column is legacy and unread."""
        return f"{self.club.name} announcement"

    def save(self, *args, **kwargs):
        """An unscheduled announcement is stamped sent on creation, since ``sent_at`` gates everything public."""
        if self.sent_at is None and self.scheduled_for is None:
            self.sent_at = timezone.now()
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and "sent_at" not in update_fields:
                kwargs["update_fields"] = [*update_fields, "sent_at"]
        super().save(*args, **kwargs)

    @property
    def is_in_grace_period(self):
        """In the retract window rather than a chosen time, told apart by created_at vs scheduled_for."""
        if self.sent_at or not self.scheduled_for or not self.created_at:
            return False
        from auctions.announcements import GRACE_SECONDS

        return (self.scheduled_for - self.created_at).total_seconds() <= GRACE_SECONDS * 4

    @property
    def is_scheduled(self):
        """Waiting for its time; nothing public may show it."""
        return self.sent_at is None and self.scheduled_for is not None

    @property
    def sent_by_email(self):
        """Whether a provider accepted an email campaign for it."""
        return bool(self.mailchimp_campaign_id or self.brevo_campaign_id)


class HashedAPIKey(models.Model):
    """A key shown once and stored as a salted hash behind a lookup prefix, shared by ClubAPIKey and
    UserAPIKey. The raw key is ``<prefix>.<secret>``. Subclasses set key_prefix and may narrow is_usable.
    """

    # Distinct per subclass, so a stray key says what kind it is.
    key_prefix = "k_"
    # select_related when verifying.
    verify_select_related: tuple[str, ...] = ()

    class Meta:
        abstract = True

    @property
    def is_usable(self):
        """Whether this key may be used now."""
        return self.is_active

    @classmethod
    def generate(cls):
        """``(raw_key, prefix, key_hash)``. Store the last two; show the first once."""
        import secrets

        prefix = cls.key_prefix + secrets.token_hex(4)
        secret = secrets.token_hex(16)
        raw_key = f"{prefix}.{secret}"
        key_hash = make_password(secret)
        return raw_key, prefix, key_hash

    @classmethod
    def verify(cls, raw_key):
        """The key matching ``raw_key``, or ``None``. Never raises on junk."""
        try:
            prefix, secret = raw_key.split(".", 1)
        except (AttributeError, ValueError):
            return None
        candidates = cls.objects.filter(prefix=prefix, is_active=True)
        if cls.verify_select_related:
            candidates = candidates.select_related(*cls.verify_select_related)
        for candidate in candidates:
            if check_password(secret, candidate.key_hash) and candidate.is_usable:
                return candidate
        return None


class ClubAPIKey(HashedAPIKey):
    """API key scoped to one Club, for external services."""

    key_prefix = "ck_"
    verify_select_related = ("club",)

    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="api_keys")
    name = models.CharField(max_length=100, help_text="Label for this integration, e.g. 'WordPress'")
    prefix = models.CharField(max_length=12, unique=True, db_index=True)
    key_hash = models.CharField(max_length=255, db_index=True)  # salted password hash of secret
    is_active = models.BooleanField(default=True)
    can_add_club_members = models.BooleanField(default=True)
    can_read_club_member_list = models.BooleanField(default=False)
    can_update_club_members = models.BooleanField(default=False)
    can_add_bap_points = models.BooleanField(default=False)
    can_renew_memberships = models.BooleanField(
        default=False,
        help_text="Renew a membership from an external system, creating the member if they're new.",
    )
    can_look_up_species = models.BooleanField(
        default=False,
        help_text=(
            'Turns a typed name ("yellow lab") into a species from this site\'s list, add new '
            "species or attach common names to existing species.  Newly added species are only "
            "visible to your club."
        ),
    )
    can_read_auction_info = models.BooleanField(
        default=False,
        help_text="Read this club's auctions: dates, rules, fees, pickup locations and settings.",
    )
    can_read_public_lots = models.BooleanField(
        default=False,
        help_text="Read the lots in this club's auctions, without anything that names a person.",
    )
    # The privacy flag: ``private`` is absent without it.
    can_read_private_lots = models.BooleanField(
        default=False,
        help_text="Include the buyer and seller of each lot.  Don't use a key with this on a public page.",
    )
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    rate_limit = models.IntegerField(null=True, blank=True, help_text="Max requests per hour. Blank = site default.")

    def __str__(self):
        return f"{self.name} ({self.prefix}…)"


class ClubAPIKeyFieldMap(models.Model):
    """Maps an external field name to a ClubMember field for a given ClubAPIKey."""

    api_key = models.ForeignKey(ClubAPIKey, on_delete=models.CASCADE, related_name="field_mappings")
    external_field = models.CharField(max_length=100)
    internal_field = models.CharField(max_length=100)

    class Meta:
        unique_together = [("api_key", "external_field")]

    def __str__(self):
        return f"{self.external_field} → {self.internal_field}"


class UserAPIKey(HashedAPIKey):
    """A key letting an agent act as one person over /mcp/.

    ``allow_writes`` (off by default) is a ceiling, not a grant: resolvers still check the owner's
    permissions. OAuth (auctions.mcp.auth) is the other way in; this is for header-based clients and
    scripts.
    """

    key_prefix = "ak_"
    # Resolvers read the owner's UserData.
    verify_select_related = ("user__userdata",)

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_keys")
    name = models.CharField(max_length=100, help_text="What this key is for, e.g. 'Claude on my laptop'")
    prefix = models.CharField(max_length=12, unique=True, db_index=True)
    key_hash = models.CharField(max_length=255, db_index=True)  # salted password hash of secret
    is_active = models.BooleanField(default=True)
    allow_writes = models.BooleanField(
        default=False,
        help_text=(
            "Let this key add and change things — lots, check-ins, invoices, members. Off by "
            "default: a key that can only read is a much smaller thing to lose. Either way it can "
            "never do anything you couldn't do yourself."
        ),
    )
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Stop accepting this key after this date. Blank means it never expires on its own.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    rate_limit = models.IntegerField(null=True, blank=True, help_text="Max requests per hour. Blank = site default.")

    @property
    def is_usable(self):
        return self.is_active and not (self.expires_at and self.expires_at <= timezone.now())

    def __str__(self):
        return f"{self.name} ({self.prefix}…)"


class Category(models.Model):
    """Picklist of species.  Used for product, lot, and interest"""

    name = models.CharField(max_length=255)
    name_on_label = models.CharField(
        max_length=255,
        default="",
        blank=True,
        help_text="Short name printed on lot labels. Leave blank to use the full category name.",
    )
    bap_points = models.PositiveIntegerField(
        default=5,
        help_text="BAP points awarded for a sold lot in this category. Set to 0 to make this category ineligible.",
    )

    def __str__(self):
        return str(self.name)

    class Meta:
        verbose_name_plural = "Categories"
        ordering = ["name"]


def normalize_species_name(text):
    """Lowercase, strip punctuation, collapse whitespace: the key for name lookups.

    Here, not in species_matching, so stored columns and queries use the same function. Apostrophes are
    deleted ("Adolf's" = "adolfs"); hyphens become spaces.
    """
    text = re.sub(r"['‘’ʼ`]+", "", (text or "").lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text)).strip()[:120]


class Species(models.Model):
    """One species a lot can be tagged with, loaded from FishBase and the curated list.

    Genus and epithet are stored separately (FishBase provides them split); ``scientific_name`` is
    rebuilt in :meth:`save` for search and display. :class:`ClubBapGenusOverride` matches on :attr:`genus`.

    A :attr:`variety` row is a **cultivar**: it carries its parent's genus and epithet, so breeder
    points, genus rules and categories work off the nominal species, and :attr:`parent` points at it.

    An :attr:`is_hybrid` row is a **cross**: no genus, species or parent, only the trade name in
    ``variety``, shown as ``Hybrid 'Tibee'``. Stored as a flag because ``parent__isnull=True`` already
    means nominal species in several places.
    """

    SOURCE_CHOICES = (
        ("fishbase", "FishBase"),
        # SeaLifeBase is no longer imported by default (auctions/fishbase.py); rows may still exist.
        ("sealifebase", "SeaLifeBase"),
        # The curated list in auctions/data/aquarium_species.csv.
        ("aquarium", "Aquarium trade list"),
        # Added on the site. Not "manual" (old Product rows), which import_fishbase folds away.
        ("admin", "Added on the site"),
        ("manual", "Added by hand"),
    )

    #: FishBase ``Aquarium`` values meaning "an aquarium fish".
    AQUARIUM_TRADE_VALUES = ("commercial", "highly commercial", "potential")

    #: How likely anyone keeps this, in three steps, for ranking suggestions. The genus step exists
    #: because FishBase's flag is incomplete (*Chindongo saulosi* is "never/rarely", but most of its
    #: genus is flagged).
    TRADE_RANK_SPECIES = 0
    TRADE_RANK_GENUS = 1
    TRADE_RANK_NONE = 2

    common_name = models.CharField(max_length=255, blank=True, db_index=True)
    common_name.help_text = "The name usually used to describe this species"
    common_name_normalized = models.CharField(max_length=120, blank=True, db_index=True)
    common_name_normalized.help_text = (
        "common_name with the punctuation stripped, so a typed lot name can match it.  Rebuilt on "
        "save; don't edit by hand."
    )
    scientific_name = models.CharField(max_length=255, blank=True, db_index=True)
    scientific_name.help_text = "Genus and species together; filled in automatically, don't edit by hand"
    genus = models.CharField(max_length=100, blank=True, db_index=True)
    genus.help_text = "First half of the scientific name, e.g. Poecilia"
    species = models.CharField(max_length=150, blank=True, db_index=True)
    species.help_text = "Second half of the scientific name (the specific epithet), e.g. reticulata"
    variety = models.CharField(max_length=100, blank=True, db_index=True)
    variety.help_text = (
        "Cultivar, strain or morph, e.g. Blue Dream.  Leave blank for a wild-type species.  "
        "A row with this set must also have a parent."
    )
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="varieties",
        help_text="The nominal species this variety belongs to.  Only used on variety rows.",
    )
    is_hybrid = models.BooleanField(default=False)
    is_hybrid.help_text = (
        "A cross with no accepted scientific name -- a tibee shrimp, a flowerhorn.  Put the name "
        "the trade uses in variety; genus, species and parent are left empty."
    )
    breeder_points = models.BooleanField(default=True, verbose_name="Eligible for breeder points")
    breeder_points.help_text = (
        "Untick to make a lot with this species ineligible for breeder points, whatever its "
        "category.  For the things a club won't award points for breeding -- wild-caught only "
        "species, or anything that isn't really bred."
    )
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.SET_NULL)
    speccode = models.PositiveIntegerField(null=True, blank=True)
    speccode.help_text = (
        "SpecCode from the source database, used to match rows on re-import.  Blank for hand-added species."
    )
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="manual")
    # Habitat flags separate species sharing a common name.
    freshwater = models.BooleanField(default=False)
    brackish = models.BooleanField(default=False)
    saltwater = models.BooleanField(default=False)
    # Family and order as names, so curated or hand-added rows need no FamCode.
    family = models.CharField(max_length=100, blank=True, db_index=True)
    family.help_text = "Taxonomic family, e.g. Cichlidae.  Used to derive the lot category."
    order = models.CharField(max_length=100, blank=True, db_index=True)
    order.help_text = "Taxonomic order, e.g. Cichliformes."
    aquarium_use = models.CharField(max_length=30, blank=True, db_index=True)
    aquarium_use.help_text = (
        "FishBase's aquarium-trade rating.  Species in the trade are ranked above the rest when "
        "suggesting a scientific name for a lot."
    )
    in_trade_override = models.BooleanField(null=True, blank=True)
    in_trade_override.help_text = (
        "Overrules FishBase on whether this species is in the hobby.  Leave unset unless you know "
        "better than the source -- and you often will, because FishBase marks plenty of fish "
        "people obviously keep as 'never/rarely'."
    )
    trade_rank = models.PositiveSmallIntegerField(default=TRADE_RANK_NONE, db_index=True)
    trade_rank.help_text = (
        "Denormalised: 0 = in the hobby, 1 = its genus is, 2 = nothing says anyone keeps it.  "
        "Rebuilt by Species.recompute_trade_ranks(); don't edit by hand."
    )
    added_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="species_added")
    added_by.help_text = "Who added this on the site.  Blank for everything the importers loaded."
    club = models.ForeignKey("Club", null=True, blank=True, on_delete=models.SET_NULL, related_name="species_added")
    club.help_text = (
        "The club this was added for, when there was an obvious one.  While the species is "
        "unapproved this is the second way it can be seen: the person who added it always can, "
        "and so can anyone else at the same club.  Often blank -- plenty of auctions have no club "
        "attached at all -- which is why it can never be the *only* way in."
    )
    approved = models.BooleanField(default=True, db_index=True)
    approved.help_text = (
        "Untick to keep this species out of everyone's suggestions except the person who added "
        "it.  That is how an auction admin gets a missing species onto their own lots at the "
        "check-in table without adding it to the whole site; ticking it here is the approval."
    )
    possible_duplicate = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text=(
            "Another row that looks like this one -- same scientific name, or the same common "
            "name.  Set automatically on save; the pair is listed on the species gaps page for a "
            "site admin to merge or dismiss."
        ),
    )

    #: Sources that number their own species; duplicates are impossible there, so the scan is skipped.
    IMPORTED_SOURCES = ("fishbase", "sealifebase")

    def find_possible_duplicate(self):
        """Another row that is probably this species, or None.

        The same scientific name at the same rank (variety included), or the same designated
        ``common_name`` (not synonyms, which FishBase shares on purpose). Never a variety against a plain
        species, never within imported lists. Hybrids compare on strain name.
        """
        others = Species.objects.exclude(pk=self.pk)
        if self.is_hybrid:
            match = others.filter(is_hybrid=True, variety__iexact=self.variety).first()
            if match:
                return match
        elif self.scientific_name:
            match = others.filter(scientific_name__iexact=self.scientific_name, variety__iexact=self.variety).first()
            if match:
                return match
        if self.common_name_normalized:
            return others.filter(common_name_normalized=self.common_name_normalized).first()
        return None

    def flag_possible_duplicate(self):
        """Point this row and its lookalike at each other, or clear a stale flag. ``update()``, since this runs
        from ``save()``.
        """
        duplicate = self.find_possible_duplicate()
        if duplicate:
            Species.objects.filter(pk=self.pk).update(possible_duplicate=duplicate.pk)
            Species.objects.filter(pk=duplicate.pk).update(possible_duplicate=self.pk)
            # Keep the in-memory row in step with update().
            self.possible_duplicate = duplicate
        elif self.possible_duplicate_id:
            # The id, not the object: the target may have been merged away.
            Species.objects.filter(pk=self.possible_duplicate_id).update(possible_duplicate=None)
            Species.objects.filter(pk=self.pk).update(possible_duplicate=None)
            self.possible_duplicate = None

    def merge_duplicate(self, duplicate):
        """Fold *duplicate* into this row and delete it; returns what moved. A site admin's decision.

        Lots, strains and common names move rather than cascade; the losing row's names are usually the
        point of merging.
        """
        if duplicate.pk == self.pk:
            return {}
        moved = {
            "lots": Lot.objects.filter(species=duplicate).update(species=self),
            "varieties": Species.objects.filter(parent=duplicate).update(parent=self),
        }
        # Skip names already carried.
        have = set(self.common_names.values_list("name_normalized", flat=True)) | {self.common_name_normalized}
        keep = [name for name in duplicate.common_names.all() if name.name_normalized not in have]
        SpeciesCommonName.objects.filter(pk__in=[name.pk for name in keep]).update(species=self)
        duplicate.common_names.all().delete()
        moved["common_names"] = len(keep)
        # The losing designated name becomes a synonym.
        if duplicate.common_name and duplicate.common_name_normalized not in have:
            SpeciesCommonName.objects.create(
                species=self,
                name=duplicate.common_name[:255],
                source=duplicate.source if duplicate.source in dict(self.SOURCE_CHOICES) else "manual",
                approved=self.approved,
            )
            moved["common_names"] += 1
        # Cache rows follow the species, minus collisions (search_text is unique).
        kept_texts = set(SpeciesSearchCache.objects.filter(species=self).values_list("search_text", flat=True))
        SpeciesSearchCache.objects.filter(species=duplicate, search_text__in=kept_texts).delete()
        moved["remembered_names"] = SpeciesSearchCache.objects.filter(species=duplicate).update(species=self)
        rejected_texts = set(SpeciesNameRejection.objects.filter(species=self).values_list("search_text", flat=True))
        SpeciesNameRejection.objects.filter(species=duplicate, search_text__in=rejected_texts).delete()
        SpeciesNameRejection.objects.filter(species=duplicate).update(species=self)
        # Nothing points at either row as a duplicate any more.
        Species.objects.filter(Q(possible_duplicate=duplicate) | Q(possible_duplicate=self)).update(
            possible_duplicate=None
        )
        self.possible_duplicate = None
        duplicate.delete()
        return moved

    def save(self, *args, **kwargs):
        self.genus = (self.genus or "").strip()
        self.species = (self.species or "").strip()
        self.variety = (self.variety or "").strip()
        # A hybrid is only a name; a leftover genus would feed genus rules and token matching.
        if self.is_hybrid:
            self.genus = ""
            self.species = ""
            self.parent = None
        # Legacy rows have a scientific name and no genus: split it rather than rebuild from blanks.
        if self.scientific_name and not self.genus and not self.species and not self.is_hybrid:
            parts = self.scientific_name.split()
            self.genus = parts[0][:100]
            self.species = " ".join(parts[1:])[:150]
        self.scientific_name = " ".join(part for part in (self.genus, self.species) if part)
        self.common_name_normalized = normalize_species_name(self.common_name)
        # Keep the species tier honest; the genus tier needs recompute_trade_ranks().
        if self.in_aquarium_trade:
            self.trade_rank = self.TRADE_RANK_SPECIES
        elif self.trade_rank == self.TRADE_RANK_SPECIES:
            self.trade_rank = self.TRADE_RANK_NONE
        super().save(*args, **kwargs)
        # Duplicate check for everything except the two big imports.
        if self.source not in self.IMPORTED_SOURCES:
            self.flag_possible_duplicate()

    @property
    def in_aquarium_trade(self):
        """True when something says this is kept in aquariums: an admin override, then the curated list, then
        FishBase.
        """
        if self.in_trade_override is not None:
            return self.in_trade_override
        return self.source == "aquarium" or self.aquarium_use in self.AQUARIUM_TRADE_VALUES

    @classmethod
    def recompute_trade_ranks(cls, genus=None, batch_size=2000):
        """Rebuild the denormalised :attr:`trade_rank`. Returns rows changed. Denormalised because suggestions
        order by it before a LIMIT. Pass *genus* to redo one genus.
        """
        traded = cls.objects.filter(in_trade_override=True) | cls.objects.filter(
            in_trade_override__isnull=True, aquarium_use__in=cls.AQUARIUM_TRADE_VALUES
        )
        traded = traded | cls.objects.filter(in_trade_override__isnull=True, source="aquarium")
        if genus:
            traded = traded.filter(genus=genus)
        # order_by() clears Meta.ordering, which would make DISTINCT per species. discard(""): hybrids
        # have no genus and would promote every blank-genus row.
        traded_genera = set(traded.order_by().values_list("genus", flat=True).distinct())
        traded_genera.discard("")

        queryset = cls.objects.all() if genus is None else cls.objects.filter(genus=genus)
        changed = 0
        batch = []
        for species in queryset.iterator(chunk_size=batch_size):
            if species.in_aquarium_trade:
                rank = cls.TRADE_RANK_SPECIES
            elif species.genus and species.genus in traded_genera:
                rank = cls.TRADE_RANK_GENUS
            else:
                rank = cls.TRADE_RANK_NONE
            if species.trade_rank != rank:
                species.trade_rank = rank
                batch.append(species)
            if len(batch) >= batch_size:
                cls.objects.bulk_update(batch, ["trade_rank"])
                changed += len(batch)
                batch = []
        if batch:
            cls.objects.bulk_update(batch, ["trade_rank"])
            changed += len(batch)
        return changed

    @property
    def earns_breeder_points(self):
        """False when lots of this species can't earn breeder points. A cultivar also answers for its parent."""
        if not self.breeder_points:
            return False
        if self.parent_id and not self.parent.breeder_points:
            return False
        return True

    @property
    def full_scientific_name(self):
        """The scientific name, with the cultivar in quotes (*Neocaridina davidi* 'Blue Dream'), or
        ``Hybrid 'Tibee'`` for a cross. What labels and pages print.
        """
        if self.is_hybrid and self.variety:
            return f"Hybrid '{self.variety}'"
        if self.variety and self.scientific_name:
            return f"{self.scientific_name} '{self.variety}'"
        return self.scientific_name or self.variety

    @property
    def label(self):
        """What users pick from: the scientific name only. Common names still drive matching; the language
        model gets :attr:`label_with_common_name`.
        """
        return self.full_scientific_name or self.common_name

    @property
    def label_with_common_name(self):
        """Scientific name with the common name in brackets, for the language model, which matches against it."""
        name = self.full_scientific_name
        if name and self.common_name:
            return f"{name} ({self.common_name})"
        return name or self.common_name

    def __str__(self):
        return self.label

    class Meta:
        verbose_name_plural = "Species"
        ordering = ["scientific_name", "variety"]
        # SpecCode is unique only per source database.
        unique_together = ("source", "speccode")


class SpeciesCommonName(models.Model):
    """A common name pointing at a :class:`Species`, from FishBase, the curated CSV, or people.

    :attr:`source` lets each writer delete only its own names, so hobby names ("yellow lab") survive
    re-imports without cloning species.
    """

    #: Same vocabulary as Species.source, so importers clear only what they wrote.
    SOURCE_CHOICES = Species.SOURCE_CHOICES

    species = models.ForeignKey(Species, on_delete=models.CASCADE, related_name="common_names")
    name = models.CharField(max_length=255, db_index=True)
    name.help_text = "As the source spells it, punctuation and all.  This is what a person reads."
    name_normalized = models.CharField(max_length=120, blank=True, db_index=True)
    name_normalized.help_text = (
        "What lookups match on: name with the punctuation stripped, by normalize_species_name.  "
        "Rebuilt on save; the importers fill it in on the bulk paths, which skip save()."
    )
    language = models.CharField(max_length=50, blank=True, default="English")
    is_preferred = models.BooleanField(default=False)
    is_preferred.help_text = "FishBase's primary name for this species in this language.  Ranked first."
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="manual", db_index=True)
    source.help_text = (
        "Which list wrote this name.  An importer only ever deletes its own, so a name added here "
        "or in aquarium_species.csv survives the next re-import of FishBase."
    )
    # Scoped like Species: a name is read before everything else, so one club's name for the wrong
    # fish can't become everyone's. Default True for importer rows.
    approved = models.BooleanField(default=True, db_index=True)
    approved.help_text = "Off means only the person or club that added it is offered it.  Everything imported is on."
    added_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="species_names_added"
    )
    added_by.help_text = "Who added this on the site.  Blank for everything the importers loaded."
    club = models.ForeignKey(
        "Club", null=True, blank=True, on_delete=models.SET_NULL, related_name="species_names_added"
    )
    club.help_text = "The club this was added for, when there was an obvious one.  See Species.club."

    def save(self, *args, **kwargs):
        self.name_normalized = normalize_species_name(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ["name"]
        # Lookups use the normalized one; the admin searches the name as written.
        indexes = [
            models.Index(fields=["name", "is_preferred"]),
            models.Index(fields=["name_normalized", "is_preferred"]),
        ]


class SpeciesSearchCache(models.Model):
    """A remembered answer to "what species is a lot called *this*?", so the model isn't asked again.

    ``species`` null means "not a species". ``scientific_name`` with no species is a gap: identified,
    but not on our list, and re-resolved once imported (:attr:`is_a_gap`).
    """

    SOURCE_CHOICES = (
        ("llm", "Language model"),
        ("user", "Chosen by a user"),
    )

    search_text = models.CharField(max_length=120, unique=True)
    search_text.help_text = "Normalised lot name: lowercased, punctuation stripped."
    species = models.ForeignKey(Species, null=True, blank=True, on_delete=models.CASCADE)
    scientific_name = models.CharField(max_length=120, blank=True, default="")
    scientific_name.help_text = (
        "What this lot name was identified as, whether or not the species list holds it.  A row "
        "with no species and a name filled in here is a gap in the list rather than a verdict "
        "about the name -- see SpeciesSearchCache.is_a_gap."
    )
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="llm")
    created_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="species_names_taught"
    )
    created_by.help_text = (
        "Who taught the site this answer, when a person did.  Blank for the language model.  "
        "Every row here is served to every club, so a wrong one needs to be traceable."
    )
    createdon = models.DateTimeField(auto_now_add=True)
    hits = models.PositiveIntegerField(default=0)
    hits.help_text = "How many times this cached answer has been served instead of asking again."
    # What people did with the answer: the only evidence it is right.
    accepts = models.PositiveIntegerField(default=0)
    accepts.help_text = (
        "Lots saved with this answer left alone.  Counted once per lot, on the save that created "
        "it -- re-saving a lot without touching the species is not new evidence."
    )
    rejects = models.PositiveIntegerField(default=0)
    rejects.help_text = (
        "Lots this answer was cleared from or changed on.  Counted once per lot, like accepts.  "
        "Enough of them retires the row; see is_discredited."
    )

    #: Rejections allowed: one in ten.
    MAX_REJECT_RATIO = 0.1

    #: ...and at least this many, so one stray clear doesn't retire an answer.
    MIN_REJECTS_TO_RETIRE = 3

    @property
    def is_a_gap(self):
        """True when the lot was identified but the list lacks the species."""
        return self.species_id is None and bool(self.scientific_name)

    @property
    def is_discredited(self):
        """True when rejections exceed :attr:`MAX_REJECT_RATIO` and :attr:`MIN_REJECTS_TO_RETIRE`
        (``9 * rejects > accepts``, integer arithmetic, read on every lot save).
        """
        return self.rejects >= self.MIN_REJECTS_TO_RETIRE and self.rejects * 9 > self.accepts

    def retire(self):
        """Discard this answer and record a :class:`SpeciesNameRejection` for the pair, so the model can't write
        it straight back. The name is left unanswered, not "not a species".
        """
        if self.species_id:
            SpeciesNameRejection.objects.get_or_create(search_text=self.search_text, species_id=self.species_id)
        self.delete()

    def __str__(self):
        return f"{self.search_text} -> {self.species or 'no species'}"


class SpeciesNameRejection(models.Model):
    """ "This lot name is **not** that species": keeps a retired answer from coming back.

    Vetoes a pair only, read only by the cache and the model shortlist, never by exact or token matching
    (so people clearing fields can't outvote the list). Deletable on the gaps page.
    """

    search_text = models.CharField(max_length=120, db_index=True)
    search_text.help_text = "Normalised lot name, the same key SpeciesSearchCache uses."
    species = models.ForeignKey(Species, on_delete=models.CASCADE, related_name="name_rejections")
    createdon = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.search_text} is not {self.species}"

    class Meta:
        unique_together = ("search_text", "species")


def _slugify_auction_title(value):
    """Slugify an auction title, removing ``-auctions``/``-contact`` suffixes that collide with club email
    routing aliases.
    """
    from django.utils.text import slugify

    slug = slugify(value)
    for suffix in ("-auctions", "-contact"):
        if slug.endswith(suffix):
            slug = slug[: -len(suffix)].rstrip("-")
    return slug or slugify(value)  # # fall back to the unsanitised slug


class Auction(CachedPropertiesMixin, models.Model):
    """An auction is a collection of lots"""

    title = models.CharField("Auction name", max_length=255, blank=False, null=False)
    title.help_text = "This is the name people will see when joining your auction"
    slug = AutoSlugField(populate_from="title", unique=True, slugify=_slugify_auction_title)
    is_online = models.BooleanField(default=True)
    is_online.help_text = "Is this is an online auction with in-person pickup at one or more locations?"
    sealed_bid = models.BooleanField(default=False)
    sealed_bid.help_text = "Users won't be able to see what the current bid is"
    lot_entry_fee = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(10)])
    lot_entry_fee.help_text = "The amount the seller will be charged if a lot sells"
    registration_fee = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)])
    registration_fee.help_text = "Added to all invoices"
    unsold_lot_fee = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(10)])
    unsold_lot_fee.help_text = "The amount the seller will be charged if their lot doesn't sell"
    winning_bid_percent_to_club = models.PositiveIntegerField(
        default=0, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    winning_bid_percent_to_club.help_text = (
        "In addition to the Lot entry fee, this percent of the winning price will be taken by the club"
    )
    pre_register_lot_discount_percent = models.PositiveIntegerField(
        default=0, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    pre_register_lot_discount_percent.help_text = "Decrease the club cut if users add lots through this website"
    pre_register_lot_entry_fee_discount = models.PositiveIntegerField(
        default=0, validators=[MinValueValidator(0), MaxValueValidator(10)]
    )
    pre_register_lot_entry_fee_discount.help_text = (
        "Decrease the lot entry fee by this amount if users add lots through this website"
    )
    force_donation_threshold = models.PositiveIntegerField(
        default=None,
        blank=True,
        null=True,
        validators=[MinValueValidator(0), MaxValueValidator(10)],
        verbose_name="Donation threshold",
    )
    force_donation_threshold.help_text = (
        "Most auctions should leave this blank.  Force lots to be a donation if they sell for this amount or less."
    )
    date_posted = models.DateTimeField(auto_now_add=True)
    date_start = models.DateTimeField("Auction start date")
    date_start.help_text = "Bidding starts on this date"
    lot_submission_start_date = models.DateTimeField("Lot submission opens", null=True, blank=True)
    lot_submission_start_date.help_text = "Users can submit (but not bid on) lots on this date"
    lot_submission_end_date = models.DateTimeField("Lot submission ends", null=True, blank=True)
    date_end = models.DateTimeField("Bidding end date", blank=True, null=True)
    date_end.help_text = "Bidding will end on this date.  If last-minute bids are placed, bidding can go up to 1 hour past this time on those lots."
    date_online_bidding_starts = models.DateTimeField("Online bidding opens", blank=True, null=True)
    date_online_bidding_ends = models.DateTimeField("Online bidding ends", blank=True, null=True)
    watch_warning_email_sent = models.BooleanField(default=False)
    first_discord_sent = models.BooleanField(default=False)
    second_discord_sent = models.BooleanField(default=False)
    discord_event_created = models.BooleanField(default=False)
    discord_event_id = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text=(
            "Discord scheduled event id for this auction, once auction_emails has created one. "
            "Kept so the event can be moved or called off when the auction is."
        ),
    )
    discord_event_needs_update = models.BooleanField(
        default=False,
        help_text="Set when something Discord shows about this auction changed and hasn't been sent yet.",
    )
    invoiced = models.BooleanField(default=False)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    club = models.ForeignKey("Club", null=True, blank=True, on_delete=models.SET_NULL, related_name="auctions")
    add_membership_fee_to_invoices_for_expired_members = models.BooleanField(
        default=False,
        help_text="And create membership if they don't have one.  You can turn this off on each invoice.",
    )
    MANAGE_USERS_CHOICES = [
        ("", "Off"),
        ("all", "Automatically add all club members"),
        ("checkin", "Automatically add, but require check-in"),
    ]
    manage_users_through_club = models.CharField(
        max_length=20,
        choices=MANAGE_USERS_CHOICES,
        default="",
        blank=True,
        help_text=(
            "Manage participants as members of the associated club. "
            "Requires an associated club and an empty auction (no lots, no invoices). "
            "Changing this deletes existing per-auction participant records."
        ),
    )
    allow_self_checkin = models.BooleanField(
        default=True,
        blank=True,
        verbose_name="Allow users to self-check in with the app",
        help_text="Uncheck if you need to assign bidder numbers",
    )
    location = models.CharField(max_length=300, null=True, blank=True)
    location.help_text = "State or region of this auction"
    summernote_description = models.TextField(verbose_name="Rules", default="", blank=True)
    lot_promotion_cost = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    first_bid_payout = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)])
    first_bid_payout.help_text = "This is a feature to encourage bidding.  Give each bidder this amount, for free.  <a href='/blog/encouraging-participation/' target='_blank'>More information</a>"
    club_member_discount = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)])
    club_member_discount.help_text = "Automatically add a discount in this amount if a paid club member has purchased at least one lot in this auction"
    # Off by default: public listing is a decision, checked by AuctionEditForm.clean.
    promote_this_auction = models.BooleanField(default=False)
    promote_this_auction.help_text = "Show this to everyone in the list of auctions"
    is_chat_allowed = models.BooleanField(default=True)
    max_lots_per_user = models.PositiveIntegerField(null=True, blank=True, validators=[MaxValueValidator(100)])
    max_lots_per_user.help_text = "A user won't be able to add more than this many lots to this auction"
    allow_additional_lots_as_donation = models.BooleanField(default=True)
    allow_additional_lots_as_donation.help_text = "If you don't set max lots per user, this has no effect"
    # New email tracking fields
    welcome_email_sent = models.BooleanField(default=False)
    welcome_email_due = models.DateTimeField(blank=True, null=True)
    invoice_email_sent = models.BooleanField(default=False)
    invoice_email_due = models.DateTimeField(blank=True, null=True)
    followup_email_sent = models.BooleanField(default=False)
    followup_email_due = models.DateTimeField(blank=True, null=True)
    reprint_reminder_sent = models.BooleanField(default=False)
    weekly_promo_emails_sent = models.PositiveIntegerField(default=0)
    weekly_promo_emails_sent.help_text = "Number of times this auction was included in weekly promotional emails"
    promo_push_notifications_sent = models.PositiveIntegerField(default=0)
    promo_push_notifications_sent.help_text = "Number of push notifications sent promoting this auction"
    make_stats_public = models.BooleanField(default=True)
    make_stats_public.help_text = "Allow any user who has a link to this auction's stats to see them.  Uncheck to only allow the auction creator to view stats"
    bump_cost = models.PositiveIntegerField(blank=True, default=1, validators=[MinValueValidator(1)])
    bump_cost.help_text = "The amount a user will be charged each time they move a lot to the top of the list"
    use_categories = models.BooleanField(default=True, verbose_name="Use category field")
    use_categories.help_text = (
        "Not shown on the bulk add lots form.  Check to use categories like Cichlids, Livebearers, etc."
    )
    is_deleted = models.BooleanField(default=False)
    ONLINE_BIDDING_OPTIONS = (
        ("allow", "Allow buy now and bidding"),
        ("buy_now_only", "Allow buy now only"),
        ("disable", "No online bidding"),
    )
    online_bidding = models.CharField(max_length=20, choices=ONLINE_BIDDING_OPTIONS, blank=False, default="allow")
    only_approved_sellers = models.BooleanField(default=False)
    only_approved_sellers.help_text = "Require admin approval before users can add lots.  This will not change permissions for users that have already joined."
    only_approved_bidders = models.BooleanField(default=False)
    only_approved_bidders.help_text = "Require admin approval before users can bid.  This only applies to new users: Users that you manually add and users who have a paid invoice in a past auctions will be allowed to bid."
    require_phone_number = models.BooleanField(default=False)
    require_phone_number.help_text = "Require users to have entered a phone number before they can join this auction"
    exact_location_set = models.BooleanField(default=False)
    exact_location_set.help_text = "The location was pinned from a phone at the venue (or confirmed exact)."
    email_users_when_invoices_ready = models.BooleanField(default=True)
    invoice_payment_instructions = models.CharField(max_length=255, blank=True, null=True, default="")
    invoice_payment_instructions.help_text = "Shown to the user on their invoice.  For example, 'You will receive a seperate PayPal invoice with payment instructions'"
    invoice_rounding = models.BooleanField(default=True)
    invoice_rounding.help_text = (
        "Round invoice totals to whole dollar amounts.  Check if you plan to accept cash payments."
    )
    only_whole_dollar_bids = models.BooleanField(default=True)
    only_whole_dollar_bids.help_text = "Require bids, minimum bids, and lot prices to be whole dollar amounts.  Uncheck to allow bids with cents (e.g. $5.50)."
    minimum_bid = models.DecimalField(
        default=2, max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    minimum_bid.help_text = "Lowest price any lot will be sold for"
    lot_entry_fee_for_club_members = models.PositiveIntegerField(
        default=0, validators=[MinValueValidator(0), MaxValueValidator(10)]
    )
    lot_entry_fee_for_club_members.verbose_name = "Alternate lot entry fee"
    lot_entry_fee_for_club_members.help_text = (
        "Used instead of the standard entry fee, when you mark someone as using alternative fees"
    )
    winning_bid_percent_to_club_for_club_members = models.PositiveIntegerField(
        default=0, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    winning_bid_percent_to_club_for_club_members.verbose_name = "Alternate winning bid percent to club"
    winning_bid_percent_to_club_for_club_members.help_text = (
        "Used instead of the standard split, when you mark someone as using alternative fees"
    )
    registration_fee_for_club_members = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)])
    registration_fee_for_club_members.verbose_name = "Alternate registration fee"
    registration_fee_for_club_members.help_text = (
        "Used instead of the registration fee, when you mark someone as using alternative fees"
    )
    ALTERNATE_SPLIT_MODE_CHOICES = (
        ("off", "Off"),
        ("club_member", "Club member discount"),
        ("custom", "Custom"),
    )
    alternate_split_mode = models.CharField(
        max_length=20,
        choices=ALTERNATE_SPLIT_MODE_CHOICES,
        blank=False,
        default="custom",
        verbose_name="Alternate split",
    )
    alternate_split_mode.help_text = "Charge some sellers different fees.  Club member discount automatically applies the alternate fees to paid club members; custom lets you mark users yourself."
    alternative_split_label = models.CharField(
        max_length=50, default="Alternate fees", blank=False, verbose_name="Alternate split label"
    )
    alternative_split_label.help_text = (
        "Label used for people getting alternate fees.  For example, club member, vendor, etc."
    )
    SET_LOT_WINNER_URLS = (
        ("", "Standard, bidder number/lot number only"),
        ("presentation", "Show a picture of the lot"),
        ("autocomplete", "Autocomplete, search by name or bidder number"),
    )
    set_lot_winners_url = models.CharField(
        max_length=20, choices=SET_LOT_WINNER_URLS, blank=True, default="presentation"
    )
    set_lot_winners_url.verbose_name = "Set lot winners"

    BUY_NOW_CHOICES = (
        ("disable", "Don't allow"),
        ("allow", "Allow"),
        ("required", "Required for all lots"),
    )
    buy_now = models.CharField(max_length=20, choices=BUY_NOW_CHOICES, default="allow")
    buy_now.help_text = "Allow lots to be sold without bidding, for a user-specified price."
    RESERVE_CHOICES = (
        ("disable", "Don't allow"),
        ("allow", "Allow"),
        ("required", "Required for all lots"),
    )
    reserve_price = models.CharField(
        max_length=20,
        choices=RESERVE_CHOICES,
        default="allow",
        verbose_name="Seller set minimum bid",
    )
    reserve_price.help_text = "Allow users to set a minimum bid on their lots"
    tax = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)])
    tax.help_text = "A percent added to the buyer's invoice for all won lots (e.g. enter 7 for 7% sales tax). Leave at 0 for no tax."
    advanced_lot_adding = models.BooleanField(default=False)
    advanced_lot_adding.help_text = "Show lot number, quantity and description fields when bulk adding lots"
    use_quantity_field = models.BooleanField(default=False, blank=True)
    custom_checkbox_name = models.CharField(
        max_length=50, default="", blank=True, null=True, verbose_name="Custom checkbox name"
    )
    custom_checkbox_name.help_text = "Shown when users add lots"
    use_reference_link = models.BooleanField(default=True, blank=True)
    use_reference_link.help_text = "Not shown on the bulk add lots form.  Especially handy for videos."
    use_description = models.BooleanField(default=True, blank=True)
    use_description.help_text = "Not shown on the bulk add lots form"
    use_donation_field = models.BooleanField(default=True, blank=True)
    use_i_bred_this_fish_field = models.BooleanField(default=True, blank=True, verbose_name="Use Breeder Points field")
    use_scientific_name = models.BooleanField(default=True, blank=True, verbose_name="Use scientific name field")
    use_scientific_name.help_text = "Suggest a species based on the lot name.  Users pick from a short list, or No species for hardware and other non-living lots."
    use_custom_checkbox_field = models.BooleanField(default=False, blank=True)
    use_custom_checkbox_field.help_text = "Optional information such as CARES, native species, difficult to keep, etc."
    custom_dropdown_name = models.CharField(
        max_length=50, default="", blank=True, null=True, verbose_name="Custom dropdown name"
    )
    custom_dropdown_name.help_text = "Shown when users add lots"
    CUSTOM_DROPDOWN_CHOICES = (
        ("disable", "Off"),
        ("allow", "Optional"),
        ("required", "Required for all lots"),
    )
    use_custom_dropdown_field = models.CharField(max_length=20, choices=CUSTOM_DROPDOWN_CHOICES, default="disable")
    use_custom_dropdown_field.help_text = "Dropdown shown when users add lots."
    CUSTOM_CHOICES = (
        ("disable", "Off"),
        ("allow", "Optional"),
        ("required", "Required for all lots"),
    )
    custom_field_1 = models.CharField(
        max_length=20,
        choices=CUSTOM_CHOICES,
        default="disable",
        verbose_name="Custom text field",
    )
    custom_field_1.help_text = (
        "Additional information on the label such as notes, scientific name, collection location..."
    )
    custom_field_1_name = models.CharField(
        max_length=50, default="Notes", blank=True, null=True, verbose_name="Custom text field name"
    )
    custom_field_1_name.help_text = "What's the custom field used for?  This is shown to users"
    allow_bulk_adding_lots = models.BooleanField(default=True)
    allow_bulk_adding_lots.help_text = "Uncheck to force users to add lots one at a time.  Turning this off encourage more detail and pictures about each lot, but makes adding lots take longer. Admins can always bulk add lots for other users."
    copy_users_when_copying_this_auction = models.BooleanField(default=False)
    copy_users_when_copying_this_auction.help_text = "Save yourself a few clicks when bulk importing users"
    extra_promo_text = models.CharField(max_length=50, default="", blank=True, null=True)
    extra_promo_link = models.URLField(blank=True, null=True)
    allow_deleting_bids = models.BooleanField(default=False, blank=True)
    allow_deleting_bids.help_text = "Allow users to delete their own bids until the auction ends"
    auto_add_images = models.BooleanField("Automatically add images to lots", default=True, blank=True)
    auto_add_images.help_text = (
        "Images taken from older lots with the same name in any auctions created by you or other admins"
    )
    message_users_when_lots_sell = models.BooleanField(
        default=True, blank=True, verbose_name="Allow push notifications for watched lots"
    )
    message_users_when_lots_sell.help_text = "Recommended if you are recording winners as lots sell.  When you enter a lot number on the set lot winners screen, send a notification to any users watching that lot"
    label_print_fields = models.CharField(
        max_length=1000,
        blank=True,
        null=True,
        default="qr_code,lot_name,scientific_name,min_bid_label,buy_now_label,quantity_label,seller_name,donation_label,custom_field_1,i_bred_this_fish_label,custom_checkbox_label,custom_dropdown_label",
    )
    use_seller_dash_lot_numbering = models.BooleanField(default=False, blank=True)
    use_seller_dash_lot_numbering.help_text = "Include the seller's bidder number with the lot number.  This option is not recommended as users find it confusing."
    paypal_email_address = models.EmailField(max_length=255, blank=True, null=True)
    paypal_email_address.help_text = "Not currently used, this is configured in the model PayPalSeller"
    enable_online_payments = models.BooleanField(default=False, blank=True, verbose_name="PayPal payments")
    enable_online_payments.help_text = "Allow users to use PayPal to pay their invoices themselves."
    dismissed_paypal_banner = models.BooleanField(default=False, blank=True)
    square_email_address = models.EmailField(max_length=255, blank=True, null=True)
    square_email_address.help_text = "Not currently used, this is configured in the model SquareSeller"
    enable_square_payments = models.BooleanField(default=False, blank=True, verbose_name="Square payments")
    enable_square_payments.help_text = "Allow users to use Square to pay their invoices themselves."
    dismissed_square_banner = models.BooleanField(default=False, blank=True)
    dismissed_promo_banner = models.BooleanField(default=False, blank=True)
    dismissed_customize_event_banner = models.BooleanField(
        default=False,
        blank=True,
        help_text=(
            "Set when an admin dismissed the prompt to write this auction's own calendar wording. "
            "Deliberately not in AUCTION_FIELDS_TO_CLONE: next year's auction gets a new event and "
            "the same generated sentence, which is the thing worth asking about again."
        ),
    )
    google_drive_link = models.URLField(max_length=500, blank=True, null=True, default="")
    google_drive_link.help_text = "Link to a Google Sheet with user information.  Make sure the sheet is shared with 'anyone with the link can view'."
    last_sync_time = models.DateTimeField(blank=True, null=True)
    last_sync_time.help_text = "Last time user data was synchronized from Google Drive"
    cached_stats = models.JSONField(blank=True, null=True, default=None)
    cached_stats.help_text = "Cached auction statistics data to avoid recalculating on every page load"
    last_stats_update = models.DateTimeField(blank=True, null=True)
    last_stats_update.help_text = "Timestamp of when auction statistics were last calculated"
    next_update_due = models.DateTimeField(blank=True, null=True, default=timezone.now)
    next_update_due.help_text = "Timestamp for when the next statistics update should be run"

    @property
    def promotion_request_mailto_query(self):
        """Pre-encoded subject/body for the 'request promoted auction access' mailto link."""
        domain = getattr(settings, "SITE_DOMAIN", "").strip()
        if domain:
            if not domain.startswith("http"):
                base = f"https://{domain}"
            else:
                base = domain.rstrip("/")
        else:
            base = ""
        absolute_url = f"{base}{self.get_absolute_url()}"
        admin_email = settings.ADMINS[0][1]
        subject = "Request access to create promoted auctions"
        body = (
            "Hello, I would like to request permission to create promoted auctions.\n\n"
            "My club's website/Facebook page is:\n\n"
            f"I've created a test auction here: {absolute_url}\n\n"
            "Thank you!"
        )
        return f"{admin_email}?subject={quote_plus(subject)}&body={quote_plus(body)}"

    @property
    def untrusted_message(self):
        """If this auction is marked as untrusted, return the message to show users"""
        return settings.UNTRUSTED_MESSAGE

    @cached_property
    def effective_paypal_seller(self):
        """The PayPalSeller for this auction: the club's route for club auctions, else the creator's. None if
        unconfigured.
        """
        if self.club:
            return self.club.effective_paypal_seller
        if self.created_by_id:
            return PayPalSeller.objects.filter(user=self.created_by).first()
        return None

    @cached_property
    def effective_square_seller(self):
        """The SquareSeller for this auction: the club's if connected (so club money never lands in a personal
        account), else the creator's. No site fallback.
        """
        from auctions.models import SquareSeller

        if self.club:
            club_seller = self.club.effective_square_seller
            if club_seller:
                return club_seller
        if self.created_by_id:
            return SquareSeller.objects.filter(user=self.created_by).first()
        return None

    @property
    def paypal_information(self):
        """The PayPal merchant id: the club or creator's seller, ``"admin"`` for the site account, or ``None``
        when a club uses its own credentials.
        """
        if self.club:
            if self.club.uses_own_paypal_credentials:
                return None
            if self.club.uses_site_paypal:
                return "admin"
            seller = self.club.effective_paypal_seller
            return seller.paypal_merchant_id if seller else None
        seller = self.effective_paypal_seller
        if seller:
            return seller.paypal_merchant_id
        if self.created_by and self.created_by.is_superuser:
            return "admin"
        return None

    @property
    def offers_tap_to_pay(self):
        """True when this auction's Square account can take in-person payments (connected and has the Tap to Pay
        scope). Read by auction_ribbon.html to request Apple's awareness modal.
        """
        seller = self.effective_square_seller
        return bool(seller and seller.square_merchant_id and seller.supports_tap_to_pay)

    @property
    def square_information(self):
        """The Square merchant id of the effective seller, if linked. No site Square."""
        seller = self.effective_square_seller
        return seller.square_merchant_id if seller else None

    @cached_property
    def show_paypal_banner(self):
        """Whether to offer the creator the connect-PayPal banner. Hidden for club auctions; the template also
        checks the viewer is the creator.
        """
        if self.club:
            return False
        if self.dismissed_paypal_banner:
            return False
        if not self.created_by.userdata.paypal_enabled:
            return False
        if self.created_by.is_superuser:
            return False
        if self.created_by.userdata.never_show_paypal_connect:
            return False
        if PayPalSeller.objects.filter(user=self.created_by).first():
            return False
        return True

    @cached_property
    def show_square_banner(self):
        """Whether to offer the creator the connect-Square banner. Hidden for club auctions.

        Doesn't check ``square_enabled``: that picks which half the template shows (connect button, or a
        request-access link), so organizers always learn card payments exist. ``SquareConnectView`` enforces
        the gate.
        """
        from auctions.models import SquareSeller

        if self.club:
            return False
        if self.dismissed_square_banner:
            return False
        if self.created_by.is_superuser:
            return False
        if self.created_by.userdata.never_show_square_connect:
            return False
        if SquareSeller.objects.filter(user=self.created_by).first():
            return False
        return True

    def __str__(self):
        result = self.title
        if "auction" not in self.title.lower():
            result += " auction"
        if not self.title.lower().startswith("the "):
            result = "The " + result
        return result

    @property
    def sender_email(self):
        return build_routed_sender_address(self.slug)

    @property
    def sender_email_with_name(self):
        """The From line for this auction's mail: the club's name (or site domain) over the auction address."""
        return sender_with_display_name(
            self.club.name if self.club else email_routing_domain(),
            self.sender_email,
        )

    @property
    def currency(self):
        """Get the currency for this auction based on the creator"""
        if self.created_by:
            return self.created_by.userdata.currency
        return "USD"

    @property
    def currency_symbol(self):
        """Get the currency symbol for this auction"""
        return get_currency_symbol(self.currency)

    def delete(self, *args, **kwargs):
        """Soft delete: sets is_deleted. Related lots and bids remain; filter with
        ``.exclude(auction__is_deleted=True)``.
        """
        self.is_deleted = True
        self.save()

    def fix_year(self, date_field, low_cutoff=2000, high_cutoff=2050):
        """Assume a year far in the past or future meant this year."""
        if date_field and (date_field.year < low_cutoff or date_field.year > high_cutoff):
            self.create_history("RULES", f"Changed invalid date {date_field.year} to current year")
            current_year = timezone.now().year
            return date_field.replace(year=current_year)
        return date_field

    def save(self, *args, **kwargs):
        previous_club_id = None
        if self.pk:
            previous_club_id = Auction.objects.filter(pk=self.pk).values_list("club_id", flat=True).first()
        self.date_start = self.fix_year(self.date_start)
        self.lot_submission_start_date = self.fix_year(self.lot_submission_start_date)
        self.lot_submission_end_date = self.fix_year(self.lot_submission_end_date)
        self.date_end = self.fix_year(self.date_end)
        self.date_online_bidding_starts = self.fix_year(self.date_online_bidding_starts)
        self.date_online_bidding_ends = self.fix_year(self.date_online_bidding_ends)
        self.summernote_description = sanitize_summernote_html(self.summernote_description)
        super().save(*args, **kwargs)
        if previous_club_id is None and self.club_id:
            self.backfill_club_money()

    def backfill_club_money(self):
        """Reconcile the club ledger for this auction's invoices. ``save`` calls it when a club is attached;
        bulk ``update(club=...)`` callers must call it themselves. Idempotent.
        """
        invoices = (
            Invoice.objects.filter(Q(auction=self) | Q(auctiontos_user__auction=self))
            .select_related("auction", "auctiontos_user", "auctiontos_user__auction")
            .distinct()
        )
        for invoice in invoices:
            invoice.sync_club_money()

    def find_user(self, name="", email="", exclude_pk=None):
        """Duplicate check when adding people: returns an AuctionTOS or None."""
        email = normalize_email(email)
        qs = AuctionTOS.objects.filter(auction__pk=self.pk)
        if not name and not email:
            return None
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        if email:
            email_search = qs.filter(email__iexact=email).order_by("createdon", "pk").first()
            if email_search:
                return email_search
        if name:
            from .filters import AuctionTOSFilter

            # Oldest match wins, deterministically.
            name_search = (
                AuctionTOSFilter.generic(self, qs, name, match_names_only=True).order_by("createdon", "pk").first()
            )
            if name_search:
                return name_search
        return None

    @cached_property
    def estimate_end(self):
        try:
            if self.is_online:
                return None

            expected_sell_percent = 95
            lots_to_use_for_estimate = 10

            total_lots = int(self.total_lots or 0)
            total_unsold = int(self.total_unsold_lots or 0)

            if total_lots == 0:
                return None

            percent_complete = (total_unsold / total_lots) * 100  # percent scale
            if percent_complete > expected_sell_percent:
                return None

            full_qs = self.lots_qs.exclude(date_end__isnull=True).order_by("-date_end")
            if full_qs.count() < lots_to_use_for_estimate:
                return None

            # evaluate a stable list of items
            recent = list(full_qs[:lots_to_use_for_estimate])
            if len(recent) < lots_to_use_for_estimate:
                return None

            first_lot = recent[0]
            last_lot = recent[-1]
            if not getattr(first_lot, "date_end", None) or not getattr(last_lot, "date_end", None):
                return None

            time_diff = first_lot.date_end - last_lot.date_end
            elapsed_seconds = time_diff.total_seconds()
            if elapsed_seconds <= 0:
                return None

            rate = elapsed_seconds / lots_to_use_for_estimate
            minutes_to_end = int(total_unsold * rate / 60)
            if minutes_to_end < 15:
                return None
            return minutes_to_end
        except Exception as e:
            logger.exception("estimate_end failed for auction %s: %s", getattr(self, "pk", "<unknown>"), e)
            return None

    @property
    def location_qs(self):
        """All locations associated with this auction"""
        return PickupLocation.objects.filter(auction=self.pk).order_by("name")

    @cached_property
    def locations(self):
        """Every pickup location, fetched once, as a list; the derived properties read this."""
        return list(self.location_qs)

    @property
    def physical_location_qs(self):
        """Find all non-default locations"""
        # Default locations used to be excluded; no longer.
        return self.location_qs.exclude(pickup_by_mail=True)

    @cached_property
    def physical_locations(self):
        """physical_location_qs, off the cached list"""
        return [location for location in self.locations if not location.pickup_by_mail]

    @property
    def location_with_location_qs(self):
        """Locations with coordinates. New auctions get a coordinate-less default location."""
        return self.physical_location_qs.exclude(latitude=0, longitude=0)

    @cached_property
    def locations_with_coordinates(self):
        """location_with_location_qs, off the cached list"""
        return [
            location for location in self.physical_locations if not (location.latitude == 0 and location.longitude == 0)
        ]

    @cached_property
    def number_of_locations(self):
        """The number of physical locations this auction has"""
        return len(self.physical_locations)

    @cached_property
    def all_location_count(self):
        """All locations, even mail"""
        return len(self.locations)

    @cached_property
    def allow_mailing_lots(self):
        return any(location.pickup_by_mail for location in self.locations)

    @staticmethod
    def get_closest_location_distance_subquery(latitude, longitude):
        """A subquery for the distance to the closest real pickup location (excluding 0,0 and mail-only), for
        annotating auctions.
        """
        return Subquery(
            PickupLocation.objects.filter(auction=OuterRef("pk"))
            .exclude(latitude=0, longitude=0)
            .exclude(pickup_by_mail=True)
            .annotate(distance=distance_to(latitude, longitude))
            .order_by("distance")
            .values("distance")[:1]
        )

    @property
    def auction_type(self):
        """Online, in-person or hybrid, for tooltips and templates; see auction_type_as_str."""
        number_of_locations = self.number_of_locations
        if self.is_online and number_of_locations == 1:
            return "online_one_location"
        if self.is_online and number_of_locations > 1:
            return "online_multi_location"
        if self.is_online and number_of_locations == 0:
            return "online_no_location"
        if not self.is_online and number_of_locations == 1:
            return "inperson_one_location"
        if not self.is_online and number_of_locations > 1:
            return "inperson_multi_location"
        return "unknown"

    @cached_property
    def auction_type_as_str(self):
        """Friendly online/in-person/hybrid string."""
        auction_type = self.auction_type
        if auction_type == "online_one_location":
            return "online auction with in-person pickup"
        if auction_type == "online_multi_location":
            return "online auction with in-person pickup at multiple locations"
        if auction_type == "online_no_location":
            if self.allow_mailing_lots:
                return "online auction with lots delivered by mail"
            return "online auction with no specified pickup location"
        if auction_type == "inperson_one_location":
            return "in-person auction"
        if auction_type == "inperson_multi_location":
            return "in person auction with lot delivery to additional locations"
        return "unknown auction type"

    @property
    def template_promo_info(self):
        if not self.extra_promo_text or self.closed or self.in_person_closed:
            return ""
        if self.extra_promo_link:
            return mark_safe(
                f"<br><a class='magic text-warning' href='{self.extra_promo_link}'>{self.extra_promo_text}</a>"
            )
        return mark_safe(f"<br><span class='magic text-warning'>{self.extra_promo_text}</span>")

    @property
    def template_date_timestamp(self):
        """For use in all auctions list"""
        if self.closed or self.in_progress:
            return self.date_end
        return self.date_start

    @property
    def template_status(self):
        """What's the `template_date_timestamp` for this auction?"""
        if self.in_progress:
            return "Now until:"
        if not self.started:
            return "Starts:"
        return ""

    @property
    def template_pre_register_fee(self):
        """For templates: winning_bid_percent_to_club - pre_register_lot_discount_percent."""
        return self.winning_bid_percent_to_club - self.pre_register_lot_discount_percent

    @property
    def uses_alternate_split(self):
        """True when some users (AuctionTOS.is_club_member) get the alternate fees."""
        return self.alternate_split_mode != "off"

    def get_absolute_url(self):
        return self.url

    def get_edit_url(self):
        return reverse("edit_auction", kwargs={"slug": self.slug})

    @property
    def url(self):
        return reverse("auction_main", kwargs={"slug": self.slug})

    @property
    def label_print_link(self):
        return f"{self.get_absolute_url()}?printredirect={reverse('print_my_labels', kwargs={'slug': self.slug})}"

    @property
    def label_print_unprinted_link(self):
        return f"{self.get_absolute_url()}?printredirect={reverse('print_my_unprinted_labels', kwargs={'slug': self.slug})}"

    @property
    def add_lot_link(self):
        return f"{reverse('new_lot')}?auction={self.slug}"

    @property
    def view_lot_link(self):
        return f"{reverse('allLots')}?auction={self.slug}&status=all"

    @property
    def user_admin_link(self):
        return reverse("auction_tos_list", kwargs={"slug": self.slug})

    @property
    def set_lot_winners_link(self):
        return f"{self.get_absolute_url()}lots/set-winners/"

    @cached_property
    def is_club_managed(self):
        """True when participants are managed via the club's ClubMember records."""
        return bool(self.manage_users_through_club) and bool(self.club_id)

    @property
    def manage_users_auto_add(self):
        """True when club members are automatically added as AuctionTOS participants."""
        return self.manage_users_through_club == "all" and bool(self.club_id)

    @property
    def use_check_in_mode(self):
        """True when club members are added as they check in."""
        return self.manage_users_through_club == "checkin" and bool(self.club_id)

    @property
    def allows_app_self_checkin(self):
        """True when attendees may join and check in from the app's proximity prompt. Only check-in auctions can
        turn it off (``allow_self_checkin``), since there checking in assigns bidder numbers.
        """
        if not self.use_check_in_mode:
            return True
        return self.allow_self_checkin

    def in_welcome_window(self, now=None):
        """True from 3 hours before the start until the auction has wrapped up (date_end, else date_start + 12h),
        for the proximity welcome.
        """
        now = now or timezone.now()
        if not self.date_start:
            return False
        if now < self.date_start - datetime.timedelta(hours=3):
            return False
        end = self.date_end or (self.date_start + datetime.timedelta(hours=12))
        return now <= end

    def permission_check(self, user):
        """See if `user` can make changes to this auction"""
        if self.created_by_id and self.created_by_id == getattr(user, "pk", None):
            # by id, so the creator is not fetched just to compare them
            return True
        if user.is_superuser:
            return True
        if not user.is_authenticated:
            return False
        tos = AuctionTOS.objects.filter(is_admin=True, user=user, user__isnull=False, auction=self.pk).first()
        if tos:
            return True
        if self.is_club_managed:
            has_club_admin = (
                ClubMember.objects.filter(club_id=self.club_id, user=user, is_deleted=False)
                .filter(Q(permission_admin=True) | Q(permission_manage_auctions=True))
                .exists()
            )
            if has_club_admin:
                return True
        return False

    @cached_property
    def pickup_locations_before_end(self):
        """The edit URL of the first pickup location whose time is before the auction end (or start, in person)."""
        locations = self.locations
        time_to_use = self.date_end
        if not self.is_online:
            time_to_use = self.date_start
        for location in locations:
            error = False
            try:
                if location.pickup_time < time_to_use:
                    error = True
                if location.second_pickup_time:
                    if location.second_pickup_time < time_to_use:
                        error = True
            except:
                error = False
            if error:
                return reverse("edit_pickup", kwargs={"pk": location.pk})
        return False

    @property
    def has_non_logical_times(self):
        """The edit URL if start or end isn't on :00 or :30, else False."""
        # A logical time has minutes of 00 or 30, and seconds of 00
        for date_field in [self.date_start, self.date_end]:
            if date_field:
                local_time = date_field.astimezone(self.timezone)
                minutes = local_time.minute
                seconds = local_time.second
                # Check if time is not :00:00 or :30:00
                if not ((minutes == 0 or minutes == 30) and seconds == 0):
                    return reverse("edit_auction", kwargs={"slug": self.slug})
        return False

    @property
    def timezone(self):
        try:
            return pytz_timezone(self.created_by.userdata.timezone)
        except:
            return pytz_timezone(settings.TIME_ZONE)

    @property
    def time_start_is_at_night(self):
        date_start_local = self.date_start.astimezone(self.timezone)
        start_time = date_start_local.time()
        midnight = time(0, 0)
        six_am = time(6, 0)
        return midnight <= start_time <= six_am

    @property
    def dynamic_end(self):
        """The absolute latest a lot in this auction can end"""
        if self.sealed_bid:
            return self.date_end
        else:
            dynamic_end = datetime.timedelta(minutes=60)
            return self.date_end + dynamic_end

    @cached_property
    def date_end_as_str(self):
        """Readable end date; always "" for in-person auctions."""
        if self.is_online:
            return self.date_end
        else:
            return ""

    @property
    def minutes_to_end(self):
        if not self.date_end:
            return 9999
        timedelta = self.date_end - timezone.now()
        seconds = timedelta.total_seconds()
        if seconds < 0:
            return 0
        minutes = seconds // 60
        return minutes

    @property
    def ending_soon(self):
        """Used to send notifications"""
        if self.minutes_to_end < 120:
            return True
        else:
            return False

    @cached_property
    def closed(self):
        """For display on the main auctions list"""
        if self.is_online and self.date_end:
            if timezone.now() > self.dynamic_end:
                return True
        # in-person auctions don't end right now
        return False

    @property
    def in_person_closed(self):
        """In-person auction started with online bidding disabled."""
        if not self.is_online and timezone.now() > self.date_start and self.online_bidding == "disable":
            return True
        if (
            self.date_online_bidding_ends
            and not self.is_online
            and self.online_bidding != "disable"
            and timezone.now() > self.date_online_bidding_ends
            and timezone.now() > self.date_start
        ):
            return True
        return False

    @cached_property
    def wind_down_time(self):
        """When the auction is fully wound down, before pretty_much_over's grace period.

        Online: the latest of date_end and every pickup time (date_end is a floor since pickups aren't
        enforced to be later). In person: the latest of start, online bidding end and lot submission end.
        None if the date is missing.
        """
        if not self.is_online:
            return max(
                self.date_start,
                self.date_online_bidding_ends or self.date_start,
                self.lot_submission_end_date or self.date_start,
            )
        latest = self.date_end
        for location in self.locations:
            for pickup in (location.pickup_time, location.second_pickup_time):
                if pickup and (latest is None or pickup > latest):
                    latest = pickup
        return latest

    @cached_property
    def pretty_much_over(self):
        """True once wound down for 24 hours. Unlike ``closed``, waits for pickups; stops the auction appearing
        in the palette and lets endauctions deactivate stray lots.
        """
        reference = self.wind_down_time
        if not reference:
            return False
        return timezone.now() > reference + datetime.timedelta(hours=24)

    @property
    def ended_badge(self):
        if self.closed or self.in_person_closed:
            return mark_safe('<span class="badge bg-danger">Ended</span>')
        return ""

    @cached_property
    def started(self):
        """For display on the main auctions list"""
        if timezone.now() > self.date_start:
            return True
        if (
            self.date_online_bidding_starts
            and not self.is_online
            and self.online_bidding != "disable"
            and timezone.now() > self.date_online_bidding_starts
        ):
            return True
        return False

    @property
    def in_person_in_progress(self):
        """For display on the main auctions list"""
        if not self.is_online and self.started and not self.in_person_closed:
            return True
        return False

    @property
    def in_progress(self):
        """For display on the main auctions list"""
        if self.is_online and self.started and not self.closed:
            return True
        return False

    @cached_property
    def club_profit_raw(self):
        """The club's raw cut of lots, ignoring invoice rounding and adjustments."""
        return add_price_info(self.lots_qs).aggregate(total_sold=Sum("club_cut"))["total_sold"] or 0

    @cached_property
    def _auction_tax_collected(self):
        """Sales tax collected across invoices: a pass-through liability, not profit. Computed from live sold
        lots, quantized once, so it may differ from summed per-invoice tax by a fraction of a cent.
        """
        if not self.tax:
            return Decimal("0.00")
        money = DecimalField(max_digits=12, decimal_places=2)
        total_final = self.lots_qs.filter(winning_price__isnull=False, banned=False).aggregate(
            total=Coalesce(
                Sum(
                    ExpressionWrapper(
                        Cast(F("winning_price"), money)
                        * (Value(Decimal("100.00")) - Cast(F("partial_refund_percent"), money))
                        / Value(Decimal("100.00")),
                        output_field=money,
                    ),
                    output_field=money,
                ),
                Value(Decimal("0.00")),
                output_field=money,
            )
        )["total"]
        rate = Decimal(self.tax) / Decimal(100)
        return (Decimal(total_final) * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @cached_property
    def _auction_membership_dues(self):
        """Membership dues collected on this auction's invoices: separate club revenue, excluded from
        ``club_profit``. Fee times renewing invoices.
        """
        club = self.club
        if not club or not club.membership_annual_fee:
            return Decimal("0.00")
        renewals = Invoice.objects.filter(auction=self.pk, renewal_needed=True).count()
        return Decimal(club.membership_annual_fee) * renewals

    @cached_property
    def club_profit(self):
        """What the club nets from auction activity; negative on a loss.

        Invoice-realized: rounding, first-bid payouts, member discounts and adjustments included. Tax and dues
        excluded (as the treasurer report). Never abs()'d. Unstamped invoices use their live ``rounded_net``;
        PAID ones their frozen ``calculated_total``. Decimal throughout.
        """
        invoices = Invoice.objects.filter(auction=self.pk)
        # Stamped totals in one query, plus live rounded_net for unstamped invoices.
        total_net = invoices.filter(calculated_total__isnull=False).aggregate(total=Sum("calculated_total"))[
            "total"
        ] or Decimal("0.00")
        for invoice in invoices.filter(calculated_total__isnull=True).select_related(
            "auction__club", "club", "auctiontos_user"
        ):
            total_net += Decimal(invoice.rounded_net)
        # calculated_total is negative when the buyer owes; negate, never abs().
        profit = -Decimal(total_net)
        # Back out tax and dues.
        profit -= self._auction_tax_collected
        profit -= self._auction_membership_dues
        return profit

    @cached_property
    def gross(self):
        """Refund-adjusted gross: the sum of sold, non-banned lots' final prices.

        Banned lots are excluded and partial refunds netted out, so ``gross == total_to_sellers +
        club_profit_raw`` and it matches ``total_sold_lots``. Donations count. Decimal.
        """
        money = DecimalField(max_digits=12, decimal_places=2)
        return self.lots_qs.filter(winning_price__isnull=False, banned=False).aggregate(
            total=Coalesce(
                Sum(
                    ExpressionWrapper(
                        Cast(F("winning_price"), money)
                        * (Value(Decimal("100.00")) - Cast(F("partial_refund_percent"), money))
                        / Value(Decimal("100.00")),
                        output_field=money,
                    ),
                    output_field=money,
                ),
                Value(Decimal("0.00")),
                output_field=money,
            )
        )["total"]

    @cached_property
    def total_to_sellers(self):
        """Total credited to sellers (``your_cut``) for sold, non-banned lots. Summed directly: ``gross -
        club_profit`` would be skewed by tax, dues and buyer promotions. Decimal.
        """
        total = add_price_info(self.lots_qs.filter(winning_price__isnull=False, banned=False)).aggregate(
            total=Sum("your_cut")
        )["total"]
        return total if total is not None else Decimal("0.00")

    @property
    def percent_to_club(self):
        """``club_profit / gross`` as a percentage; ``Decimal("0")`` with no gross. Negative on a loss."""
        gross = self.gross
        if not gross:
            return Decimal(0)
        return Decimal(self.club_profit) / Decimal(gross) * 100

    @cached_property
    def total_donations(self):
        # Exclude banned lots, like the other money figures.
        return (
            self.lots_qs.filter(winning_price__isnull=False, donation=True)
            .exclude(banned=True)
            .aggregate(total=Sum("winning_price"))["total"]
            or 0
        )

    @property
    def invoice_recalculate(self):
        """Force update of all invoice totals in this auction"""
        invoices = Invoice.objects.filter(auction=self.pk)
        for invoice in invoices:
            invoice.recalculate()
            invoice.save()

    @property
    def show_invoice_ready_button(self):
        """Always True; the ready status is confusing, but PayPal may need it."""
        return True

    @cached_property
    def users_with_bidding_disabled(self):
        """Participants who can't bid, for the bulk re-enable button. Zero in check-in auctions, where bidding
        waits for check-in.
        """
        if self.use_check_in_mode:
            return 0
        return AuctionTOS.objects.filter(auction=self.pk, bidding_allowed=False).count()

    @property
    def show_paypal_csv_link(self):
        if self.is_online:
            return True
        if self.online_bidding == "disable":
            return False
        return True

    @property
    def paypal_payments_enabled(self):
        """True when buyers can pay invoices through PayPal directly (hides the manual PayPal CSV export).
        Includes club site or own-credential routes.
        """
        if self.club and (self.club.uses_site_paypal or self.club.uses_own_paypal_credentials):
            return True
        return bool(self.enable_online_payments)

    @cached_property
    def tos_qs(self):
        """AuctionTOS rows with ``has_ever_granted_permission``: the user has joined any of this creator's
        auctions themselves (manually_added=False).
        """
        return (
            AuctionTOS.objects.filter(auction=self.pk)
            .annotate(
                has_ever_granted_permission=Case(
                    When(
                        Q(user__isnull=False)
                        & Exists(
                            AuctionTOS.objects.filter(
                                user=OuterRef("user"), auction__created_by=self.created_by, manually_added=False
                            )
                        ),
                        then=Value(True),
                    ),
                    default=Value(False),
                    output_field=BooleanField(),
                )
            )
            .order_by("-createdon")
        )

    @cached_property
    def number_of_confirmed_tos(self):
        """How many people selected a pickup location in this auction"""
        return self.tos_qs.count()

    @cached_property
    def seller_tos_qs(self):
        """Participants with at least one live (not banned or deleted) lot here, distinct."""
        return AuctionTOS.objects.filter(
            auctiontos_seller__auction=self.pk,
            auctiontos_seller__banned=False,
            auctiontos_seller__is_deleted=False,
        ).distinct()

    @cached_property
    def buyer_tos_qs(self):
        """Participants who won at least one sold, live lot, distinct."""
        return AuctionTOS.objects.filter(
            auctiontos_winner__auction=self.pk,
            auctiontos_winner__winning_price__isnull=False,
            auctiontos_winner__banned=False,
            auctiontos_winner__is_deleted=False,
        ).distinct()

    @cached_property
    def number_of_sellers(self):
        return self.seller_tos_qs.count()

    @cached_property
    def number_of_sellers_who_didnt_buy(self):
        return self.seller_tos_qs.exclude(id__in=self.buyer_tos_qs.values_list("id", flat=True)).count()

    @cached_property
    def number_of_buyers(self):
        return self.buyer_tos_qs.count()

    @cached_property
    def median_lot_price(self):
        # Sold, non-banned lots on hammer price; partial refunds not netted.
        lots = self.lots_qs.filter(winning_price__isnull=False).exclude(banned=True)
        if lots:
            return median_value(lots, "winning_price")
        else:
            return 0

    @cached_property
    def lots_qs(self):
        """All lots in this auction"""
        # return Lot.objects.exclude(is_deleted=True).filter(auction=self.pk)
        return Lot.objects.exclude(is_deleted=True).filter(auctiontos_seller__auction__pk=self.pk)

    @cached_property
    def total_sold_lots(self):
        return self.lots_qs.filter(winning_price__isnull=False).exclude(banned=True).count()

    @cached_property
    def total_sold_lots_with_buy_now_percent(self):
        """Percentage of sold lots via buy now. Online uses ``buy_now_used``; in person infers
        ``winning_price == buy_now_price``, which over-counts coincidental matches.
        """
        if not self.total_sold_lots:
            return 0
        if self.is_online:
            return (
                self.lots_qs.filter(winning_price__isnull=False, buy_now_used=True).exclude(banned=True).count()
                / self.total_sold_lots
                * 100
            )
        else:
            return (
                self.lots_qs.filter(winning_price__isnull=False, winning_price=F("buy_now_price"))
                .exclude(banned=True)
                .count()
                / self.total_sold_lots
                * 100
            )

    @cached_property
    def total_unsold_lots(self):
        return self.lots_qs.filter(winning_price__isnull=True).exclude(banned=True).count()

    @cached_property
    def total_lots(self):
        return self.lots_qs.exclude(banned=True).count()

    @cached_property
    def number_of_lots_with_scanned_qr(self):
        # "Scanned": opened from a QR code (src=qr) or AR (src=ar).
        return (
            self.lots_qs.filter(
                Q(pageview__source__icontains="qr") | Q(pageview__source__iexact="ar"),
                auction__pk=self.pk,
            )
            .distinct()
            .count()
        )

    @cached_property
    def number_of_lots_added_to_queue(self):
        # Lots ever queued (sticky Lot.added_to_queue).
        return self.lots_qs.filter(added_to_queue=True).count()

    @cached_property
    def labels_qs(self):
        lots = self.lots_qs.exclude(banned=True)
        if self.is_online:
            lots = lots.filter(auctiontos_winner__isnull=False, winning_price__isnull=False)
        return lots

    @cached_property
    def unprinted_labels_qs(self):
        return self.labels_qs.exclude(label_printed=True)

    @property
    def percent_unsold_lots(self):
        # No lots means 0% unsold, like the sibling percentages.
        if not self.total_lots:
            return 0
        return self.total_unsold_lots / self.total_lots * 100

    @cached_property
    def lots_sold_per_minute(self):
        """Average lots sold per minute in person, ignoring the first and last 10% of lots (as the speed graph)."""
        if self.is_online:
            return 0  # Not applicable for online auctions

        ignore_percent = 10
        lots = (
            Lot.objects.exclude(Q(date_end__isnull=True) | Q(is_deleted=True))
            .filter(auction=self, winning_price__isnull=False)
            .order_by("-date_end")
        )
        total_lots = lots.count()

        if total_lots < 10:  # Not enough lots for meaningful calculation
            return 0

        # Calculate start and end indices to ignore first and last 10%
        start_index = int(ignore_percent / 100 * total_lots)
        end_index = int((1 - (ignore_percent / 100)) * total_lots) - 1

        if start_index >= end_index:
            return 0

        # Get the time range for the middle 80% of lots
        start_date = lots[start_index].date_end
        end_date = lots[end_index].date_end

        # Calculate total time in minutes
        total_time = (start_date - end_date).total_seconds() / 60

        # Calculate number of lots in this time period
        num_lots = end_index - start_index

        if total_time <= 0:
            return 0

        # Return lots per minute
        return num_lots / total_time

    @cached_property
    def total_auction_duration(self):
        """In person, ignoring the first and last 10% of lots, as the speed graph."""
        if self.is_online:
            return 0  # Not applicable for online auctions

        ignore_percent = 10
        lots = (
            Lot.objects.exclude(Q(date_end__isnull=True) | Q(is_deleted=True))
            .filter(auction=self, winning_price__isnull=False)
            .order_by("-date_end")
        )
        total_lots = lots.count()

        if total_lots < 10:  # Not enough lots for meaningful calculation
            return 0

        # Calculate start and end indices to ignore first and last 10%
        start_index = int(ignore_percent / 100 * total_lots)
        end_index = int((1 - (ignore_percent / 100)) * total_lots) - 1

        if start_index >= end_index:
            return 0

        # Get the time range for the middle 80% of lots
        start_date = lots[start_index].date_end
        end_date = lots[end_index].date_end

        # Calculate total time in minutes
        middle_time = (start_date - end_date).total_seconds() / 60
        return middle_time + middle_time * ignore_percent * 2 / 100

    @property
    def total_auction_duration_str(self):
        """Format total_auction_duration (minutes) as 'Hh MMm'."""
        minutes_total = int(round(self.total_auction_duration or 0))
        hours, minutes = divmod(minutes_total, 60)
        return f"{hours}h {minutes:02d}m"

    @property
    def template_lot_link(self):
        """Not used directly; see template_lot_link_first_column and template_lot_link_separate_column."""
        if timezone.now() > self.lot_submission_start_date:
            result = f"<a href='{self.view_lot_link}'>View lots</a>"
        else:
            result = "<small class='text-muted'>Lots not yet open</small>"
        return result

    @property
    def template_lot_link_first_column(self):
        """Shown on small screens only"""
        return mark_safe(f'<small><span class="d-md-none"><br>{self.template_lot_link}</span></small>')

    @property
    def template_lot_link_separate_column(self):
        """Shown on big screens only"""
        return mark_safe(f'<span class="d-none d-md-inline">{self.template_lot_link}</span>')

    @property
    def can_submit_lots(self):
        if timezone.now() < self.lot_submission_start_date:
            return False
        if self.lot_submission_end_date:
            if self.lot_submission_end_date < timezone.now():
                return False
            else:
                return True
        if self.is_online:
            if self.date_end > timezone.now():
                return False
        return True

    @cached_property
    def number_of_participants(self):
        """Participants who bought or sold at least one live lot."""
        buyer_ids = self.buyer_tos_qs.values_list("id", flat=True)
        sellers_who_didnt_buy = self.seller_tos_qs.exclude(id__in=buyer_ids).count()
        return sellers_who_didnt_buy + self.buyer_tos_qs.count()

    @cached_property
    def preregistered_users(self):
        return AuctionTOS.objects.filter(auction=self.pk, manually_added=False).count()

    @cached_property
    def campaigns_qs(self):
        return AuctionCampaign.objects.filter(auction=self.pk).order_by("-timestamp")

    @cached_property
    def number_of_reminder_emails(self):
        return self.campaigns_qs.exclude(result="ERR").count()

    @cached_property
    def reminder_email_clicks(self):
        if self.number_of_reminder_emails == 0:
            return 0
        return (
            self.campaigns_qs.exclude(result="ERR").exclude(result="NONE").count()
            / self.number_of_reminder_emails
            * 100
        )

    @cached_property
    def reminder_email_joins(self):
        if self.number_of_reminder_emails == 0:
            return 0
        return self.campaigns_qs.filter(result="JOINED").count() / self.number_of_reminder_emails * 100

    @cached_property
    def all_auctions_reminder_email_clicks(self):
        campaigns = AuctionCampaign.objects.exclude(result="ERR").count()
        if campaigns == 0:
            return 0
        return AuctionCampaign.objects.exclude(result="ERR").exclude(result="NONE").count() / campaigns * 100

    @cached_property
    def all_auctions_reminder_email_joins(self):
        campaigns = AuctionCampaign.objects.exclude(result="ERR").count()
        if campaigns == 0:
            return 0
        return AuctionCampaign.objects.filter(result="JOINED").count() / campaigns * 100

    @cached_property
    def weekly_promo_email_clicks(self):
        return PageView.objects.filter(source="weekly_email", auction=self.pk).count()

    @property
    def weekly_promo_email_click_rate(self):
        if self.weekly_promo_emails_sent == 0:
            return 0
        return (self.weekly_promo_email_clicks / self.weekly_promo_emails_sent) * 100

    @cached_property
    def multi_location(self):
        """True with more than one location."""
        return self.number_of_locations > 1

    @cached_property
    def no_location(self):
        """True with no physical pickup location (mail excluded)."""
        return not self.locations_with_coordinates

    @property
    def can_be_deleted(self):
        if self.total_lots:
            return False
        else:
            return True

    @cached_property
    def paypal_invoices(self):
        return Invoice.objects.filter(auction=self, status="UNPAID")

    @cached_property
    def draft_paypal_invoices(self):
        """Used for a tooltip warning telling people to make invoices ready"""
        return Invoice.objects.filter(auction=self, status="DRAFT", calculated_total__lt=0).count()

    @property
    def paypal_invoices_to_export(self):
        """The UNPAID invoices written to the PayPal bulk CSV: those still owing (``rounded_net_after_payments <
        0``). Shared by ``paypal_invoice_chunks`` and the export view so chunk counts agree.
        """
        return [invoice for invoice in self.paypal_invoices if invoice.rounded_net_after_payments < 0]

    @property
    def paypal_invoice_chunks(self):
        """Chunks for the PayPal invoice export (150 per https://www.paypal.com/invoice/batch)."""
        invoices_count = len(self.paypal_invoices_to_export)
        chunk_size = 150
        chunks = (invoices_count + chunk_size - 1) // chunk_size
        return list(range(1, chunks + 1))

    @cached_property
    def set_location_link(self):
        """Edit link for the first location missing coordinates."""
        location = next(
            (
                candidate
                for candidate in self.locations
                if candidate.latitude == 0 and candidate.longitude == 0 and not candidate.pickup_by_mail
            ),
            None,
        )
        if self.all_location_count == 1:
            location = self.locations[0]
        if location:
            return reverse("edit_pickup", kwargs={"pk": location.pk})
        return None

    @cached_property
    def admin_checklist_mostly_completed(self):
        if (
            self.admin_checklist_location_set
            and self.admin_checklist_rules_updated
            and self.admin_checklist_joined
            and self.admin_checklist_others_joined
        ):
            return True
        return False

    @cached_property
    def admin_checklist_completed(self):
        if (
            self.admin_checklist_mostly_completed
            and self.admin_checklist_lots_added
            and self.admin_checklist_winner_set
            and self.admin_checklist_additional_admin
        ):
            return True
        return False

    @cached_property
    def admin_checklist_location_set(self):
        return bool(self.allow_mailing_lots or self.locations_with_coordinates)

    @cached_property
    def admin_checklist_rules_updated(self):
        if "You should remove this line and edit this section to suit your auction." in self.summernote_description:
            return False
        return True

    @cached_property
    def admin_checklist_joined(self):
        if (
            AuctionTOS.objects.filter(auction__pk=self.pk).filter(Q(user=self.created_by) | Q(is_admin=True)).count()
            > 0
        ):
            return True
        return False

    @cached_property
    def admin_checklist_others_joined(self):
        if self.number_of_confirmed_tos > 1:
            return True
        return False

    @cached_property
    def admin_checklist_lots_added(self):
        if self.lots_qs.exists():
            return True
        return False

    @cached_property
    def admin_checklist_winner_set(self):
        if self.is_online:
            return True
        if self.lots_qs.filter(auctiontos_winner__isnull=False).exists():
            return True
        return False

    @cached_property
    def admin_checklist_additional_admin(self):
        if self.is_online:
            return True
        if (
            AuctionTOS.objects.filter(auction__pk=self.pk).exclude(user=self.created_by).filter(is_admin=True).count()
            > 0
        ):
            return True
        return False

    @cached_property
    def event_needing_custom_wording(self):
        """This auction's calendar event when an admin should be asked to reword it, else None.

        The club's website shows our events, the event is generated, neither field is custom, and the auction
        hasn't happened.
        """
        if self.dismissed_customize_event_banner or not self.club_id:
            return None
        if self.pretty_much_over:
            return None
        if not self.club.embeds_events_on_website:
            return None
        event = self.calendar_events.filter(is_deleted=False).first()
        if not event or not event.is_automatic:
            return None
        if event.title_is_custom or event.description_is_custom:
            return None
        return event

    @cached_property
    def location_link(self):
        if not self.all_location_count:
            return reverse("create_auction_pickup_location", kwargs={"slug": self.slug})
        if self.all_location_count == 1 and not self.is_online:
            return reverse("edit_pickup", kwargs={"pk": self.locations[0].pk})
        return reverse("auction_pickup_location", kwargs={"slug": self.slug})

    @property
    def video_tutorial(self):
        if self.is_online:
            return settings.ONLINE_TUTORIAL_YOUTUBE_ID
        else:
            return settings.IN_PERSON_TUTORIAL_YOUTUBE_ID

    @property
    def video_tutorial_chapters(self):
        if self.is_online:
            return settings.ONLINE_TUTORIAL_CHAPTERS
        else:
            return settings.IN_PERSON_TUTORIAL_CHAPTERS

    @property
    def hybrid_tutorial(self):
        return settings.HYBRID_TUTORIAL_YOUTUBE_ID

    @property
    def hybrid_tutorial_chapters(self):
        return settings.HYBRID_TUTORIAL_CHAPTERS

    @cached_property
    def auction_admins_qs(self):
        # user_id, not user, to avoid fetching the creator row.
        return AuctionTOS.objects.filter(
            Q(is_admin=True) | Q(user_id=self.created_by_id), auction__pk=self.pk
        ).order_by("name")

    @cached_property
    def auction_admins_pks(self):
        """For use in querysets, pks only"""
        return self.auction_admins_qs.values_list("user__pk", flat=True)

    @property
    def auction_admins_user_pks(self):
        """User pks of the whole admin team for ban enforcement: always the creator, never None."""
        pks = {pk for pk in self.auction_admins_pks if pk}
        if self.created_by_id:
            pks.add(self.created_by_id)
        return pks

    def user_banned_by_admins(self, user):
        """True if anyone on the admin team banned ``user``. Bans apply to every auction the banner administers."""
        if not user or not getattr(user, "pk", None):
            return False
        return UserBan.objects.filter(banned_user=user.pk, user__pk__in=self.auction_admins_user_pks).exists()

    def tos_for_user(self, user):
        """The AuctionTOS for a signed-in user, by user FK or account email, newest first, or None. Bid
        enforcement and the lot page both use this.
        """
        if not user or not getattr(user, "is_authenticated", False):
            return None
        query = Q(user=user)
        if user.email:
            query |= Q(email=user.email)
        return AuctionTOS.objects.filter(query, auction=self).order_by("-createdon").first()

    # Stat getter/setter properties
    @property
    def get_stat_activity(self):
        """Get activity chart data from cached stats"""
        if self.cached_stats and "activity" in self.cached_stats:
            return self.cached_stats["activity"]
        return {"labels": [], "providers": [], "data": []}

    def set_stat_activity(self):
        """Calculate and return activity chart data"""
        bins = 21
        days_before = 16
        days_after = bins - days_before
        dates_messed_with = False

        if self.is_online:
            date_start = self.date_end - timezone.timedelta(days=days_before)
            date_end = self.date_end + timezone.timedelta(days=days_after)
        else:  # in person
            date_start = self.date_start - timezone.timedelta(days=days_before)
            date_end = self.date_start + timezone.timedelta(days=days_after)

        # A future date_end shifts the graph to the same range ending now.
        if date_end > timezone.now():
            time_difference = date_end - date_start
            date_end = timezone.now()
            date_start = date_end - time_difference
            dates_messed_with = True

        views = self.page_views
        joins = AuctionTOS.objects.filter(auction=self)
        new_lots = Lot.objects.filter(auction=self)
        searches = SearchHistory.objects.filter(auction=self)
        bids = LotHistory.objects.filter(lot__auction=self, changed_price=True)
        watches = Watch.objects.filter(lot_number__auction=self)

        return {
            "labels": self._get_activity_labels(bins, days_before, days_after, dates_messed_with),
            "providers": ["Views", "Joins", "New lots", "Searches", "Bids", "Watches"],
            "data": [
                bin_data(views, "date_start", bins, date_start, date_end),
                bin_data(joins, "createdon", bins, date_start, date_end),
                bin_data(new_lots, "date_posted", bins, date_start, date_end),
                bin_data(searches, "createdon", bins, date_start, date_end),
                bin_data(bids, "timestamp", bins, date_start, date_end),
                bin_data(watches, "createdon", bins, date_start, date_end),
            ],
        }

    @property
    def get_stat_attrition(self):
        """Get attrition chart data from cached stats"""
        if self.cached_stats and "attrition" in self.cached_stats:
            return self.cached_stats["attrition"]
        return {"labels": [], "providers": [], "data": []}

    def set_stat_attrition(self):
        """Calculate and return attrition chart data"""
        ignore_percent = 10
        lots = (
            Lot.objects.exclude(Q(date_end__isnull=True) | Q(is_deleted=True) | Q(banned=True))
            .filter(auction=self, winning_price__isnull=False)
            .order_by("-date_end")
        )
        total_lots = lots.count()
        if total_lots > 0:
            start_index = int(ignore_percent / 100 * total_lots)
            end_index = int((1 - (ignore_percent / 100)) * total_lots) - 1
            start_date = lots[start_index].date_end
            end_date = lots[end_index].date_end if total_lots > 1 else start_date
            total_runtime = end_date - start_date
            add_back_on = total_runtime / ignore_percent
            start_date = start_date - (add_back_on * 2)
            end_date = end_date + (add_back_on * 2)
            lots = lots.filter(date_end__lte=start_date, date_end__gte=end_date)

            attrition_data = [
                {
                    "x": (lot.date_end - end_date).total_seconds() // 60,
                    "y": lot.winning_price,
                }
                for lot in lots
            ]
            return {
                "labels": [],
                "providers": ["Lots"],
                "data": [attrition_data],
            }
        else:
            return {"labels": [], "providers": ["Lots"], "data": [[]]}

    @property
    def get_stat_auctioneer_speed(self):
        """Get auctioneer speed chart data from cached stats"""
        if self.cached_stats and "auctioneer_speed" in self.cached_stats:
            return self.cached_stats["auctioneer_speed"]
        return {"labels": [], "providers": [], "data": []}

    def set_stat_auctioneer_speed(self):
        """Calculate and return auctioneer speed chart data"""
        lots = (
            Lot.objects.exclude(Q(date_end__isnull=True) | Q(is_deleted=True) | Q(banned=True))
            .filter(auction=self, winning_price__isnull=False)
            .order_by("-date_end")
        )
        auctioneer_data = []
        for i in range(1, len(lots)):
            minutes = (lots[i - 1].date_end - lots[i].date_end).total_seconds() / 60
            ignore_if_more_than = 3  # minutes
            if minutes <= ignore_if_more_than:
                auctioneer_data.append({"x": i, "y": minutes})
        return {
            "labels": [],
            "providers": ["Minutes per lot"],
            "data": [auctioneer_data],
        }

    @property
    def get_stat_lot_sell_prices(self):
        """Get lot sell prices chart data from cached stats"""
        if self.cached_stats and "lot_sell_prices" in self.cached_stats:
            return self.cached_stats["lot_sell_prices"]
        return {"labels": [], "providers": [], "data": []}

    def _lot_sell_price_bins(self):
        """Sell-price histogram bins: ``(start_bin, bin_width, num_bins)``, whole-dollar and left-inclusive.
        Labels and counts both derive from this. Banned lots excluded.
        """
        sold_lots = self.lots_qs.filter(winning_price__isnull=False).exclude(banned=True)

        # Make bins dynamic based on actual sell prices
        if sold_lots.exists():
            max_price = sold_lots.aggregate(max_price=Max("winning_price"))["max_price"] or 40
            # Round up to nearest $10 for cleaner bins
            max_price = int((max_price + 9) // 10 * 10)

            # $2 bins aligned to whole dollars.
            bin_width = 2  # Each bin covers $2
            num_bins = min((max_price - 1) // bin_width, 30)  # Cap at 30 bins to avoid too many
            if num_bins < 10:
                num_bins = 10  # Minimum 10 bins
                bin_width = max((max_price - 1) // num_bins, 1)  # Adjust bin width if needed
        else:
            # No sold lots, use default bins
            bin_width = 2
            num_bins = 19

        return 1, bin_width, num_bins

    def set_stat_lot_sell_prices(self):
        """Lot sell price chart data, from ``_lot_sell_price_bins``."""
        sold_lots = self.lots_qs.filter(winning_price__isnull=False).exclude(banned=True)
        start_bin, bin_width, num_bins = self._lot_sell_price_bins()
        end_bin = start_bin + num_bins * bin_width

        histogram = bin_data(
            sold_lots,
            "winning_price",
            number_of_bins=num_bins,
            start_bin=start_bin,
            end_bin=end_bin,
            add_column_for_high_overflow=True,
        )

        # "Not sold", the priced buckets, then "{end_bin}+".
        labels = ["Not sold"]
        for i in range(num_bins):
            bin_start = start_bin + i * bin_width
            bin_end = start_bin + (i + 1) * bin_width
            labels.append(f"{self.currency_symbol}{bin_start}-{bin_end}")
        labels.append(f"{self.currency_symbol}{end_bin}+")

        return {
            "labels": labels,
            "providers": ["Number of lots"],
            "data": [[self.total_unsold_lots] + histogram],
        }

    @property
    def get_stat_referrers(self):
        """Get referrers chart data from cached stats"""
        if self.cached_stats and "referrers" in self.cached_stats:
            return self.cached_stats["referrers"]
        return {"labels": [], "providers": [], "data": []}

    def set_stat_referrers(self):
        """Calculate and return referrers chart data"""
        from django.contrib.sites.models import Site

        views = (
            self.page_views.exclude(referrer__isnull=True)
            .exclude(referrer__startswith=Site.objects.get_current().domain)
            .exclude(referrer__exact="")
            .values("referrer")
            .annotate(count=Count("referrer"))
        )
        labels = []
        data = []
        other = 0
        for view in views:
            if view["count"] > 1:
                labels.append(view["referrer"])
                data.append(view["count"])
            else:
                other += 1
        labels.append("Other")
        data.append(other)
        return {
            "labels": labels,
            "providers": ["Number of clicks"],
            "data": [data],
        }

    @property
    def get_stat_images(self):
        """Get images chart data from cached stats"""
        if self.cached_stats and "images" in self.cached_stats:
            return self.cached_stats["images"]
        return {"labels": [], "providers": [], "data": []}

    def set_stat_images(self):
        """Calculate and return images chart data"""
        from django.db.models import Avg

        # Exclude banned lots, as the other money stats.
        lots = (
            self.lots_qs.filter(winning_price__isnull=False).exclude(banned=True).annotate(num_images=Count("lotimage"))
        )
        lots_with_no_images = lots.filter(num_images=0)
        lots_with_one_image = lots.filter(num_images=1)
        lots_with_one_or_more_images = lots.filter(num_images__gt=1)
        medians = []
        averages = []
        counts = []
        for lots_subset in [
            lots_with_no_images,
            lots_with_one_image,
            lots_with_one_or_more_images,
        ]:
            try:
                medians.append(median_value(lots_subset, "winning_price"))
            except:
                medians.append(0)
            averages.append(lots_subset.aggregate(avg_value=Avg("winning_price"))["avg_value"])
            counts.append(lots_subset.count())
        return {
            "labels": ["No images", "One image", "More than one image"],
            "providers": ["Median sell price", "Average sell price", "Number of lots"],
            "data": [medians, averages, counts],
        }

    @property
    def get_stat_travel_distance(self):
        """Get travel distance chart data from cached stats"""
        if self.cached_stats and "travel_distance" in self.cached_stats:
            return self.cached_stats["travel_distance"]
        return {"labels": [], "providers": [], "data": []}

    def set_stat_travel_distance(self):
        """Calculate and return travel distance chart data"""
        auctiontos = AuctionTOS.objects.filter(auction=self, user__isnull=False)
        histogram = bin_data(
            auctiontos,
            "distance_traveled",
            number_of_bins=5,
            start_bin=1,
            end_bin=51,
            add_column_for_high_overflow=True,
        )
        return {
            "labels": [
                "1-10 miles",
                "11-20 miles",
                "21-30 miles",
                "31-40 miles",
                "41-50 miles",
                "51+ miles",
            ],
            "providers": ["Number of users"],
            "data": [histogram],
        }

    @property
    def get_stat_previous_auctions(self):
        """Get previous auctions chart data from cached stats"""
        if self.cached_stats and "previous_auctions" in self.cached_stats:
            return self.cached_stats["previous_auctions"]
        return {"labels": [], "providers": [], "data": []}

    def set_stat_previous_auctions(self):
        """Calculate and return previous auctions chart data"""
        auctiontos = AuctionTOS.objects.filter(auction=self, email__isnull=False)
        histogram = bin_data(
            auctiontos,
            "previous_auctions_count",
            number_of_bins=2,
            start_bin=0,
            end_bin=2,
            add_column_for_high_overflow=True,
        )
        return {
            "labels": ["First auction", "1 previous auction", "2+ previous auctions"],
            "providers": ["Number of users"],
            "data": [histogram],
        }

    @property
    def get_stat_lots_submitted(self):
        """Get lots submitted chart data from cached stats"""
        if self.cached_stats and "lots_submitted" in self.cached_stats:
            return self.cached_stats["lots_submitted"]
        return {"labels": [], "providers": [], "data": []}

    def set_stat_lots_submitted(self):
        """Calculate and return lots submitted chart data"""
        invoices = Invoice.objects.filter(auction=self)
        histogram = bin_data(
            invoices,
            "lots_sold",
            number_of_bins=4,
            start_bin=1,
            end_bin=9,
            add_column_for_low_overflow=True,
            add_column_for_high_overflow=True,
        )
        return {
            "labels": [
                "Buyer only (0 lots sold)",
                "1-2 lots",
                "3-4 lots",
                "5-6 lots",
                "7-8 lots",
                "9+ lots",
            ],
            "providers": ["Number of users"],
            "data": [histogram],
        }

    @property
    def get_stat_location_volume(self):
        """Get location volume chart data from cached stats"""
        if self.cached_stats and "location_volume" in self.cached_stats:
            return self.cached_stats["location_volume"]
        return {"labels": [], "providers": [], "data": []}

    def set_stat_location_volume(self):
        """Calculate and return location volume chart data"""
        locations = []
        sold = []
        bought = []
        for location in self.locations:
            locations.append(location.name)
            sold.append(location.total_sold)
            bought.append(location.total_bought)
        return {
            "labels": locations,
            "providers": ["Total bought", "Total sold"],
            "data": [bought, sold],
        }

    @property
    def get_stat_feature_use(self):
        """Get feature use chart data from cached stats"""
        if self.cached_stats and "feature_use" in self.cached_stats:
            return self.cached_stats["feature_use"]
        return {"labels": [], "providers": [], "data": []}

    def set_stat_feature_use(self):
        """Calculate and return feature use chart data"""
        auctiontos = AuctionTOS.objects.filter(auction=self)
        auctiontos_with_account = auctiontos.filter(user__isnull=False)
        searches = SearchHistory.objects.filter(user__isnull=False, auction=self).values("user").distinct().count()
        seach_percent = int(searches / auctiontos_with_account.count() * 100) if auctiontos_with_account.count() else 0
        watch_qs = Watch.objects.filter(lot_number__auction=self).values("user").distinct()
        watches = watch_qs.count()
        watch_percent = int(watches / auctiontos_with_account.count() * 100) if auctiontos_with_account.count() else 0
        notifications = (
            PushInformation.objects.filter(user__in=watch_qs, user__userdata__push_notifications_when_lots_sell=True)
            .values("user")
            .distinct()
            .count()
        )
        notification_percent = (
            int(notifications / auctiontos_with_account.count() * 100) if auctiontos_with_account.count() else 0
        )
        has_used_proxy_bidding = UserData.objects.filter(
            has_used_proxy_bidding=True,
            user__in=auctiontos_with_account.values_list("user"),
        ).count()
        has_used_proxy_bidding_percent = (
            int(has_used_proxy_bidding / auctiontos_with_account.count() * 100)
            if auctiontos_with_account.count()
            else 0
        )
        chat = (
            LotHistory.objects.filter(
                changed_price=False,
                lot__auction=self,
                user__in=auctiontos_with_account.values_list("user"),
            )
            .values("user")
            .distinct()
            .count()
        )
        chat_percent = int(chat / auctiontos_with_account.count() * 100) if auctiontos_with_account.count() else 0
        mobile_app = (
            auctiontos_with_account.filter(user__mobile_devices__isnull=False).values("user").distinct().count()
        )
        if self.is_online:
            lot_with_buy_now = (
                Lot.objects.filter(auction=self, buy_now_used=True).values("auctiontos_winner").distinct().count()
            )
        else:
            from django.db.models import F

            lot_with_buy_now = (
                Lot.objects.filter(auction=self, winning_price=F("buy_now_price"))
                .values("auctiontos_winner")
                .distinct()
                .count()
            )
        auctiontos_count = auctiontos.count()
        if auctiontos_count == 0:
            lot_with_buy_now_percent = 0
            account_percent = 0
            mobile_app_percent = 0
        else:
            account_percent = int(auctiontos_with_account.count() / auctiontos_count * 100)
            lot_with_buy_now_percent = int(lot_with_buy_now / auctiontos_count * 100)
            mobile_app_percent = int(mobile_app / auctiontos_count * 100)
        invoice_count = Invoice.objects.filter(auction=self).count()
        if invoice_count:
            viewed_invoices = Invoice.objects.filter(auction=self, opened=True).count()
            view_invoice_percent = int(viewed_invoices / invoice_count * 100)
        else:
            view_invoice_percent = 0
        sold_lots = Lot.objects.filter(auction=self, auctiontos_winner__isnull=False)
        leave_feedback = sold_lots.filter(~Q(feedback_rating=0)).values("auctiontos_winner").distinct().count()
        all_sold_lots = sold_lots.values("auctiontos_winner").distinct().count()
        if all_sold_lots == 0:
            leave_feedback_percent = 0
        else:
            leave_feedback_percent = int(leave_feedback / all_sold_lots * 100)
        return {
            "labels": [
                "An account",
                "Mobile app",
                "Search",
                "Watch",
                "Push notifications as lots sell",
                "Proxy bidding",
                "Chat",
                "Buy now",
                "View invoice",
                "Leave feedback for sellers",
            ],
            "providers": ["Percent of users"],
            "data": [
                [
                    account_percent,
                    mobile_app_percent,
                    seach_percent,
                    watch_percent,
                    notification_percent,
                    has_used_proxy_bidding_percent,
                    chat_percent,
                    lot_with_buy_now_percent,
                    view_invoice_percent,
                    leave_feedback_percent,
                ]
            ],
        }

    @property
    def page_views(self):
        """Every page view of this auction (rules page, lot list, lots).

        The OR across a join can't use an index, so callers need their own bound. Rows before 2026-09-09
        name only the lot; once ``tasks.backfill_page_view_auctions`` finishes this can be
        ``filter(auction=self)``.
        """
        return PageView.objects.filter(Q(auction=self) | Q(lot_number__auction=self))

    @cached_property
    def unique_views(self):
        """Distinct visitors to the rules page or lots: distinct users plus anonymous sessions that never appear
        with a user. Removes the structural double count of signing in. Returns total and breakdown.
        """
        all_views = self.page_views
        logged_in = all_views.filter(user__isnull=False).values("user").distinct().count()
        # Not exclude(session_id__in=subquery): MariaDB's NOT IN anti-join full-scanned PageView. Two
        # DISTINCT sets diffed in Python.
        user_sessions = set(
            all_views.filter(user__isnull=False, session_id__isnull=False)
            .values_list("session_id", flat=True)
            .distinct()
        )
        anonymous_sessions = set(
            all_views.filter(user__isnull=True, session_id__isnull=False)
            .values_list("session_id", flat=True)
            .distinct()
        )
        anonymous = len(anonymous_sessions - user_sessions)
        return {"total": logged_in + anonymous, "logged_in": logged_in, "anonymous": anonymous}

    def get_stat_misc(self):
        """Slow one-off stats that depend on page views."""
        if self.cached_stats and "misc" in self.cached_stats:
            return self.cached_stats["misc"]
        return {}

    def set_stat_misc(self):
        """Slow one-off stats that depend on page views."""

        unique_views = self.unique_views
        total_views = unique_views["total"]
        user_views = unique_views["logged_in"]
        anonymous_views = unique_views["anonymous"]

        total_bidders = User.objects.filter(bid__lot_number__auction=self).annotate(c=Count("id")).count()
        # Winners via auctiontos_winner: admin-declared winners have no User FK.
        total_winners = self.buyer_tos_qs.count()

        # Additional email/reminder stats
        reminder_emails_sent = self.number_of_reminder_emails
        reminder_email_click_rate = self.reminder_email_clicks
        reminder_email_join_rate = self.reminder_email_joins

        # QR code scans
        qr_scans = self.number_of_lots_with_scanned_qr

        if self.use_check_in_mode:
            participants = AuctionTOS.objects.filter(auction=self, checked_in__isnull=False).count()
        else:
            participants = (
                Invoice.objects.filter(auction=self)
                .exclude(auctiontos_user__isnull=True)
                .values("auctiontos_user")
                .distinct()
                .count()
            )

        return {
            "total_unique_views": total_views,
            "logged_in_unique_views": user_views,
            "anonymous_unique_views": anonymous_views,
            "total_bidders": total_bidders,
            "total_winners": total_winners,
            "reminder_emails_sent": reminder_emails_sent,
            "reminder_email_click_rate": reminder_email_click_rate,
            "reminder_email_join_rate": reminder_email_join_rate,
            "number_of_lots_with_scanned_qr": qr_scans,
            "club_stats": {
                "gross": self.gross,
                "total_lots": self.total_lots,
                "checked_in": participants,
            },
        }

    def _make_stats_json_serializable(self, obj):
        """Recursively convert Decimals to float for the JSONField."""
        if isinstance(obj, dict):
            return {k: self._make_stats_json_serializable(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._make_stats_json_serializable(item) for item in obj]
        if isinstance(obj, Decimal):
            return float(obj)
        return obj

    def recalculate_stats(self):
        """Recalculate and cache all auction statistics in cached_stats."""
        stats = {}

        # Call all setter methods to calculate stats
        stats["activity"] = self.set_stat_activity()
        stats["attrition"] = self.set_stat_attrition()
        stats["auctioneer_speed"] = self.set_stat_auctioneer_speed()
        stats["lot_sell_prices"] = self.set_stat_lot_sell_prices()
        stats["referrers"] = self.set_stat_referrers()
        stats["images"] = self.set_stat_images()
        stats["travel_distance"] = self.set_stat_travel_distance()
        stats["previous_auctions"] = self.set_stat_previous_auctions()
        stats["lots_submitted"] = self.set_stat_lots_submitted()
        stats["location_volume"] = self.set_stat_location_volume()
        stats["feature_use"] = self.set_stat_feature_use()
        stats["misc"] = self.set_stat_misc()

        # Decimals to float for the JSONField.
        self.cached_stats = self._make_stats_json_serializable(stats)
        self.last_stats_update = timezone.now()

        # Scheduling: within a week of the start every 4 hours, otherwise daily, never after 90 days.
        now = timezone.now()

        if self.date_start:
            days_until_start = (self.date_start - now).days
            days_since_start = (now - self.date_start).days

            # Auctions > 90 days in the past aren't recalculated at all
            if days_since_start > 90:
                self.next_update_due = None
            elif -7 <= days_until_start <= 7:
                self.next_update_due = now + timezone.timedelta(hours=4)
            # Other auctions - once per day
            else:
                self.next_update_due = now + timezone.timedelta(days=1)
        else:
            # No start date set - use daily updates
            self.next_update_due = now + timezone.timedelta(days=1)

        self.save(update_fields=["cached_stats", "last_stats_update", "next_update_due"])

        return stats

    def _get_activity_labels(self, bins, days_before, days_after, dates_messed_with):
        """Helper method to generate labels for activity chart"""
        if dates_messed_with:
            return [(f"{i - 1} days ago") for i in range(bins, 0, -1)]
        before = [(f"{i} days before") for i in range(days_before, 0, -1)]
        after = [(f"{i} days after") for i in range(1, days_after)]
        midpoint = "start"
        if self.is_online:
            midpoint = "end"
        return before + [midpoint] + after

    def create_history(self, applies_to, action="Edited", user=None, form=None):
        """Record auction history. ``applies_to``: RULES, USERS, INVOICES, LOTS, STATS; ``user`` is the actor
        or None; ``form`` supplies changed data.
        """
        # Don't create history if the auction hasn't been saved yet
        if not self.pk:
            return
        changed_fields = history.changed_field_summary(form)
        if form:
            action += " "
            for field_name in form.changed_data:
                action += history.field_label(form, field_name)
                action += ", "
            action = action[:-2]  # remove the last comma and space
        if len(action) > 800:
            action = action[:797] + "..."
        AuctionHistory.objects.create(
            auction=self,
            user=user,
            action=action[:800],
            applies_to=applies_to,
            changed_fields=changed_fields,
        )


class PickupLocation(InvalidatesRelatedCache, CachedPropertiesMixin, models.Model):
    """A pickup location for an auction; an auction can have several."""

    # Auction.locations, and the dozen properties derived from it
    invalidates_cache_on = ("auction",)

    name = models.CharField(max_length=70, default="", blank=True, null=True)
    name.help_text = "Location name shown to users.  e.x. University Mall in VT"
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    auction = models.ForeignKey(Auction, null=True, blank=True, on_delete=models.CASCADE)
    description = models.CharField(max_length=300, blank=True, null=True)
    description.help_text = "Notes, shipping charges, etc.  For example: 'Parking lot near Sears entrance'"
    users_must_coordinate_pickup = models.BooleanField(default=False)
    users_must_coordinate_pickup.help_text = (
        "You probably want this unchecked, to have everyone arrive at the same time."
    )
    pickup_location_contact_name = models.CharField(
        max_length=200, blank=True, null=True, verbose_name="Contact person's name"
    )
    pickup_location_contact_name.help_text = (
        "Name of the person coordinating this pickup location.  Contact info is only shown to logged in users."
    )
    pickup_location_contact_phone = models.CharField(
        max_length=200, blank=True, null=True, verbose_name="Contact person's phone"
    )
    pickup_location_contact_email = models.CharField(
        max_length=200, blank=True, null=True, verbose_name="Contact person's email"
    )
    pickup_time = models.DateTimeField(blank=True, null=True)
    second_pickup_time = models.DateTimeField(blank=True, null=True)
    second_pickup_time.help_text = "Only for <a href='/blog/multiple-location-auctions/'>multi-location auctions</a>; people will return to pick up lots from other locations at this time."
    latitude = models.FloatField(blank=True, default=0)
    longitude = models.FloatField(blank=True, default=0)
    address = models.CharField(max_length=500, blank=True, null=True)
    address.help_text = "Enter an address to search the map below.  What you enter here won't be shown to users."
    location_coordinates = PlainLocationField(based_fields=["address"], blank=True, null=True, verbose_name="Map")
    allow_selling_by_default = models.BooleanField(default=True)
    allow_selling_by_default.help_text = "This is not used"
    allow_bidding_by_default = models.BooleanField(default=True)
    allow_bidding_by_default.help_text = "This is not used"
    pickup_by_mail = models.BooleanField(default=False)
    pickup_by_mail.help_text = "Special pickup location without an actual location"
    is_default = models.BooleanField(default=False)
    is_default.help_text = "This was a default location added for an in-person auction."
    contact_person = models.ForeignKey("AuctionTOS", null=True, blank=True, on_delete=models.SET_NULL)
    contact_person.help_text = "Only users that you have granted admin permissions to will show up here.  Their phone and email will be shown to users who select this location."

    def __str__(self):
        if self.pickup_by_mail:
            return "Mail me my lots"
        return self.name

    @property
    def short_name(self):
        if self.pickup_by_mail:
            return "Mail"
        words = self.name.split()
        abbreviation = ""
        for word in words:
            abbreviation += word[0].upper()
        return abbreviation

    @property
    def directions_link(self):
        """Google maps link to the lat and lng of this pickup location"""
        if self.has_coordinates:
            return f"https://www.google.com/maps/search/?api=1&query={self.latitude},{self.longitude}"
        return ""

    @property
    def has_coordinates(self):
        """Return True if this should be included on the auctions map list"""
        if self.latitude and self.longitude:
            return True
        return False

    @property
    def user_list(self):
        """All auctiontos associated with this location"""
        return AuctionTOS.objects.filter(pickup_location=self.pk)

    @cached_property
    def number_of_users(self):
        """How many people have chosen this pickup location?"""
        return self.user_list.count()

    @property
    def incoming_lots(self):
        """Queryset of all lots destined for this location"""
        return Lot.objects.filter(
            auctiontos_winner__pickup_location__pk=self.pk,
            is_deleted=False,
            banned=False,
        )

    @property
    def outgoing_lots(self):
        """Queryset of all lots coming from this location"""
        lots = Lot.objects.filter(
            auctiontos_seller__pickup_location__pk=self.pk,
            is_deleted=False,
            banned=False,
            auctiontos_winner__isnull=False,
        )
        return lots

    @cached_property
    def number_of_incoming_lots(self):
        return self.incoming_lots.count()

    @cached_property
    def number_of_outgoing_lots(self):
        return self.outgoing_lots.count()

    @cached_property
    def email_list(self):
        """All emails at this location, for bcc."""
        return "".join(f"{tos.email}, " for tos in self.user_list.only("email") if tos.email)

    @cached_property
    def total_sold(self):
        lots = self.outgoing_lots.aggregate(total_winning_price=Sum("winning_price"))
        return lots["total_winning_price"] or 0

    @cached_property
    def total_bought(self):
        lots = self.incoming_lots.aggregate(total_winning_price=Sum("winning_price"))
        return lots["total_winning_price"] or 0


class AuctionIgnore(models.Model):
    """If a user does not want to participate in an auction, create one of these"""

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    auction = models.ForeignKey(Auction, on_delete=models.CASCADE)
    createdon = models.DateTimeField(auto_now_add=True, blank=True)

    def __str__(self):
        return f"{self.user} ignoring {self.auction}"

    class Meta:
        verbose_name = "User ignoring auction"
        verbose_name_plural = "User ignoring auction"


class AuctionTOS(InvalidatesRelatedCache, CachedPropertiesMixin, models.Model):
    """How a person engages with an auction; the basis of the admin users view. Usually one person, who may
    or may not have an account.
    """

    # the auction caches its participant counts
    invalidates_cache_on = ("auction",)

    user = models.ForeignKey(User, on_delete=models.SET_NULL, blank=True, null=True)
    auction = models.ForeignKey(Auction, on_delete=models.CASCADE)
    pickup_location = models.ForeignKey(PickupLocation, on_delete=models.CASCADE)
    createdon = models.DateTimeField(auto_now_add=True, blank=True)
    confirm_email_sent = models.BooleanField(default=False, blank=True)
    second_confirm_email_sent = models.BooleanField(default=False, blank=True)
    print_reminder_email_sent = models.BooleanField(default=False, blank=True)
    is_admin = models.BooleanField(
        default=False,
        verbose_name="Grant admin permissions to help run this auction",
        blank=True,
        db_index=True,
    )
    # A string on purpose: bidder numbers may one day contain characters.
    bidder_number = models.CharField(max_length=20, default="", blank=True, db_index=True)
    bidder_number.help_text = "Must be unique, blank to automatically generate"
    bidding_allowed = models.BooleanField(default=True, blank=True)
    selling_allowed = models.BooleanField(default=True, blank=True)
    name = models.CharField(max_length=181, null=True, blank=True, db_index=True)
    email = models.EmailField(null=True, blank=True, db_index=True)
    EMAIL_ADDRESS_STATUSES = (
        ("BAD", "Invalid"),
        ("UNKNOWN", "Unknown"),
        ("VALID", "Verified"),
    )
    email_address_status = models.CharField(
        max_length=20, choices=EMAIL_ADDRESS_STATUSES, default="UNKNOWN", blank=True
    )
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    address = models.CharField(max_length=500, blank=True, null=True)
    manually_added = models.BooleanField(default=False, blank=True, null=True)
    time_spent_reading_rules = models.PositiveIntegerField(validators=[MinValueValidator(0)], blank=True, default=0)
    is_club_member = models.BooleanField(default=False, blank=True, verbose_name="Club member")
    memo = models.CharField(max_length=500, blank=True, null=True, default="")
    memo.help_text = "Only other auction admins can see this"
    possible_duplicate = models.ForeignKey(
        "AuctionTOS", on_delete=models.SET_NULL, related_name="duplicate", blank=True, null=True
    )
    possible_duplicate.help_text = "There's a chance this user is a duplicate if this is set"
    add_to_calendar = models.CharField(max_length=20, blank=True, null=True)
    checked_in = models.DateTimeField(blank=True, null=True, default=None)
    door_prize_called = models.DateTimeField(blank=True, null=True, default=None)
    clubmember = models.ForeignKey(
        "ClubMember",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="auction_tos_records",
        help_text="When the auction is managed through its club, links this record to the ClubMember that owns the bidder_number and permissions.",
    )

    @property
    def phone_as_string(self):
        """Add proper dashes to phone"""
        if not self.phone_number:
            return ""
        n = re.sub(r"\D", "", self.phone_number)
        if len(n) == 10:
            return f"{n[:3]}-{n[3:6]}-{n[6:]}"
        return n

    @property
    def bulk_add_link_html(self):
        """Link to add multiple lots at once for this user"""
        url = reverse(
            "bulk_add_lots_auto",
            kwargs={"bidder_number": self.bidder_number, "slug": self.auction.slug},
        )
        if not self.selling_allowed:
            icon = '<i class="text-danger me-1 bi bi-cash-coin" title="Selling not allowed"></i>'
        else:
            icon = "<i class='bi bi-calendar-plus me-1'></i>"
        return html.format_html(f"<a href='{url}' hx-noget>{icon} Add lots</a>")

    @property
    def bought_lots_qs(self):
        lots = Lot.objects.exclude(is_deleted=True).filter(auctiontos_winner=self.pk, auction__isnull=False)
        return lots

    @property
    def lots_qs(self):
        lots = Lot.objects.exclude(is_deleted=True).filter(auctiontos_seller=self.pk, auction__isnull=False)
        return lots

    @staticmethod
    def annotate_lot_counts(queryset, auction=None):
        """Add per-person lot counts for the users table as subqueries (separate joins would explode rows).
        Pass ``auction`` to apply its label printing rule.
        """
        lots = Lot.objects.exclude(is_deleted=True).filter(auction__isnull=False)

        def count_of(field, **extra):
            return Subquery(
                lots.filter(**{field: OuterRef("pk")}, **extra)
                .order_by()
                .values(field)
                .annotate(total=Count("pk"))
                .values("total")[:1],
                output_field=IntegerField(),
            )

        unprinted = {"banned": False, "label_printed": False}
        printable = {"banned": False}
        if auction is not None and auction.is_online:
            # Online auctions print labels only for sold lots.
            sold = {"auctiontos_winner__isnull": False, "winning_price__isnull": False}
            unprinted |= sold
            printable |= sold
        return queryset.annotate(
            annotated_lots_count=Coalesce(count_of("auctiontos_seller"), Value(0)),
            annotated_bought_lots_count=Coalesce(count_of("auctiontos_winner"), Value(0)),
            annotated_unbanned_lot_count=Coalesce(count_of("auctiontos_seller", banned=False), Value(0)),
            annotated_unprinted_label_count=Coalesce(count_of("auctiontos_seller", **unprinted), Value(0)),
            annotated_print_labels_count=Coalesce(count_of("auctiontos_seller", **printable), Value(0)),
        )

    @cached_property
    def bought_lots_count(self):
        """Lots this person won, from the annotation when present."""
        annotated = getattr(self, "annotated_bought_lots_count", None)
        return self.bought_lots_qs.count() if annotated is None else annotated

    @cached_property
    def lots_count(self):
        """Lots this person is selling. Annotation first, same as bought_lots_count."""
        annotated = getattr(self, "annotated_lots_count", None)
        return self.lots_qs.count() if annotated is None else annotated

    def lot_owner(self, added_by=None):
        """The account for `Lot.user`: `self.user`, else `added_by` when the adder is demonstrably this seller
        (same email). An admin adding for someone else is never the owner.
        """
        if self.user:
            return self.user
        if not added_by or not added_by.is_authenticated or not self.email:
            return None
        if normalize_email(self.email) == normalize_email(added_by.email):
            return added_by
        return None

    @property
    def unbanned_lot_qs(self):
        return self.lots_qs.exclude(banned=True)

    @cached_property
    def unbanned_lot_count(self):
        annotated = getattr(self, "annotated_unbanned_lot_count", None)
        return self.unbanned_lot_qs.count() if annotated is None else annotated

    @cached_property
    def self_submitted_unbanned_lot_count(self):
        """Count of unbanned lots that this user added themselves (not added by admin)"""
        return self.unbanned_lot_qs.filter(added_by=self.user).count()

    @property
    def print_labels_qs(self):
        """A set of rules to determine what we print"""
        lots = self.unbanned_lot_qs
        if self.auction.is_online:
            lots = lots.filter(auctiontos_winner__isnull=False, winning_price__isnull=False)
        return lots

    @property
    def unprinted_labels_qs(self):
        return self.print_labels_qs.exclude(label_printed=True)

    @cached_property
    def unprinted_label_count(self):
        annotated = getattr(self, "annotated_unprinted_label_count", None)
        if annotated is not None:
            return annotated
        return self.unprinted_labels_qs.count()

    @cached_property
    def print_labels_link_html(self):
        if self.unbanned_lot_count:
            url = reverse(
                "print_labels_by_bidder_number",
                kwargs={"bidder_number": self.bidder_number, "slug": self.auction.slug},
            )
            return f"<a href='{url}'><i class='bi bi-tags me-1'></i>Print labels</a>"
        return ""

    @cached_property
    def print_labels_count(self):
        annotated = getattr(self, "annotated_print_labels_count", None)
        if annotated is not None:
            return annotated
        return self.print_labels_qs.count()

    @cached_property
    def print_unprinted_labels_link_html(self):
        if self.unprinted_label_count and self.unprinted_label_count != self.print_labels_count:
            unprinted_url = reverse(
                "print_unprinted_labels_by_bidder_number",
                kwargs={"bidder_number": self.bidder_number, "slug": self.auction.slug},
            )
            return f"<a href='{unprinted_url}'>Print only {self.unprinted_label_count} unprinted labels</a>"
        return ""

    @cached_property
    def print_labels_html(self):
        """For use in HTMX users table; print lot labels for this user"""
        if self.unbanned_lot_count:
            result = self.print_labels_link_html
            if self.print_unprinted_labels_link_html:
                result += f"""
                <button type="button" class="btn btn-sm btn-primary dropdown-toggle dropdown-toggle-split" data-bs-toggle="dropdown" aria-haspopup="true" aria-expanded="false">
                </button>
                <div class="dropdown-menu">
                    <span class='dropdown-item'>{self.print_unprinted_labels_link_html}</span>
                </div>"""
            return html.format_html(result)
        return ""

    @cached_property
    def actions_dropdown_html(self):
        show_on_mobile_string = "d-md-none"
        result = f"""<button type='button' class='btn btn-sm btn-primary dropdown-toggle dropdown-toggle-split' data-bs-toggle='dropdown'
        aria-haspopup='true' aria-expanded='false'>Actions </button>
        <div class = "dropdown-menu" id='actions_dropdown'>
        <span class='dropdown-item {show_on_mobile_string}'>{self.bulk_add_link_html}</span>"""
        if self.invoice_link_html:
            result += f"<span class='dropdown-item {show_on_mobile_string}'>{self.invoice_link_html}</span>"
        if self.print_labels_link_html:
            result += f"<span class='dropdown-item {show_on_mobile_string}'>{self.print_labels_link_html}</span>"
        if self.print_unprinted_labels_link_html:
            result += (
                f"<span class='dropdown-item {show_on_mobile_string}'>{self.print_unprinted_labels_link_html}</span>"
            )
        if self.email:
            email_url = f"mailto:{self.email}"
            icon_class = "bi bi-envelope"
            if self.email_address_status == "BAD":
                icon_class = "bi bi-envelope-exclamation-fill text-danger"
            if self.email_address_status == "VALID":
                icon_class = "bi bi-envelope-check-fill"
            result += (
                f"<span class='dropdown-item'><a href={email_url}><i class='{icon_class} me-1'></i>Email</a></span>"
            )
        won_lots_url = (
            reverse("auction_lot_list", kwargs={"slug": self.auction.slug}) + f"?query=winner%3A{self.bidder_number}"
        )
        result += f"<span class='dropdown-item'><a href={won_lots_url}><i class='bi bi bi-calendar-check me-1'></i>View {self.bought_lots_count} lots won</a></span>"
        sold_lots_url = (
            reverse("auction_lot_list", kwargs={"slug": self.auction.slug}) + f"?query=seller%3A{self.bidder_number}"
        )

        result += f"<span class='dropdown-item'><a href={sold_lots_url}><i class='bi bi-calendar me-1'></i>View {self.lots_count} lots sold</a></span>"
        delete_url = reverse("auctiontosdelete", kwargs={"pk": self.pk})
        merge_url = f"{delete_url}?action=merge"
        result += (
            f"<span class='dropdown-item'><a href={merge_url}><i class='bi bi-people me-1'></i>Merge with...</a></span>"
        )
        result += f"<span class='dropdown-item'><a href={delete_url}><i class='bi bi-person-fill-x me-1'></i>Delete</a></span>"
        problems_url = reverse(
            "auction_no_show",
            kwargs={
                "slug": self.auction.slug,
                "tos": self.bidder_number,
            },
        )
        result += f"<span class='dropdown-item'><a href={problems_url}><i class='bi bi-exclamation-circle me-1'></i>Problems</a></span>"
        bulk_add_images_url = reverse(
            "bulk_add_image",
            kwargs={
                "slug": self.auction.slug,
                "bidder_number": self.bidder_number,
            },
        )
        result += f"<span class='dropdown-item {show_on_mobile_string}'><a href={bulk_add_images_url}><i class='bi bi-file-image me-1'></i>Quick add images</a></span>"
        # Club-managed: surface membership actions here, so the users list doubles as the member list.
        if self.auction.is_club_managed and self.clubmember_id:
            club = self.auction.club
            cm = self.clubmember
            result += "<div class='dropdown-divider'></div>"
            if club.membership_annual_fee:
                renew_url = reverse("club_member_renew", kwargs={"pk": cm.pk})
                set_expiry_url = reverse("club_member_renew_page", kwargs={"slug": club.slug, "pk": cm.pk})
                result += (
                    f"<span class='dropdown-item'><a href='javascript:void(0)' hx-get='{renew_url}' "
                    f"hx-target='#modals-here'><i class='bi bi-calendar-check me-1'></i>Renew membership</a></span>"
                    f"<span class='dropdown-item'><a href='{set_expiry_url}'>"
                    f"<i class='bi bi-calendar-range me-1'></i>Set expiration date</a></span>"
                )
            if club.show_member_barcode:
                membership_number_url = reverse("club_member_membership_number", kwargs={"pk": cm.pk})
                result += (
                    f"<span class='dropdown-item'><a href='javascript:void(0)' hx-get='{membership_number_url}' "
                    f"hx-target='#modals-here'><i class='bi bi-credit-card-2-front me-1'></i>Membership number</a></span>"
                )
                if not cm.is_deleted:
                    resend_card_url = reverse("club_member_confirm", kwargs={"pk": cm.pk, "action": "resend_card"})
                    result += (
                        f"<span class='dropdown-item'><a href='javascript:void(0)' hx-get='{resend_card_url}' "
                        f"hx-target='#modals-here'><i class='bi bi-send me-1'></i>Resend membership card</a></span>"
                    )
            # Deactivating the member differs from deleting them from this auction; offer both, as the
            # club page does.
            if cm.is_deleted:
                reactivate_url = reverse("club_member_reactivate", kwargs={"pk": cm.pk})
                result += (
                    f"<span class='dropdown-item'><a href='javascript:void(0)' hx-post='{reactivate_url}' "
                    f"hx-target='#modals-here' hx-swap='innerHTML'>"
                    f"<i class='bi bi-person-check me-1'></i>Reactivate club member</a></span>"
                )
            else:
                deactivate_url = reverse("club_member_confirm", kwargs={"pk": cm.pk, "action": "delete"})
                result += (
                    f"<span class='dropdown-item'><a href='javascript:void(0)' hx-get='{deactivate_url}' "
                    f"hx-target='#modals-here'>"
                    f"<i class='bi bi-person-dash me-1'></i>Deactivate club member</a></span>"
                )
        if self.auction.club and not self.auction.is_club_managed:
            club = self.auction.club
            already_in_club = False
            if self.email:
                already_in_club = ClubMember.objects.filter(
                    club=club, email__iexact=self.email, is_deleted=False
                ).exists()
            if not already_in_club and self.user_id:
                already_in_club = ClubMember.objects.filter(club=club, user_id=self.user_id, is_deleted=False).exists()
            club_name = html.escape(club.name)
            if already_in_club:
                result += f"<span class='dropdown-item text-muted'><i class='bi bi-person-check me-1'></i>Already in {club_name}</span>"
            else:
                add_to_club_url = reverse("add_single_auctiontos_to_club", kwargs={"pk": self.pk})
                result += (
                    f"<span class='dropdown-item'>"
                    f"<a href='javascript:void(0)' hx-post='{add_to_club_url}' hx-swap='none'>"
                    f"<i class='bi bi-person-fill-add me-1'></i>Add to {club_name}</a></span>"
                )
        result += "</div>"
        return html.format_html(result)

    @cached_property
    def invoice(self):
        """This person's invoice for this auction, or None, from the reverse relation (prefetchable)."""
        invoices = self.auctiontos.all()
        if invoices._result_cache is None:
            return invoices.order_by("-date").first()
        return max(invoices, key=lambda invoice: invoice.date, default=None)

    @cached_property
    def club_member_record(self):
        """The ClubMember in the auction's club: the direct link, else by user and email. Or None."""
        if not self.auction.club_id:
            return None
        if self.clubmember and not self.clubmember.is_deleted:
            return self.clubmember
        member = None
        if self.user_id:
            member = ClubMember.objects.filter(
                club_id=self.auction.club_id, user_id=self.user_id, is_deleted=False
            ).first()
        email = (self.email or "").strip()
        if not member and email:
            member = ClubMember.objects.filter(
                club_id=self.auction.club_id, email__iexact=email, is_deleted=False
            ).first()
        return member

    def update_alternate_split_from_membership(self, invoice=None):
        """In club-member-discount mode, keep ``is_club_member`` in step with paid (or renewing) membership and
        recalculate the invoice on change. True if changed.
        """
        if self.auction.alternate_split_mode != "club_member":
            return False
        invoice = invoice or self.invoice
        if invoice:
            should_apply = invoice.treat_as_club_member
        else:
            member = self.club_member_record
            should_apply = bool(member and member.is_paid_member)
        if self.is_club_member != should_apply:
            self.is_club_member = should_apply
            self.save(update_fields=["is_club_member"])
            if invoice:
                invoice.recalculate()
            return True
        return False

    @property
    def requires_check_in_before_bidding(self):
        return self.auction.use_check_in_mode and self.checked_in is None

    @property
    def can_bid_in_auction(self):
        return self.bidding_allowed and not self.requires_check_in_before_bidding

    @cached_property
    def invoice_link_html(self):
        """A link to this person's invoice, or a create link."""
        if self.invoice:
            status = "bag"
            if self.invoice.status == "UNPAID":
                status = "bag-check"
            if self.invoice.status == "PAID":
                status = "bag-heart text-success"
            return html.format_html(
                f"<a href='{self.invoice.get_absolute_url()}' hx-noget><i class='bi bi-{status} me-1'></i>View<span class='d-sm-inline d-md-none'> invoice</span></a>"
            )
        else:
            # Show create link for admins
            create_url = reverse("create_invoice", kwargs={"pk": self.pk})
            return html.format_html(
                f"<a href='{create_url}' hx-noget><i class='bi bi-plus me-1'></i>Create<span class='d-sm-inline d-md-none'> invoice</span></a>"
            )

    @property
    def gross_sold(self):
        """Before club cut"""
        if self.invoice:
            return self.invoice.total_sold_gross or 0
        return 0

    @property
    def total_club_cut(self):
        """Total amount of profit this user brought to the club"""
        if self.invoice:
            return self.invoice.total_sold_club_cut
        return 0

    def save(self, *args, **kwargs):
        # Normalize a real email; leave None/"" alone (the "no email" filter uses email__isnull).
        if self.email:
            self.email = normalize_email(self.email)
        if not self.pk:
            # logger.debug("new instance of auctionTOS")
            if self.auction.only_approved_sellers:
                self.selling_allowed = False
            if self.auction.only_approved_bidders:
                # default
                self.bidding_allowed = False
                if self.manually_added:
                    # anyone manually added can bid
                    self.bidding_allowed = True
                else:
                    if self.user:
                        user_has_participated_before = AuctionTOS.objects.filter(
                            user=self.user,
                            auction__created_by__pk__in=self.auction.auction_admins_pks,
                            auctiontos__status="PAID",
                        ).first()
                        if user_has_participated_before:
                            self.bidding_allowed = True
            # no emails for in-person auctions, thankyouverymuch
            if not self.auction.is_online:
                pass
            if self.email and not self.user:
                self.user = User.objects.filter(is_active=True, email=self.email).first()
        # Only on creation: don't copy user details, so adding public emails can't harvest data. See
        # user_logged_in_callback in signals.py. Then set a bidder number.
        if not self.bidder_number or self.bidder_number == "None":
            last_used = None
            if self.user or self.email:
                query = Q()
                if self.user:
                    query |= Q(user=self.user)
                if self.email:
                    query |= Q(email=self.email)
                last_obj = (
                    AuctionTOS.objects.filter(query, auction__created_by=self.auction.created_by)
                    .exclude(pk=self.pk)
                    .order_by("-createdon")
                    .first()
                )
                if last_obj:
                    last_used = last_obj.bidder_number

            user_data = None
            preferred = None
            if self.user:
                user_data = self.user.userdata
                preferred = user_data.preferred_bidder_number or None

            self.bidder_number = _generate_unique_bidder_number(
                is_taken=lambda n: (
                    AuctionTOS.objects.filter(bidder_number=n, auction=self.auction).exclude(pk=self.pk or 0).exists()
                ),
                preferred=preferred,
                phone=self.phone_number,
                address=self.address,
                last_used=last_used,
            )
            if (
                user_data
                and not user_data.preferred_bidder_number
                and self.bidder_number
                and self.bidder_number != "ERROR"
            ):
                user_data.preferred_bidder_number = self.bidder_number
                # Write only this field: the cached UserData may be stale and a full save reverts others.
                UserData.objects.filter(pk=user_data.pk).update(preferred_bidder_number=self.bidder_number)
        if not self.bidder_number:
            # I don't ever want this to be null
            self.bidder_number = "ERROR"
        if str(self.memo) == "None":
            self.memo = ""
        # Email changes reset the email status.
        if not self.name:
            self.name = "Unknown"
        if self.pk:
            saved_tos = AuctionTOS.objects.filter(pk=self.pk).first()
            if saved_tos and saved_tos.email != self.email:
                self.email_address_status = "UNKNOWN"
                # Unlink only on a real change to an address the linked user doesn't own.
                user_owns_new_email = bool(
                    self.user and self.user.email and normalize_email(self.user.email) == self.email
                )
                if not self.manually_added and saved_tos.email and not user_owns_new_email:
                    # Unlink so a later join can link the right user.
                    self.user = None
        # if this is a known address, update the status
        if self.email and self.email_address_status == "UNKNOWN":
            existing_instance = (
                AuctionTOS.objects.exclude(email_address_status="UNKNOWN")
                .filter(
                    email=self.email,
                    auction__created_by=self.auction.created_by,
                )
                .order_by("-createdon")
                .first()
            )
            if existing_instance:
                self.email_address_status = existing_instance.email_address_status

        # Check for and remove forward slashes in bidder_number
        if self.bidder_number and "/" in self.bidder_number:
            original_bidder_number = self.bidder_number
            self.bidder_number = self.bidder_number.replace("/", "")

            existing_tos = AuctionTOS.objects.filter(bidder_number=self.bidder_number, auction=self.auction)
            if self.pk:
                existing_tos = existing_tos.exclude(pk=self.pk)

            if existing_tos.exists():
                # If there would be a conflict, append a suffix to make it unique
                suffix = 1
                base_bidder_number = self.bidder_number
                while existing_tos.exists() and suffix < 100:
                    self.bidder_number = f"{base_bidder_number}{suffix}"
                    existing_tos = AuctionTOS.objects.filter(bidder_number=self.bidder_number, auction=self.auction)
                    if self.pk:
                        existing_tos = existing_tos.exclude(pk=self.pk)
                    suffix += 1

            # Create auction history entry after save
            needs_history = True
        else:
            needs_history = False

        super().save(*args, **kwargs)

        # Create history entry after save (needs pk to exist)
        if needs_history:
            self.auction.create_history(
                applies_to="USERS",
                action=f"Removed '/' character from bidder number for {self.name}. Changed from '{original_bidder_number}' to '{self.bidder_number}'. The '/' character is not allowed in bidder numbers.",
                user=None,  # System change
            )

        # Exact email duplicate: merge now. iexact matches unnormalized existing rows.
        if self.email:
            email_duplicate = (
                AuctionTOS.objects.filter(auction=self.auction, email__iexact=self.email)
                .exclude(pk=self.pk)
                .order_by("createdon")
                .first()
            )
            if email_duplicate:
                # Keep the older record; merge_duplicate preserves self's non-empty fields.
                email_duplicate.merge_duplicate(self, reason="same email")
                return

        # Name matches are flagged for review, not merged.
        duplicate_instance = self.auction.find_user(name=self.name, email="", exclude_pk=self.pk)
        if duplicate_instance:
            # update() avoids recursion.
            AuctionTOS.objects.filter(pk=self.pk).update(possible_duplicate=duplicate_instance.pk)
            AuctionTOS.objects.filter(pk=duplicate_instance.pk).update(possible_duplicate=self.pk)
            # Keep in sync with update().
            self.possible_duplicate = duplicate_instance
        else:
            # Use the id: the flagged row may have been merged away.
            if self.possible_duplicate_id:
                # remove ourselves from the duplicate if it was previously set
                AuctionTOS.objects.filter(pk=self.possible_duplicate_id).update(possible_duplicate=None)
                AuctionTOS.objects.filter(pk=self.pk).update(possible_duplicate=None)
                self.possible_duplicate = None

        # The same user's other row in this auction: keep the older, merge this one in.
        if self.user:
            existing = (
                AuctionTOS.objects.filter(user=self.user, auction=self.auction)
                .exclude(pk=self.pk)
                .order_by("createdon")
                .first()
            )
            if existing:
                existing.merge_duplicate(self, reason="same user account")
                return

        if self.user:
            related_campaign = (
                AuctionCampaign.objects.filter(auction=self.auction, user=self.user).exclude(result="JOINED").first()
            )
            if related_campaign:
                related_campaign.result = "JOINED"
                related_campaign.save()

    @cached_property
    def display_name_for_admins(self):
        """Same as display name, but no anonymous option"""
        if self.auction.is_online:
            if self.user and not self.manually_added:
                return self.user.username
        if self.bidder_number:
            return self.bidder_number
        return "Unknown user"

    @cached_property
    def display_name(self):
        """Usernames for online auctions, bidder numbers in person."""
        if self.auction.is_online:
            if self.user and not self.manually_added:
                userData = self.user.userdata
                if userData.username_visible:
                    return self.user.username
                else:
                    return "Anonymous"
        if self.bidder_number:
            return self.bidder_number
        return "Unknown user"

    def __str__(self):
        return self.display_name

    class Meta:
        verbose_name = "User in auction"
        verbose_name_plural = "Users in auction"

    def force_set_bidder_number(self, number, via_barcode=False, acting_user=None):
        """Assign *number* to this TOS, first renumbering any holder in the auction. Logs history; saves via
        update_fields.

        In club-managed mode the number belongs to the member, so this hands off to
        ``services.set_member_bidder_number``.
        """
        from django.db import transaction as _tx

        number = str(number).strip()
        if not number:
            return
        if self.clubmember_id and self.auction.is_club_managed:
            from .services import set_member_bidder_number

            with _tx.atomic():
                set_member_bidder_number(self.clubmember, number, acting_user=acting_user)
                self.bidder_number = number
                source = " via barcode" if via_barcode else ""
                self.auction.create_history(
                    applies_to="USERS",
                    action=f"Assigned bidder number {number} to {self.name}{source}",
                    user=acting_user,
                )
            return
        with _tx.atomic():
            conflicting = (
                AuctionTOS.objects.filter(auction=self.auction, bidder_number=number).exclude(pk=self.pk).first()
            )
            if conflicting:
                new_number = _generate_unique_bidder_number(
                    is_taken=lambda n: (
                        AuctionTOS.objects.filter(bidder_number=n, auction=self.auction)
                        .exclude(pk=conflicting.pk)
                        .exists()
                        or n == number
                    ),
                    phone=conflicting.phone_number,
                    address=conflicting.address,
                )
                AuctionTOS.objects.filter(pk=conflicting.pk).update(bidder_number=new_number)
            self.bidder_number = number
            AuctionTOS.objects.filter(pk=self.pk).update(bidder_number=number)
            source = " via barcode" if via_barcode else ""
            self.auction.create_history(
                applies_to="USERS",
                action=f"Assigned bidder number {number} to {self.name}{source}",
                user=acting_user,
            )

    def merge_duplicate(self, duplicate, reason="same email", user=None, preserve_missing_fields=True):
        """Merge a duplicate AuctionTOS into self (the older record): move lots, adjustments and payments,
        keep missing fields, log history, delete the duplicate. Club-managed auctions also merge ClubMembers.
        ``user`` for admin-triggered merges.
        """
        if self.pk is None or duplicate.pk is None:
            # Both must be saved; an unsaved one means a save-time merge already deleted it.
            msg = "Cannot merge AuctionTOS records that have not been saved (or were already deleted)."
            raise ValueError(msg)
        if duplicate == self:
            msg = "Cannot merge an AuctionTOS record with itself."
            raise ValueError(msg)
        if duplicate.auction != self.auction:
            msg = "Cannot merge AuctionTOS records from different auctions."
            raise ValueError(msg)
        # Keep duplicate's values where self has none (explicit None/"" check).
        if preserve_missing_fields:
            fields_to_preserve = [
                "user",
                "name",
                "email",
                "memo",
                "address",
                "phone_number",
                "bidder_number",
                "clubmember",
            ]
            updates = {}
            for field in fields_to_preserve:
                self_val = getattr(self, field, None)
                dup_val = getattr(duplicate, field, None)
                if (self_val is None or self_val == "") and dup_val:
                    updates[field] = dup_val
                    setattr(self, field, dup_val)
            if updates:
                AuctionTOS.objects.filter(pk=self.pk).update(**updates)
        # Move won lots to self
        Lot.objects.filter(auctiontos_winner=duplicate).update(auctiontos_winner=self)
        # Move sold lots to self
        Lot.objects.filter(auctiontos_seller=duplicate).update(auctiontos_seller=self)
        # Get or create an invoice for self
        invoice = Invoice.objects.filter(auctiontos_user=self).first()
        if not invoice:
            invoice = Invoice.objects.create(auctiontos_user=self, auction=self.auction)
        duplicate_invoice = Invoice.objects.filter(auctiontos_user=duplicate).first()
        if duplicate_invoice:
            InvoiceAdjustment.objects.filter(invoice=duplicate_invoice).update(invoice=invoice)
            InvoicePayment.objects.filter(invoice=duplicate_invoice).update(invoice=invoice)
        invoice.recalculate()
        merge_action = f"Merged {duplicate.name} (bidder #{duplicate.bidder_number}) into {self.name} (bidder #{self.bidder_number}): {reason}"
        if self.auction.is_club_managed and self.auction.club_id:
            self_club_member = self.clubmember
            dup_club_member = duplicate.clubmember
            if dup_club_member and dup_club_member != self_club_member:
                if self_club_member:
                    # Move other TOS rows pointing at the duplicate ClubMember.
                    AuctionTOS.objects.filter(clubmember=dup_club_member).exclude(pk=duplicate.pk).update(
                        clubmember=self_club_member
                    )
                    # Preserve contact info on the surviving ClubMember
                    for field in ("name", "email", "phone_number", "address"):
                        self_val = getattr(self_club_member, field, None)
                        dup_val = getattr(dup_club_member, field, None)
                        if (self_val is None or self_val == "") and dup_val:
                            setattr(self_club_member, field, dup_val)
                    # Keep the later paid-through date; a merge must never shorten a membership.
                    for field in ("membership_last_paid", "membership_expiration_date"):
                        self_val = getattr(self_club_member, field, None)
                        dup_val = getattr(dup_club_member, field, None)
                        if dup_val and (self_val is None or dup_val > self_val):
                            setattr(self_club_member, field, dup_val)
                    self_club_member.save()
                    ClubHistory.objects.create(
                        club=self.auction.club,
                        user=user,
                        action=f"Merged club member {dup_club_member} into {self_club_member}: {reason}",
                        applies_to="MEMBERS",
                    )
                    dup_club_member.is_deleted = True
                    dup_club_member.save()
                else:
                    # self TOS has no club member yet — adopt the duplicate's
                    self.clubmember = dup_club_member
                    AuctionTOS.objects.filter(pk=self.pk).update(clubmember=dup_club_member)
            ClubHistory.objects.create(
                club=self.auction.club,
                user=user,
                action=merge_action,
                applies_to="MEMBERS",
            )
        else:
            # Standard auction — record in AuctionHistory
            self.auction.create_history(
                applies_to="USERS",
                action=merge_action,
                user=user,
            )
        # Clear a possible_duplicate pointing at the row being deleted, or saving self fails on the FK.
        if self.possible_duplicate_id == duplicate.pk:
            self.possible_duplicate = None
        # Delete the duplicate (cascades to delete its now-empty invoice)
        duplicate.delete()

    @cached_property
    def closest_location_for_this_user(self):
        result = PickupLocation.objects.none()
        if self.user and self.auction.multi_location:
            userData = self.user.userdata
            if userData.latitude:
                result = (
                    PickupLocation.objects.filter(auction=self.auction)
                    .annotate(distance=distance_to(userData.latitude, userData.longitude))
                    .order_by("distance")
                    .first()
                )
        return result

    @property
    def has_selected_closest_location(self):
        if self.closest_location_for_this_user:
            if self.closest_location_for_this_user == self.pickup_location:
                return True
            return False
        return True

    @cached_property
    def distance_traveled(self):
        if self.user and not self.manually_added:
            userData = self.user.userdata
            if userData.latitude:
                location = (
                    PickupLocation.objects.filter(pk=self.pickup_location.pk)
                    .annotate(
                        distance=distance_to(
                            userData.latitude,
                            userData.longitude,
                            approximate_distance_to=5,
                        )
                    )
                    .order_by("distance")
                    .first()
                )
                return location.distance
        return -1

    @cached_property
    def previous_auctions_count(self):
        return AuctionTOS.objects.filter(email=self.email, createdon__lte=self.createdon).exclude(pk=self.pk).count()

    @property
    def closer_location_savings(self):
        if not self.has_selected_closest_location:
            if self.closest_location_for_this_user and self.distance_traveled:
                return int(self.distance_traveled - self.closest_location_for_this_user.distance)
        return 0

    @cached_property
    def closer_location_warning(self):
        current_site = Site.objects.get_current()
        if self.closer_location_savings > 9:
            return f"You've selected {self.pickup_location}, but {self.closest_location_for_this_user} is {int(self.closer_location_savings)} miles closer to you.  You can change your pickup location on the auction rules page: https://{current_site.domain}{self.auction.get_absolute_url()}#join"
        return ""

    @cached_property
    def closer_location_warning_html(self):
        current_site = Site.objects.get_current()
        if self.closer_location_savings > 9:
            return f"You've selected {self.pickup_location}, but {self.closest_location_for_this_user} is {int(self.closer_location_savings)} miles closer to you.  You can change your pickup location <a href='https://{current_site.domain}{self.auction.get_absolute_url()}#join'>on the auction rules page</a>"
        return ""

    @property
    def timezone(self):
        try:
            return pytz_timezone(self.user.userdata.timezone)
        except:
            return self.auction.timezone

    @property
    def pickup_time_as_localized_string(self):
        """Do not use this in templates; it's for emails"""
        time = self.pickup_location.pickup_time
        localized_time = time.astimezone(self.timezone)
        return localized_time.strftime("%B %d at %I:%M %p")

    @property
    def second_pickup_time_as_localized_string(self):
        """Do not use this in templates; it's for emails"""
        if self.pickup_location.second_pickup_time:
            time = self.pickup_location.second_pickup_time
            localized_time = time.astimezone(self.timezone)
            return localized_time.strftime("%B %d at %I:%M %p")
        return ""

    @property
    def auction_date_as_localized_string(self):
        """Note that this is a different date for in person and online!"""
        if self.auction.is_online:
            time = self.auction.date_end
        else:
            # offline auctions use start time
            time = self.auction.date_start
        localized_time = time.astimezone(self.timezone)
        return localized_time.strftime("%B %d at %I:%M %p")

    @cached_property
    def trying_to_avoid_ban(self):
        """We track IPs in userdata, so we can do a quick check for this"""
        if self.user:
            userData = self.user.userdata
            if userData.last_ip_address:
                other_users = UserData.objects.filter(last_ip_address=userData.last_ip_address).exclude(pk=userData.pk)
                for other_user in other_users:
                    logger.debug("%s is also known as %s", self.user, other_user.user)
                    banned = UserBan.objects.filter(
                        banned_user=other_user.user, user__pk__in=self.auction.auction_admins_user_pks
                    ).first()
                    if banned:
                        url = reverse("userpage", kwargs={"slug": other_user.user.username})
                        return f"<a href='{url}'>{other_user.user.username}</a>"
        return False

    @cached_property
    def number_of_userbans(self):
        if self.user:
            other_bans = UserBan.objects.filter(banned_user=self.user)
            return other_bans.count()
        return ""


class AuctionDropdown(models.Model):
    auction = models.ForeignKey(Auction, on_delete=models.CASCADE)
    createdon = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    value = models.CharField(max_length=CUSTOM_DROPDOWN_MAX_LENGTH)

    def __str__(self):
        return self.value

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        old_value = None
        if not is_new:
            old_value = AuctionDropdown.objects.filter(pk=self.pk).values_list("value", flat=True).first()
        super().save(*args, **kwargs)
        duplicates = AuctionDropdown.objects.filter(auction=self.auction, value__iexact=self.value).order_by(
            "createdon", "pk"
        )
        oldest = duplicates.first()
        if oldest and oldest.pk != self.pk:
            AuctionDropdown.objects.filter(pk=self.pk).delete()
            self.pk = oldest.pk
            self.id = oldest.pk
            self.createdon = oldest.createdon
            self.user = oldest.user
            self.value = oldest.value
            self.auction = oldest.auction
            return
        duplicates.exclude(pk=self.pk).delete()
        if is_new:
            self.auction.create_history(
                applies_to="RULES",
                action=f"Added custom dropdown option '{self.value}'",
                user=self.user,
            )
        elif old_value != self.value:
            self.auction.create_history(
                applies_to="RULES",
                action=f"Renamed custom dropdown option '{old_value}' to '{self.value}'",
                user=self.user,
            )

    def delete(self, *args, **kwargs):
        value = self.value
        auction = self.auction
        user = self.user
        super().delete(*args, **kwargs)
        auction.create_history(
            applies_to="RULES",
            action=f"Removed custom dropdown option '{value}'",
            user=user,
        )


class Lot(CachedPropertiesMixin, models.Model):
    """A lot is something to bid on"""

    PIC_CATEGORIES = (
        ("ACTUAL", "My photo of this exact item"),
        (
            "REPRESENTATIVE",
            "My photo, but not of this exact item.  e.x. This is the parents of these fry",
        ),
        # Was "This picture is from the internet": a stored self-report of infringement, and the value
        # blanks default to. The catch-all now asks for what must be true.
        ("RANDOM", "Not my photo - I have permission to use it"),
    )
    # Three lot numbers; use lot_number_display. This is the pk.
    lot_number = models.AutoField(primary_key=True)
    # below is an automatically assigned int for use in auctions
    lot_number_int = models.IntegerField(null=True, blank=True, verbose_name="Lot number", db_index=True)
    # The auction lot number until 2025; lot_number_int is used now (issue #269).
    custom_lot_number = models.CharField(max_length=9, blank=True, null=True, verbose_name="Lot number", db_index=True)
    custom_lot_number.help_text = "You can override the default lot number with this"
    lot_name = models.CharField(max_length=40)
    slug = AutoSlugField(populate_from="lot_name", unique=False, always_update=True)
    # lot_name.help_text = "Short description of this lot"
    image = ThumbnailerImageField(upload_to="images/", blank=True)
    image.help_text = "Optional.  Add a picture of the item here."
    image_source = models.CharField(max_length=20, choices=PIC_CATEGORIES, blank=True)
    image_source.help_text = "Where did you get this image?"
    custom_checkbox = models.BooleanField(default=False, verbose_name="Custom checkbox")
    custom_field_1 = models.CharField(max_length=60, default="", blank=True)
    custom_dropdown = models.CharField(max_length=CUSTOM_DROPDOWN_MAX_LENGTH, default="", blank=True)
    i_bred_this_fish = models.BooleanField(default=False, verbose_name=settings.I_BRED_THIS_FISH_LABEL)
    i_bred_this_fish.help_text = "Check to get breeder points for this lot"
    summernote_description = models.TextField(verbose_name="Description", default="", blank=True)
    reference_link = models.URLField(blank=True, null=True)
    reference_link.help_text = (
        "A URL with additional information about this lot.  YouTube videos will be automatically embedded."
    )
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    quantity.help_text = "How many of this item are in this lot?"
    reserve_price = models.DecimalField(
        default=2,
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01")), MaxValueValidator(2000)],
        verbose_name="Minimum bid",
    )
    reserve_price.help_text = "Also called a reserve price. Lot will not be sold unless someone bids at least this much"
    buy_now_price = models.DecimalField(
        default=None,
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01")), MaxValueValidator(1000)],
        blank=True,
        null=True,
    )
    buy_now_price.help_text = (
        "This lot will be sold with no bidding for this price, if someone is willing to pay this much"
    )
    species = models.ForeignKey(
        Species, null=True, blank=True, on_delete=models.SET_NULL, verbose_name="Scientific name"
    )
    species.help_text = (
        "Start typing a lot name and pick from the list.  Leave as No species for hardware and other non-living lots."
    )
    species_category = models.ForeignKey(
        Category,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        verbose_name="Category",
    )
    species_category.help_text = "An accurate category will help people find this lot more easily"
    date_posted = models.DateTimeField(auto_now_add=True, blank=True)
    last_bump_date = models.DateTimeField(null=True, blank=True)
    last_bump_date.help_text = (
        "Any time a lot is bumped, this date gets changed.  It's used for sorting by newest lots."
    )
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    auctiontos_seller = models.ForeignKey(
        AuctionTOS,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="auctiontos_seller",
    )
    auction = models.ForeignKey(Auction, blank=True, null=True, on_delete=models.SET_NULL)
    auction.help_text = "<span class='text-warning' id='last-auction-special'></span>Only auctions that you have <span class='text-warning'>joined</span> will be shown here. This lot must be brought to that auction"
    date_end = models.DateTimeField(auto_now_add=False, blank=True, null=True)
    winner = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="winner")
    auctiontos_winner = models.ForeignKey(
        AuctionTOS,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="auctiontos_winner",
        verbose_name="Winner",
    )
    active = models.BooleanField(default=True, db_index=True)
    winning_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, db_index=True)
    refunded = models.BooleanField(default=False)
    refunded.help_text = "Don't charge the winner or pay the seller for this lot."
    banned = models.BooleanField(default=False, verbose_name="Removed", blank=True)
    banned.help_text = "This lot will be hidden from views, and users won't be able to bid on it.  Removed lots are not charged in invoices."
    ban_reason = models.CharField(max_length=100, blank=True, null=True)
    deactivated = models.BooleanField(default=False)
    deactivated.help_text = "You can deactivate your own lots to remove all bids and stop bidding.  Lots can be reactivated at any time, but existing bids won't be kept"
    lot_run_duration = models.PositiveIntegerField(default=10, validators=[MinValueValidator(1), MaxValueValidator(30)])
    lot_run_duration.help_text = "Days to run this lot for"
    relist_if_sold = models.BooleanField(default=False)
    relist_if_sold.help_text = "When this lot sells, create a new copy of it.  Useful if you have many copies of something but only want to sell one at a time."
    relist_if_not_sold = models.BooleanField(default=False)
    relist_if_not_sold.help_text = "When this lot ends without being sold, reopen bidding on it.  Lots can be automatically relisted up to 5 times."
    relist_countdown = models.PositiveIntegerField(default=4, validators=[MinValueValidator(0), MaxValueValidator(10)])
    number_of_bumps = models.PositiveIntegerField(blank=True, default=0, validators=[MinValueValidator(0)])
    donation = models.BooleanField(default=False)
    donation.help_text = "All proceeds from this lot will go to the club"
    watch_warning_email_sent = models.BooleanField(default=False)
    coming_up_push_sent = models.BooleanField(default=False)
    coming_up_push_sent.help_text = (
        "Set once this lot's watchers got the 'coming up soon -- N lots away' push while it sat in the "
        "top 10 of the in-person queue. Deduped so that push fires at most once per lot; the later "
        "'about to be sold' push (selling_push_notification_sent) overwrites it on the device."
    )
    selling_push_notification_sent = models.BooleanField(default=False)
    selling_push_notification_sent.help_text = (
        "Set once this lot's watchers got the final 'about to be sold' push (it reached the head of "
        "the queue or was pulled up on the set-winners screen). Deduped so that push fires at most "
        "once per lot; shares a notification tag with the 'coming up soon' push so it overwrites it."
    )
    added_to_queue = models.BooleanField(default=False)
    added_to_queue.help_text = (
        "Set once this lot has ever been added to the in-person selling queue. Sticky (never unset "
        "when the queue entry is removed or the lot sells) so the auction stats can report how much "
        "the queue was used."
    )
    # Unused; remove in a future migration.
    seller_invoice = models.ForeignKey(
        "Invoice",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="seller_invoice",
    )
    buyer_invoice = models.ForeignKey(
        "Invoice",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="buyer_invoice",
    )
    transportable = models.BooleanField(default=True)
    promoted = models.BooleanField(default=False, verbose_name="Promote this lot")
    promoted.help_text = "This does nothing right now lol"
    promotion_budget = models.PositiveIntegerField(default=2, validators=[MinValueValidator(0), MaxValueValidator(5)])
    promotion_budget.help_text = "The most money you're willing to spend on ads for this lot."
    # Now a random number so some lots outside favourite categories appear in recommendations.
    promotion_weight = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(20)])
    feedback_rating = models.IntegerField(default=0, validators=[MinValueValidator(-1), MaxValueValidator(1)])
    feedback_text = models.CharField(max_length=500, blank=True, null=True)
    winner_feedback_rating = models.IntegerField(default=0, validators=[MinValueValidator(-1), MaxValueValidator(1)])
    winner_feedback_text = models.CharField(max_length=500, blank=True, null=True)
    date_of_last_user_edit = models.DateTimeField(auto_now_add=True, blank=True)
    is_chat_allowed = models.BooleanField(default=True)
    is_chat_allowed.help_text = (
        "Uncheck to prevent chatting on this lot.  This will not remove any existing chat messages"
    )
    buy_now_used = models.BooleanField(default=False)

    # Copied from userdata, so a seller can't move after posting.
    latitude = models.FloatField(blank=True, null=True, db_index=True)
    longitude = models.FloatField(blank=True, null=True, db_index=True)
    address = models.CharField(max_length=500, blank=True, null=True)

    # Payment and shipping options from the last lot; only shown when not in an auction.
    payment_paypal = models.BooleanField(default=False, verbose_name="PayPal accepted")
    payment_cash = models.BooleanField(default=False, verbose_name="Cash accepted")
    payment_other = models.BooleanField(default=False, verbose_name="Other payment method accepted")
    payment_other_method = models.CharField(max_length=80, blank=True, null=True, verbose_name="Payment method")
    payment_other_address = models.CharField(max_length=200, blank=True, null=True, verbose_name="Payment address")
    payment_other_address.help_text = "The address or username you wish to get payment at"
    # shipping options
    local_pickup = models.BooleanField(default=False)
    local_pickup.help_text = "Check if you'll meet people in person to exchange this lot"
    other_text = models.CharField(max_length=200, blank=True, null=True, verbose_name="Shipping notes")
    other_text.help_text = "Shipping methods, temperature restrictions, etc."
    shipping_locations = models.ManyToManyField(Location, blank=True, verbose_name="I will ship to")
    shipping_locations.help_text = "Check all locations you're willing to ship to"
    is_deleted = models.BooleanField(default=False)
    added_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="added_by")
    added_by.help_text = "User who added this lot -- used for pre-registration discounts"
    category_automatically_added = models.BooleanField(default=False)
    category_checked = models.BooleanField(default=False)
    label_printed = models.BooleanField(default=False)
    label_needs_reprinting = models.BooleanField(default=False)
    partial_refund_percent = models.IntegerField(
        default=0, validators=[MinValueValidator(0), MaxValueValidator(100)], blank=True
    )
    no_more_refunds_possible = models.BooleanField(default=False)
    no_more_refunds_possible.help_text = (
        "Set to True after a Square refund is issued to prevent multiple refunds that would unbalance the books"
    )
    max_bid_revealed_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="max_bid_revealed_by"
    )
    admin_validated = models.BooleanField(default=False)
    # Delegates image management to another lot (``images`` and ``thumbnail`` read the source). Lot
    # copying deep-copies images instead.
    use_images_from = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="image_source_for",
        help_text="Images are managed from this lot",
    )
    image_url = models.URLField(blank=True, null=True)
    image_url.help_text = "If filled out, an image will be added to this lot using this URL when saving"
    bap_points_awarded = models.IntegerField(default=0)
    BAP_REASON_CHOICES = (
        ("not_eligible", "Not eligible"),
        ("not_long_enough", "Not long enough since last submission"),
        ("category_not_eligible", "Category not eligible (BAP points = 0)"),
        ("species_not_eligible", "Species not eligible for breeder points"),
        ("not_club_member", "Not a club member"),
        ("not_bred", "Didn't breed this fish"),
        ("not_active_member", "Not an active club member"),
        ("not_sold", "Not sold"),
        ("low_quantity", "Quantity below club minimum"),
        ("not_donation", "Lot is not a donation"),
        ("has_min_bid", "Lot has a minimum bid set"),
    )
    bap_auto_reason = models.CharField(max_length=30, choices=BAP_REASON_CHOICES, blank=True, default="")
    manually_approved = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        from django.db import transaction

        # Assign lot_number_int or custom_lot_number under a row lock to avoid races.
        needs_lock = self.auction and (
            (self.lot_number_int is None)  # Standard mode needs lot_number_int
            or (
                not self.custom_lot_number and self.auction.use_seller_dash_lot_numbering
            )  # Seller dash mode needs custom_lot_number
        )

        if needs_lock:
            # We need to wrap the entire save in a transaction with locking
            with transaction.atomic():
                Auction.objects.select_for_update().get(pk=self.auction.pk)

                # Assign lot_number_int if needed
                if self.lot_number_int is None:
                    # Now safely get the max lot_number_int while holding the lock
                    minimum_lot_number = 1
                    # Deliberately includes deleted and removed lots.
                    max_number = Lot.objects.filter(auction=self.auction).aggregate(Max("lot_number_int"))[
                        "lot_number_int__max"
                    ]
                    self.lot_number_int = (max_number or (minimum_lot_number - 1)) + 1

                # Continue with the rest of the save logic
                self._do_save(*args, **kwargs)
        else:
            # No lock needed, proceed normally
            self._do_save(*args, **kwargs)

    def invalidate_cached_properties(self, *names):
        """Drop this lot's caches and the counts its participants and auction hold (reached via fields_cache)."""
        super().invalidate_cached_properties(*names)
        for relation in ("auctiontos_seller", "auctiontos_winner", "auction"):
            related = self._state.fields_cache.get(relation)
            if related is not None:
                related.invalidate_cached_properties()

    def _do_save(self, *args, **kwargs):
        """Internal method to complete the save operation"""
        # Seller-dash auctions: bidder_number-lot_number.
        if not self.custom_lot_number and self.auction and self.auction.use_seller_dash_lot_numbering:
            if self.auctiontos_seller:
                custom_lot_number = 1
                other_lots = self.auctiontos_seller.lots_qs
                for lot in other_lots:
                    match = re.findall(r"\d+", f"{lot.custom_lot_number}")
                    if match:
                        # last string of digits found
                        match = int(match[-1])
                        if match >= custom_lot_number:
                            custom_lot_number = match + 1
                # trim the end to fit in custom lot number if the length is too long
                self.custom_lot_number = f"{self.auctiontos_seller.bidder_number}-{custom_lot_number}"[:9]
        # a bit of magic to automatically set categories
        fix_category = False
        if not self.species_category or (self.species_category and self.species_category.name == "Uncategorized"):
            fix_category = True
        if not self.species_category:
            fix_category = True
        if self.category_checked:
            fix_category = False
        # A species' category overrides a guessed category, even after the species changes. A
        # person's choice (category_automatically_added unset) is never touched. "Uncategorized"
        # counts as no choice.
        uncategorised = not self.species_category or self.species_category.name == "Uncategorized"
        from_species = self.category_from_species
        if from_species and (fix_category or uncategorised or self.category_automatically_added):
            self.category_checked = True
            if self.auction and not self.auction.use_categories:
                self.species_category = Category.objects.filter(name="Uncategorized").first()
            else:
                self.species_category = from_species
                self.category_automatically_added = True
        elif fix_category:
            self.category_checked = True
            if self.auction:
                if not self.auction.use_categories:
                    # force uncategorized for non-fish auctions
                    self.species_category = Category.objects.filter(name="Uncategorized").first()
                else:
                    result = guess_category(self.lot_name)
                    if result:
                        self.species_category = result
                        self.category_automatically_added = True
                    else:
                        self.species_category = Category.objects.filter(name="Uncategorized").first()
        if not self.reference_link:
            search = self.lot_name.replace(" ", "%20")
            self.reference_link = f"https://www.google.com/search?q={search}&tbm=isch"
        if (
            self.auction
            and self.auction.force_donation_threshold
            and self.winning_price
            and self.winning_price <= self.auction.force_donation_threshold
        ):
            self.donation = True
        self.summernote_description = sanitize_summernote_html(self.summernote_description)
        if not self.quantity:
            self.quantity = 1
        super().save(*args, **kwargs)

        # chat history subscription for the owner
        if self.user:
            subscription, created = ChatSubscription.objects.get_or_create(
                user=self.user,
                lot=self,
                defaults={
                    "unsubscribed": not self.user.userdata.email_me_when_people_comment_on_my_lots,
                },
            )
        # lot_number_display must be unique in the auction (issue #420). Only after the first save;
        # the initial save is protected by the row lock.
        if self.auction and self.pk:
            # Check for duplicates based on lot_number_display
            if self.auction.use_seller_dash_lot_numbering and self.custom_lot_number:
                duplicate_lot = (
                    Lot.objects.filter(
                        auction=self.auction,
                        custom_lot_number=self.custom_lot_number,
                    )
                    .exclude(pk=self.pk)
                    .first()
                )
                if duplicate_lot:
                    # Generate a new custom_lot_number for this (newest) lot
                    if self.auctiontos_seller:
                        custom_lot_number = 1
                        # Only fetch custom_lot_number field for performance
                        other_lot_numbers = (
                            Lot.objects.filter(
                                auction=self.auction,
                                auctiontos_seller=self.auctiontos_seller,
                            )
                            .exclude(pk=self.pk)
                            .values_list("custom_lot_number", flat=True)
                        )
                        for lot_number in other_lot_numbers:
                            match = re.findall(r"\d+", f"{lot_number}")
                            if match:
                                match = int(match[-1])
                                if match >= custom_lot_number:
                                    custom_lot_number = match + 1
                        self.custom_lot_number = f"{self.auctiontos_seller.bidder_number}-{custom_lot_number}"[:9]
                        self.label_printed = False
                        # Update in database without triggering full save logic
                        Lot.objects.filter(pk=self.pk).update(
                            custom_lot_number=self.custom_lot_number, label_printed=False
                        )
                        self.auction.create_history(
                            "LOTS",
                            f"Duplicate lot number detected, changed to {self.lot_number_display}",
                            user=None,
                        )
            elif self.lot_number_int and not self.auction.use_seller_dash_lot_numbering:
                # Check for duplicate lot_number_int in standard mode
                duplicate_lot = (
                    Lot.objects.filter(
                        auction=self.auction,
                        lot_number_int=self.lot_number_int,
                    )
                    .exclude(pk=self.pk)
                    .first()
                )
                if duplicate_lot:
                    # Generate a new lot_number_int for this (newest) lot
                    max_number = Lot.objects.filter(auction=self.auction).aggregate(Max("lot_number_int"))[
                        "lot_number_int__max"
                    ]
                    self.lot_number_int = (max_number or 0) + 1
                    self.label_printed = False
                    # Update in database without triggering full save logic
                    Lot.objects.filter(pk=self.pk).update(lot_number_int=self.lot_number_int, label_printed=False)
                    self.auction.create_history(
                        "LOTS",
                        f"Duplicate lot number detected, changed to {self.lot_number_display}",
                        user=None,
                    )

    def __str__(self):
        return "" + str(self.lot_number_display) + " - " + self.lot_name

    @cached_property
    def currency(self):
        """Get the currency for this lot based on the auction creator or lot owner"""
        if self.auction and self.auction.created_by:
            return self.auction.created_by.userdata.currency
        elif self.user:
            return self.user.userdata.currency
        return "USD"

    @cached_property
    def currency_symbol(self):
        """Get the currency symbol for this lot"""
        return get_currency_symbol(self.currency)

    def add_winner_message(self, user, tos, winning_price):
        """A lot history message when a winner is set or changed. Must be called on every change so invoices
        recalculate.
        """
        message = (
            f"{user.username} has set bidder {tos} as the winner of this lot ({self.currency_symbol}{winning_price})"
        )
        try:
            LotHistory.objects.create(
                lot=self,
                user=user,
                message=message,
                notification_sent=True,
                bid_amount=winning_price,
                changed_price=True,
                seen=True,
            )
        except Exception:
            logger.exception("Failed to create winner LotHistory for lot %s", self.pk)
        try:
            invoice = Invoice.objects.filter(auctiontos_user=tos, auction=self.auction).first()
            if not invoice:
                invoice = Invoice.objects.create(auctiontos_user=tos, auction=self.auction)
            invoice.recalculate()
        except Exception:
            logger.exception("Failed to recalculate invoice after winner set on lot %s", self.pk)
        self.send_websocket_message(
            {
                "type": "chat_message",
                "info": "LOT_END_WINNER",
                "message": message,
                "high_bidder_pk": tos.user.pk if tos.user else -1,
                "high_bidder_name": tos.display_name_for_admins,
                "current_high_bid": winning_price,
            }
        )

    def send_websocket_message(self, message):
        try:
            channel_layer = channels.layers.get_channel_layer()
            serialized = {k: float(v) if isinstance(v, Decimal) else v for k, v in message.items()}
            async_to_sync(channel_layer.group_send)(f"lot_{self.pk}", serialized)
        except Exception:
            # Channel failures never block declaring a winner.
            logger.exception("Failed to send websocket message for lot %s", self.pk)

    def send_ending_very_soon_message(self):
        """Send a websocket message when the lot is ending in less than a minute"""
        if self.ending_very_soon and not self.sold:
            result = {
                "type": "chat_message",
                "info": "CHAT",
                "message": "Bidding ends in less than a minute!!",
                "pk": -1,
                "username": "System",
            }
            self.send_websocket_message(result)

    def send_lot_end_message(self):
        """Websocket message and LotHistory when a lot ends, with or without a winner."""
        info = None
        bidder = None

        if self.high_bidder:
            self.sell_to_online_high_bidder
            info = "LOT_END_WINNER"
            bidder = self.high_bidder
            high_bidder_pk = self.high_bidder.pk
            high_bidder_name = str(self.high_bidder_display)
            current_high_bid = self.high_bid
            message = f"Won by {self.high_bidder_display}"

        # Unsold at this point: find the high bidder.
        if not self.sold:
            high_bidder_pk = None
            high_bidder_name = None
            current_high_bid = None
            message = "This lot did not sell"
            bidder = None
            info = "ENDED_NO_WINNER"

        result = {
            "type": "chat_message",
            "info": info,
            "message": message,
            "high_bidder_pk": high_bidder_pk,
            "high_bidder_name": high_bidder_name,
            "current_high_bid": current_high_bid,
        }

        if info:
            self.send_websocket_message(result)
            try:
                LotHistory.objects.create(
                    lot=self,
                    user=bidder,
                    message=message,
                    changed_price=True,
                    current_price=self.high_bid,
                )
            except Exception:
                # Activity feed only; never block ending the lot.
                logger.exception("Failed to create lot end LotHistory for lot %s", self.pk)
        self.save()

    def send_non_auction_lot_emails(self):
        """Send winner and seller emails for lots not in an auction"""
        if self.winner and not self.auction:
            current_site = Site.objects.get_current()
            # email the winner first
            mail.send(
                self.winner.email,
                headers={"Reply-to": self.user.email},
                template="non_auction_lot_winner",
                context={"lot": self, "domain": current_site.domain, "reply_to_email": self.user.email},
            )
            # now, email the seller
            mail.send(
                self.user.email,
                headers={"Reply-to": self.winner.email},
                template="non_auction_lot_seller",
                context={"lot": self, "domain": current_site.domain, "reply_to_email": self.winner.email},
            )

    def process_relist_logic(self):
        """Automatic relisting for non-auction lots: ``(relist, sendNoRelistWarning)``."""
        relist = False
        sendNoRelistWarning = False

        if not self.auction:
            if self.winner and self.relist_if_sold and (not self.relist_countdown):
                sendNoRelistWarning = True
            if (not self.winner) and self.relist_if_not_sold and (not self.relist_countdown):
                sendNoRelistWarning = True
            if self.winner and self.relist_if_sold and self.relist_countdown:
                self.relist_countdown -= 1
                relist = True
            if (not self.winner) and self.relist_if_not_sold and self.relist_countdown:
                # no need to relist unsold lots, just decrement the countdown
                self.relist_countdown -= 1
                self.date_end = timezone.now() + datetime.timedelta(days=self.lot_run_duration)
                self.active = True
                self.seller_invoice = None
                self.buyer_invoice = None
        self.save()
        return relist, sendNoRelistWarning

    def relist_lot(self):
        """Create a relisted copy of this lot and return it."""
        originalImages = LotImage.objects.filter(lot_number=self.pk)
        originalPk = self.pk
        self.pk = None  # create a new, duplicate lot
        self.date_end = timezone.now() + datetime.timedelta(days=self.lot_run_duration)
        self.active = True
        self.winner = None
        self.winning_price = None
        self.seller_invoice = None
        self.buyer_invoice = None
        self.buy_now_used = False
        self.save()

        # copy shipping locations
        for location in Lot.objects.get(lot_number=originalPk).shipping_locations.all():
            self.shipping_locations.add(location)

        # copy images
        for originalImage in originalImages:
            newImage = LotImage.objects.create(
                createdon=originalImage.createdon,
                lot_number=self,
                image_source=originalImage.image_source,
                is_primary=originalImage.is_primary,
                url=originalImage.url,
            )
            if originalImage.image:
                newImage.image = get_thumbnailer(originalImage.image)
                # Shared file, shared Cloudflare image.
                newImage.cloudflare_image_id = originalImage.cloudflare_image_id
            # A sold lot's picture isn't of the new item.
            if originalImage.image_source == "ACTUAL":
                newImage.image_source = "REPRESENTATIVE"
            newImage.save()

        return self

    def refund(self, amount, user, message=None):
        """Record a refund message; processes a Square refund when possible."""
        if amount and amount != self.partial_refund_percent:
            # Check if we should process a Square refund automatically
            if self.square_refund_possible and not self.no_more_refunds_possible:
                error = self.square_refund(amount)
                if error:
                    # Log the error but continue with the refund record
                    import logging

                    logger = logging.getLogger(__name__)
                    logger.error("Square refund failed for lot %s: %s", self.pk, error)
                    if not message:
                        message = f"{user} has issued a {amount}% refund on this lot. Square refund failed: {error}"
                else:
                    if not message:
                        message = (
                            f"{user} has issued a {amount}% refund on this lot. Square refund processed automatically."
                        )
            else:
                if not message:
                    message = f"{user} has issued a {amount}% refund on this lot."

            LotHistory.objects.create(lot=self, user=user, message=message, changed_price=True)
        self.partial_refund_percent = amount
        self.save()

    @cached_property
    def winner_invoice(self):
        """The winner's Invoice, or None; via AuctionTOS first, which is prefetchable."""
        from auctions.models import Invoice

        if self.auctiontos_winner_id:
            invoice = self.auctiontos_winner.invoice
            if invoice:
                return invoice
        if self.winner_id:
            return Invoice.objects.filter(auctiontos_user__user_id=self.winner_id, auction=self.auction).first()
        return None

    @cached_property
    def sellers_invoice(self):
        """The seller's Invoice, or None; via AuctionTOS first, which is prefetchable."""
        from auctions.models import Invoice

        if self.auctiontos_seller_id:
            invoice = self.auctiontos_seller.invoice
            if invoice:
                return invoice
        if self.user_id:
            return Invoice.objects.filter(auctiontos_user__user_id=self.user_id, auction=self.auction).first()
        return None

    @cached_property
    def square_refund_possible(self):
        """True with a Square payment on the winner's invoice that covers the lot and no refund yet."""
        if not self.winning_price or self.winning_price <= 0:
            return False

        # Check if a refund has already been issued
        if self.no_more_refunds_possible:
            return False

        invoice = self.winner_invoice
        if not invoice:
            return False

        # Check for Square payments with available refund amount
        from decimal import Decimal

        from auctions.models import InvoicePayment

        payment = (
            InvoicePayment.objects.filter(invoice=invoice, payment_method__iexact="square")
            .exclude(amount__lt=0)
            .order_by("-amount_available_to_refund")
            .first()
        )

        if not payment:
            return False

        # Check if there's enough available to refund
        lot_cost = Decimal(str(self.winning_price))
        return payment.amount_available_to_refund >= lot_cost

    def square_refund(self, percent):
        """Create a Square refund for ``percent`` of the winning price. Error string or None."""
        from decimal import Decimal

        from auctions.models import InvoicePayment, SquareSeller

        if not self.winning_price or self.winning_price <= 0:
            return "No valid winning price for this lot"

        if percent < 0 or percent > 100:
            return "Refund percent must be between 0 and 100"

        # Calculate refund amount
        refund_amount = (Decimal(str(self.winning_price)) * Decimal(str(percent))) / Decimal(100)
        if refund_amount <= 0:
            return "Refund amount must be positive"

        # Get the buyer's invoice using the property
        invoice = self.winner_invoice
        if not invoice:
            return "No invoice found for winner"

        # Find the Square payment
        payment = (
            InvoicePayment.objects.filter(invoice=invoice, payment_method__iexact="square")
            .exclude(amount__lt=0)
            .order_by("-amount_available_to_refund")
            .first()
        )

        if not payment:
            return "No Square payment found for this invoice"

        if payment.amount_available_to_refund < refund_amount:
            return f"Insufficient funds available to refund. Available: {payment.amount_available_to_refund}, Requested: {refund_amount}"

        # Get seller's Square credentials
        seller = SquareSeller.objects.filter(user=self.auction.created_by).first()
        if not seller:
            return "Seller has not connected their Square account"

        # Process refund using SquareSeller method
        reason = f"Lot {self.lot_number_display} - {percent}% refund"
        error = seller.process_refund(payment, refund_amount, reason)
        if error:
            return error

        # Mark that a refund has been issued to prevent double refunds
        self.no_more_refunds_possible = True
        self.save()

        # Webhook will create the negative InvoicePayment record
        return None

    def remove(self, banned, user, message=None):
        """Call this to add a message when banning (removing) a lot"""
        if banned and banned != self.banned:
            if not message:
                message = f"{user} removed this lot."
            LotHistory.objects.create(lot=self, user=user, message=message, changed_price=True)
        self.banned = banned
        self.save()

    def delete(self, *args, **kwargs):
        self.is_deleted = True
        self.save()

    def is_owned_by(self, user):
        """Whether `user` is the seller and may edit, delete or add images.

        `Lot.user` is null on many real sellers' lots (created through an unlinked TOS), so the seller TOS is
        checked too, by account or verified email, as `InvoiceView` does. `backfill_lot_users` repairs stored
        rows.
        """
        if not user or not user.is_authenticated:
            return False
        if self.user_id and self.user_id == user.pk:
            return True
        tos = self.auctiontos_seller
        if not tos:
            return False
        if tos.user_id and tos.user_id == user.pk:
            return True
        return bool(tos.email) and normalize_email(tos.email) == normalize_email(user.email)

    def image_permission_check(self, user):
        """See if `user` can add/edit images to this lot"""
        if self.use_images_from:
            # images are managed from another lot; nothing should be added here
            return False
        if not self.can_add_images:
            return False
        if not user.is_authenticated:
            return False
        # Lots borrowing this lot's images that can't take images (e.g. sold).
        dependent_lots = Lot.objects.filter(use_images_from=self, is_deleted=False)
        for dependent_lot in dependent_lots:
            if dependent_lot.auction and not dependent_lot.can_add_images:
                return False
        if self.is_owned_by(user):
            return True
        if user.is_superuser:
            return True
        if self.auction:
            # Auction.permission_check, which includes club admins of club-managed auctions.
            return self.auction.permission_check(user)
        return False

    @property
    def i_bred_this_fish_display(self):
        if self.i_bred_this_fish:
            return "Yes"
        else:
            return ""

    @property
    def seller_invoice_link(self):
        """/invoices/123 for the auction/seller of this lot"""
        invoice = self.sellers_invoice
        if invoice:
            return reverse("invoice_by_pk", kwargs={"pk": invoice.pk})
        return ""

    @property
    def winner_invoice_link(self):
        """/invoices/123 for the auction/winner of this lot"""
        invoice = self.winner_invoice
        if invoice:
            return reverse("invoice_by_pk", kwargs={"pk": invoice.pk})
        return ""

    @cached_property
    def tos_needed(self):
        if not self.auction:
            return False
        if self.auctiontos_seller:
            return False
        if AuctionTOS.objects.filter(user=self.user, auction=self.auction).exists():
            return False
        return self.auction.get_absolute_url()

    @cached_property
    def winner_location(self):
        """String of location of the winner for this lot"""
        try:
            return str(self.auctiontos_winner.pickup_location)
        except:
            pass
        tos = AuctionTOS.objects.filter(user=self.winner, auction=self.auction).first()
        if tos:
            return str(tos.pickup_location)
        return ""

    @cached_property
    def location_as_object(self):
        """Pickup location of the seller"""
        try:
            return self.auctiontos_seller.pickup_location
        except:
            pass
        tos = AuctionTOS.objects.filter(user=self.user, auction=self.auction).first()
        if tos:
            return tos.pickup_location
        return None

    @property
    def location(self):
        """String of location of the seller of this lot"""
        return str(self.location_as_object) or ""

    @property
    def seller_name(self):
        """Full name of the seller of this lot"""
        if self.auctiontos_seller:
            return self.auctiontos_seller.name
        if self.user:
            return self.user.first_name + " " + self.user.last_name
        return "Unknown"

    @property
    def seller_email(self):
        """Email of the seller of this lot"""
        if self.auctiontos_seller:
            return self.auctiontos_seller.email
        if self.user:
            return self.user.email
        return "Unknown"

    @property
    def winner_name(self):
        """Full name of the winner of this lot"""
        if self.auctiontos_winner:
            return self.auctiontos_winner.name
        if self.winner:
            return self.winner.first_name + " " + self.winner.last_name
        return ""

    @property
    def winner_email(self):
        """Email of the winner of this lot"""
        if self.auctiontos_winner:
            return self.auctiontos_winner.email
        if self.winner:
            return self.winner.email
        return ""

    @cached_property
    def seller_as_str(self):
        """String of the seller name or number, for use on lot pages"""
        if self.auctiontos_seller:
            return str(self.auctiontos_seller)
        if self.user:
            return str(self.user)
        return "Unknown"

    @cached_property
    def high_bidder_display(self):
        if self.sealed_bid:
            return "Sealed bid"
        if self.winner_as_str:
            return self.winner_as_str
        if self.high_bidder:
            userData = self.high_bidder.userdata
            if userData.username_visible:
                return str(self.high_bidder)
            else:
                return "Anonymous"
        if self.auction and not self.auction.is_online and self.auction.online_bidding == "buy_now_only":
            if self.buy_now_price:
                return "Buy now"
            return ""
        return "No bids"

    @cached_property
    def high_bidder_for_admins(self):
        if self.auctiontos_winner:
            return self.auctiontos_winner.display_name_for_admins
        if self.winner:
            return str(self.winner)
        if self.high_bidder:
            tos = AuctionTOS.objects.filter(user=self.high_bidder, auction=self.auction).first()
            if tos:
                return tos.bidder_number
            else:
                # should never happen
                return "Unknown bidder"
        return "No bids"

    @property
    def auction_show_high_bidder_template(self):
        """Admin-only HTML to reveal the high bidder. Returns safe HTML."""
        if (
            self.auction
            and self.high_bidder
            and not self.auction.is_online
            and not self.ended
            and self.auction.online_bidding == "allow"
        ):
            return mark_safe(f"""<a href='javascript:void(0);'
                hx-get="{reverse("auction_show_high_bidder", kwargs={"pk": self.pk})}"
                hx-swap="outerHTML"
                hx-trigger="click"
            >
                Reveal max bid
            </a>""")
        else:
            return ""

    @cached_property
    def winner_as_str(self):
        """String of the winner name or number, for use on lot pages"""
        if self.auctiontos_winner:
            return f"{self.auctiontos_winner}"
        if self.winner:
            userData = self.winner.userdata
            if userData.username_visible:
                return str(self.winner)
            else:
                return "Anonymous"
        return ""

    @property
    def sell_to_online_high_bidder(self):
        if self.high_bidder:
            self.winner = self.high_bidder
            self.winning_price = self.high_bid
            self.active = False
            tos = AuctionTOS.objects.filter(auction=self.auction, user=self.high_bidder).order_by("-createdon").first()
            if tos:
                self.auctiontos_winner = tos
            self.save()
            return f"{self.high_bidder_for_admins} is now the winner of lot {self.lot_number_display} for ${self.winning_price}"
        else:
            return "No high bidder"

    @property
    def sold(self):
        if self.winner or self.auctiontos_winner:
            if self.winning_price:
                return True
        return False

    @property
    def bap_placeholder(self):
        """Points label: Culture, HAP or BAP by club settings and category."""
        if self.auction and self.auction.club:
            club = self.auction.club
            cat = self.species_category.name if self.species_category else None
            if club.separate_cap and cat in ("Live food cultures", "Snails and other inverts"):
                return "Culture"
            if club.separate_hap and cat == "Aquatic plants":
                return "HAP"
        return "BAP"

    @cached_property
    def unsold_lot_no_bap_reason(self):
        """A BAP_REASON_CHOICES key if ineligible for points, or None. Ignores whether sold (see
        sold_lot_no_bap_reason).
        """
        if not self.auction or not self.auction.club:
            return "not_eligible"
        club = self.auction.club
        if not club.enable_breeder_award_program:
            return "not_eligible"
        if not self.i_bred_this_fish:
            return "not_bred"
        if self.species and not self.species.earns_breeder_points:
            return "species_not_eligible"
        if club.only_donation_lots and not self.donation:
            return "not_donation"
        if club.no_min_bids and self.reserve_price > self.auction.minimum_bid:
            return "has_min_bid"
        category_name = self.species_category.name if self.species_category else None
        # Live food is eligible only when CAP is on.
        if not club.separate_cap and category_name == "Live food cultures":
            return "category_not_eligible"
        # Seller identity, needed before the low_quantity check.
        seller_user = self.user or (self.auctiontos_seller.user if self.auctiontos_seller else None)
        seller_email = (self.auctiontos_seller.email if self.auctiontos_seller else None) or (
            seller_user.email if seller_user else None
        )
        # not_long_enough takes priority over low_quantity.
        if club.days_between_same_name_lots > 0 and (seller_user or seller_email):
            cutoff = timezone.now() - datetime.timedelta(days=club.days_between_same_name_lots)
            base_prior = Lot.objects.filter(
                auction__club=club,
                lot_name=self.lot_name,
                bap_points_awarded__gt=0,
                date_end__gte=cutoff,
            ).exclude(pk=self.pk)
            prior = False
            if seller_user:
                prior = base_prior.filter(user=seller_user).exists()
            if not prior and seller_email:
                prior = base_prior.filter(
                    Q(auctiontos_seller__email__iexact=seller_email) | Q(user__email__iexact=seller_email)
                ).exists()
            if prior:
                return "not_long_enough"
        # The same rule on the species row (not its parent): strains are separate things to breed.
        if club.days_between_same_species_lots > 0 and self.species_id and (seller_user or seller_email):
            cutoff = timezone.now() - datetime.timedelta(days=club.days_between_same_species_lots)
            base_prior = Lot.objects.filter(
                auction__club=club,
                species_id=self.species_id,
                bap_points_awarded__gt=0,
                date_end__gte=cutoff,
            ).exclude(pk=self.pk)
            prior = False
            if seller_user:
                prior = base_prior.filter(user=seller_user).exists()
            if not prior and seller_email:
                prior = base_prior.filter(
                    Q(auctiontos_seller__email__iexact=seller_email) | Q(user__email__iexact=seller_email)
                ).exists()
            if prior:
                return "not_long_enough"
        # Plants, snails and live food ignore quantity minimums.
        ignore_quantity = category_name in ("Aquatic plants", "Live food cultures", "Snails and other inverts")
        if not ignore_quantity and self.quantity < club.min_quantity:
            return "low_quantity"
        if not self.species_category or self.species_category.bap_points == 0:
            return "category_not_eligible"
        member = None
        if seller_user:
            member = ClubMember.objects.filter(club=club, user=seller_user, is_deleted=False).first()
        if not member and seller_email:
            member = ClubMember.objects.filter(club=club, email__iexact=seller_email, is_deleted=False).first()
        if not member:
            return "not_club_member"
        seller_user = seller_user or member.user
        if club.only_active_members_can_participate:
            today = timezone.now().date()
            if member.membership_expiration_date:
                valid = member.membership_expiration_date >= today
            elif member.membership_last_paid:
                if club.membership_system == "january_first":
                    valid = member.membership_last_paid >= datetime.date(today.year, 1, 1)
                else:
                    valid = member.membership_last_paid >= today - datetime.timedelta(days=365)
            else:
                valid = False
            if not valid:
                return "not_active_member"
        return None

    @property
    def sold_lot_no_bap_reason(self):
        """A BAP_REASON_CHOICES key if ineligible for awarded points, or None."""
        if not self.sold:
            club = self.auction.club if self.auction else None
            if not club or club.only_sold_lots:
                return "not_sold"
        return self.unsold_lot_no_bap_reason

    def bap_points_for_club(self, club):
        """Points this lot is worth to *club* before the checkbox bonus: genus override, category override,
        club flat rate, category default.
        """
        if self.species and self.species.genus:
            genus_override = ClubBapGenusOverride.objects.filter(club=club, genus=self.species.genus).first()
            if genus_override is not None:
                return genus_override.points
        if self.species_category:
            category_override = ClubBapCategoryOverride.objects.filter(
                club=club, category=self.species_category
            ).first()
            if category_override is not None:
                return category_override.points
        # `is not None`: 0 means zero.
        if club.points_per_lot is not None:
            return club.points_per_lot
        return self.species_category.bap_points if self.species_category else 5

    def default_bap_points(self, club):
        """What Approve offers: :meth:`bap_points_for_club` plus ``points_for_custom_checkbox`` if ticked. The
        one definition; ``ClubBapLotHTMxTable.render_actions`` is a deliberate prefetched copy.
        """
        points = self.bap_points_for_club(club)
        if club.points_for_custom_checkbox > 0 and self.custom_checkbox:
            points += club.points_for_custom_checkbox
        return points

    def auto_award_bap_points(self):
        """Store bap_auto_reason when a winner is set, and create a BapAward if auto_add_points. Safe to call
        repeatedly.
        """
        if not (self.auction and self.auction.club):
            return
        # Skip if a BapAward already exists for this lot (auto or manual)
        if BapAward.objects.filter(lot=self).exists():
            return
        club = self.auction.club
        # Always stored so the pending table needs no live queries; "" is eligible.
        reason = self.sold_lot_no_bap_reason
        self.bap_auto_reason = reason or ""
        self.bap_points_awarded = 0
        self.save(update_fields=["bap_auto_reason", "bap_points_awarded"])
        if reason:
            # Ineligible — reason stored above; nothing more to do
            return
        if not club.auto_add_points:
            # Eligible, manual approval: the admin creates the award.
            return
        # Eligible + auto_add_points: create the BapAward now
        points = self.default_bap_points(club)
        seller_user = self.user or (self.auctiontos_seller.user if self.auctiontos_seller else None)
        if not seller_user:
            return
        member = ClubMember.objects.filter(club=club, user=seller_user, is_deleted=False).first()
        if not member:
            return
        award_date = self.date_end.date() if self.date_end else timezone.now().date()
        placeholder = self.bap_placeholder
        bap_pts = points if placeholder == "BAP" else 0
        hap_pts = points if placeholder == "HAP" else 0
        cap_pts = points if placeholder == "Culture" else 0
        BapAward.objects.create(
            club_member=member,
            date=award_date,
            points=bap_pts,
            hap_points=hap_pts,
            cap_points=cap_pts,
            lot=self,
            awarded_by=None,  # None = auto-awarded by the system
        )
        self.bap_points_awarded = points
        self.save(update_fields=["bap_points_awarded"])

    @property
    def pre_registered(self):
        """True if this lot will get a discount for being pre-registered"""
        if self.auction:
            if self.auction.pre_register_lot_discount_percent:
                if self.added_by and self.user:
                    if self.added_by == self.user:
                        return True
        return False

    @cached_property
    def number_of_watchers(self):
        return Watch.objects.filter(lot_number=self.lot_number).count()

    @property
    def hard_end(self):
        """The absolute latest a lot can end, even with dynamic endings"""
        dynamic_end = datetime.timedelta(minutes=60)
        if self.auction:
            return self.auction.dynamic_end
        # No hard end for non-auction lots; date_end is extended by bidding.reset_lot_end_time.
        return self.date_end + dynamic_end

    @property
    def calculated_end(self):
        """When this lot ends. See calculated_end_for_templates for display."""
        # for in-person auctions only
        if self.is_part_of_in_person_auction:
            return self.auction.date_start + datetime.timedelta(days=364)
        # online auctions update lot.date_end (rolling endings)
        if self.date_end:
            return self.date_end
        # Shouldn't happen: date_end blank.
        return timezone.now()

    @property
    def ends_when_sold(self):
        """True when an in-person lot has no end to show yet, only "when the auctioneer gets to it"."""
        return self.is_part_of_in_person_auction and not (self.winner_as_str and self.date_end)

    @property
    def calculated_end_for_templates(self):
        """calculated_end for display: in-person lots say they end when the admin ends them."""
        if self.is_part_of_in_person_auction:
            if self.winner_as_str and self.date_end:
                # a sold lot that's part of an in-person auction
                return self.date_end
            return "Ends when sold"
        else:
            return self.calculated_end

    @property
    def can_add_images(self):
        """Yeah, go for it as long as the lot isn't sold"""
        if self.winning_price:
            return False
        return True

    @property
    def bids_can_be_removed(self):
        """True or False"""
        # Buy now ends the lot before the auction closes.
        if self.auction and self.ended and not self.auction.closed:
            return True
        if self.ended:
            return False
        if (
            not self.auction.is_online
            and self.auction.date_online_bidding_ends
            and self.auction.online_bidding != "disable"
            and timezone.now() > self.auction.date_online_bidding_ends
        ):
            return False
        return True

    @property
    def cannot_change_reason(self):
        """Reasons used for both editing and deleting"""
        if self.high_bidder:
            return "There are already bids placed on this lot"
        if self.winner or self.auctiontos_winner:
            return "This lot has sold"
        return False

    @property
    def cannot_be_edited_reason(self):
        if self.cannot_change_reason:
            return self.cannot_change_reason
        if self.auction:
            # Editable until lot submission ends.
            if timezone.now() > self.auction.lot_submission_end_date:
                return "Lot submission is over for this auction"
        return False

    @property
    def can_be_edited(self):
        """Whether this lot can be edited."""
        if self.cannot_be_edited_reason:
            return False
        return True

    @property
    def cannot_be_deleted_reason(self):
        if self.cannot_change_reason:
            return self.cannot_change_reason
        if self.auction and self.auction.is_online and self.auction.unsold_lot_fee:
            # Deletable until 24 hours before lot submission ends.
            if timezone.now() > self.auction.lot_submission_end_date - datetime.timedelta(hours=24):
                return "It's too late to delete lots in this auction"
        if self.auction and self.auction.unsold_lot_fee:
            # you have at most 24 hours to delete a lot
            if timezone.now() > self.date_posted + datetime.timedelta(hours=24):
                if timezone.now() < self.date_posted + datetime.timedelta(minutes=20):
                    pass  # you are allowed to delete very new lots
                else:
                    return "You can only delete auction lots in the first 24 hours after they have been created."
        return False

    @property
    def can_be_deleted(self):
        """Whether this lot can be deleted (not right before the auction ends)."""
        if self.cannot_be_deleted_reason:
            return False
        return True

    @property
    def bidding_allowed_on(self):
        """bidding is not allowed on very new lots"""
        first_bid_date = self.date_posted + datetime.timedelta(minutes=20)
        if self.auction:
            if self.auction.is_online and self.auction.date_start > first_bid_date:
                return self.auction.date_start
            if (
                not self.auction.is_online
                and self.auction.online_bidding != "disable"
                and self.auction.date_online_bidding_starts
                and self.auction.date_online_bidding_starts > first_bid_date
            ):
                return self.auction.date_online_bidding_starts
        return first_bid_date

    @property
    def bidding_error(self):
        """False if bidding is allowed, else an error message."""
        if self.banned:
            if self.ban_reason:
                return f"This lot has been removed: {self.ban_reason}"
            return "This lot has been removed"
        if self.tos_needed:
            return "The creator of this lot has not confirmed their pickup location for this auction."
        if self.auction:
            if self.auction.online_bidding == "disable":
                return "This auction doesn't allow online bidding"
            if not self.auction.started:
                return "Bidding hasn't opened yet for this auction"
            if (
                not self.auction.is_online
                and self.auction.date_online_bidding_ends
                and timezone.now() > self.auction.date_online_bidding_ends
            ):
                return "Online bidding has ended for this auction"
            if (
                not self.auction.is_online
                and self.auction.date_online_bidding_starts
                and timezone.now() < self.auction.date_online_bidding_starts
            ):
                return "Online bidding hasn't started yet for this auction"
            if self.auction.online_bidding == "buy_now_only" and not self.buy_now_price:
                return "This lot does not have a buy now price set, you can't buy it now"
        if self.deactivated:
            return "This lot has been deactivated by its owner"
        if self.bidding_allowed_on > timezone.now():
            difference = self.bidding_allowed_on - timezone.now()
            delta = difference.seconds
            unit = "second"
            if delta > 60:
                delta = delta // 60
                unit = "minute"
            if delta > 60:
                delta = delta // 60
                unit = "hour"
            if delta > 24:
                delta = delta // 24
                unit = "day"
            if delta != 1:
                unit += "s"
            return f"This lot is very new, you can bid on it in {delta} {unit}"
        return False

    @property
    def is_part_of_in_person_auction(self):
        # Issue #116: an early auction end would need Auction.date_end and a view to set it.
        if self.auction:
            if self.auction.is_online:
                return False
            else:
                return True
        return False

    @cached_property
    def ended(self):
        """Whether the lot has ended, for display. ``active`` is set from this by endauctions."""
        # lot attached to in person auctions do not end unless manually set
        if self.sold or self.banned or self.is_deleted:
            return True
        if self.is_part_of_in_person_auction:
            return False
        # all other lots end
        if timezone.now() > self.calculated_end:
            return True
        else:
            return False

    @property
    def minutes_to_end(self):
        """Minutes until bidding ends; 0 if ended."""
        if self.is_part_of_in_person_auction:
            return 999
        timedelta = self.calculated_end - timezone.now()
        seconds = timedelta.total_seconds()
        if seconds < 0:
            return 0
        minutes = seconds // 60
        return minutes

    @property
    def ending_soon(self):
        """2 hours before - used to send notifications about watched lots"""
        if self.is_part_of_in_person_auction:
            return False
        warning_date = self.calculated_end - datetime.timedelta(hours=2)
        if timezone.now() > warning_date:
            return True
        else:
            return False

    @property
    def ending_very_soon(self):
        """True when ending within a minute, so a notification is pushed."""
        if self.minutes_to_end < 1:
            return True
        return False

    @property
    def within_dynamic_end_time(self):
        """True when ending within 15 minutes, so late bids extend it."""
        if self.is_part_of_in_person_auction:
            return False
        if self.minutes_to_end < 15:
            return True
        else:
            return False

    @property
    def sealed_bid(self):
        if self.auction:
            if self.auction.sealed_bid:
                return True
        return False

    @property
    def price(self):
        """Price display"""
        logger.warning(
            "this is most likely safe to remove, it uses max_bid which should never be displayed to normal people.  I don't think it's used anywhere."
        )
        if self.winning_price:
            return self.winning_price
        return self.max_bid

    def _latest_bid_per_user_subquery(self):
        """Subquery for each user's latest bid pk (by -bid_time, -pk), to dedupe bids."""
        return (
            Bid.objects.exclude(is_deleted=True)
            .filter(
                lot_number=self.lot_number,
                user=OuterRef("user"),
            )
            .order_by("-bid_time", "-pk")
            .values("pk")[:1]
        )

    @cached_property
    def max_bid(self):
        """The highest bid amount. Never public."""
        allBids = (
            Bid.objects.exclude(is_deleted=True)
            .filter(
                lot_number=self.lot_number,
                last_bid_time__lte=self.calculated_end,
                amount__gte=self.reserve_price,
                pk=Subquery(self._latest_bid_per_user_subquery()),
            )
            .order_by("-amount", "last_bid_time")[:2]
        )
        try:
            # $1 more than the second highest bid
            bidPrice = allBids[0].amount
            return bidPrice
        except:
            return self.reserve_price

    @cached_property
    def bids(self):
        """Bids, highest first, one per user (their latest), as a list.

        From ``self.bid_set`` so lists can prefetch. A user's latest bid counts only if placed by the end and
        at least the reserve. Cached; ``Bid.save()`` clears it.
        """
        if self.pk is None:
            # Unsaved: no bids (bid_set would raise).
            return []
        related = self.bid_set.all()
        if related._result_cache is None:
            # Not prefetched: fetch users with the bids.
            related = related.select_related("user")
        latest_per_user = {}
        for bid in related:
            if bid.is_deleted:
                continue
            current = latest_per_user.get(bid.user_id)
            if current is None or (bid.bid_time, bid.pk) > (current.bid_time, current.pk):
                latest_per_user[bid.user_id] = bid
        calculated_end = self.calculated_end
        qualifying = [
            bid
            for bid in latest_per_user.values()
            if bid.last_bid_time <= calculated_end and bid.amount >= self.reserve_price
        ]
        qualifying.sort(key=lambda bid: (-bid.amount, bid.last_bid_time))
        return qualifying

    @property
    def high_bid(self):
        """returns the high bid amount for this lot"""
        if self.winning_price:
            return self.winning_price
        if self.sealed_bid:
            try:
                return self.bids[0].amount
            except IndexError:
                return 0
        else:
            if self.auction and self.auction.online_bidding == "buy_now_only" and not self.bids:
                if self.buy_now_price:
                    return self.buy_now_price
                return ""
            try:
                bids = self.bids
                # The highest bid wins; the second sets the price.
                if bids[0].amount == bids[1].amount:
                    return bids[0].amount
                else:
                    # One cent over the second, or $1 in whole-dollar auctions.
                    if self.auction and not self.auction.only_whole_dollar_bids:
                        bidPrice = bids[1].amount + Decimal("0.01")
                    else:
                        bidPrice = bids[1].amount + 1
                return bidPrice
            except IndexError:
                return self.reserve_price

    @property
    def high_bidder(self):
        """Name of the highest bidder"""
        if self.banned:
            return False
        try:
            bids = self.bids
            return bids[0].user
        except:
            return False

    @cached_property
    def all_page_views(self):
        """All page views of this lot."""
        return PageView.objects.filter(lot_number=self.lot_number)

    @cached_property
    def anonymous_views(self):
        return PageView.objects.filter(lot_number=self.lot_number, user_id__isnull=True).count()

    @cached_property
    def page_views(self):
        """Total page views, from the annotation when present; COUNT(*) otherwise."""
        annotated = getattr(self, "annotated_page_views", None)
        if annotated is not None:
            return annotated
        return self.all_page_views.count()

    @cached_property
    def ar_interaction_counts(self):
        """Distinct users who scanned, zoomed or fully zoomed this lot in AR (``ar_*`` PageView sources), plus
        a ``total``. One query.
        """
        rows = (
            PageView.objects.filter(lot_number=self.lot_number, source__in=("ar_scan", "ar_zoom", "ar_zoom_full"))
            .values("source")
            .annotate(n=Count("user", distinct=True))
        )
        by_source = {row["source"]: row["n"] for row in rows}
        counts = {
            "scanned": by_source.get("ar_scan", 0),
            "zoomed": by_source.get("ar_zoom", 0),
            "zoomed_full": by_source.get("ar_zoom_full", 0),
        }
        counts["total"] = sum(counts.values())
        return counts

    # Labels for our own ``src`` values; anything else shows raw.
    PAGE_VIEW_SOURCE_LABELS = {
        "": "Opened the lot page directly",
        "ar": '"Open lot page" from lot scanning',
        "ar_scan": "Scanned this lot's label",
        "ar_zoom": "Aimed at this label up close while scanning",
        "ar_zoom_full": "Held on the label until the lot card opened",
        "qr": "Scanned the printed QR code",
        "lot_list": "From a lot list",
        "recommended": "From recommended lots",
        "feedback": "From the leave-feedback page",
        "invoice_sold": "From an invoice (sold)",
        "invoice_bought": "From an invoice (bought)",
        "userpage": "From a user's page",
        "ban_page": "From the no-show page",
    }
    # AR sources are one row per (user, lot), so views equal people.
    AR_PAGE_VIEW_SOURCES = ("ar_scan", "ar_zoom", "ar_zoom_full")

    @cached_property
    def page_view_source_breakdown(self):
        """Page views grouped by ``src`` with unique viewers (users plus anonymous sessions), most first. One
        query. None and "" are merged.
        """
        rows = (
            PageView.objects.filter(lot_number=self.lot_number)
            .values("source")
            .annotate(
                views=Count("pk"),
                users=Count("user", distinct=True),
                sessions=Count("session_id", distinct=True, filter=Q(user__isnull=True)),
            )
        )
        merged = {}
        for row in rows:
            source = row["source"] or ""
            entry = merged.setdefault(
                source,
                {
                    "source": source,
                    "label": self.PAGE_VIEW_SOURCE_LABELS.get(source, source),
                    "is_ar_event": source in self.AR_PAGE_VIEW_SOURCES,
                    "views": 0,
                    "unique": 0,
                },
            )
            entry["views"] += row["views"]
            entry["unique"] += row["users"] + row["sessions"]
        return sorted(merged.values(), key=lambda entry: (-entry["views"], entry["label"]))

    @cached_property
    def number_of_bids(self):
        """How many users placed bids on this lot?"""
        return (
            Bid.objects.exclude(is_deleted=True)
            .filter(
                lot_number=self.lot_number,
                bid_time__lte=self.calculated_end,
                amount__gte=self.reserve_price,
            )
            .values("user")
            .distinct()
            .count()
        )

    @property
    def view_to_bid_ratio(self):
        """Bids per view: low means interesting but unwanted."""
        if self.page_views:
            return self.number_of_bids / self.page_views
        else:
            return 0

    @property
    def chat_allowed(self):
        if not self.is_chat_allowed:
            return False
        if self.auction:
            if not self.auction.is_chat_allowed:
                return False
        return True

    @cached_property
    def image_count(self):
        """Count the number of images associated with this lot"""
        return len(self.images)

    @property
    def multimedia_count(self):
        """Images plus a video reference link."""
        count = 0
        if self.video_link:
            count = 1
        return self.image_count + count

    @cached_property
    def images(self):
        """All images, from use_images_from if set, as a list sorted primary first (prefetchable)."""
        source = self.use_images_from if self.use_images_from_id else self
        if source.pk is None:
            # Unsaved: no images.
            return []
        return sorted(source.lotimage_set.all(), key=lambda image: (not image.is_primary, image.createdon))

    @cached_property
    def auto_image(self):
        """Grab an automatically generated image"""
        if not self.auction:
            return None
        if self.user and not self.user.userdata.auto_add_images:
            return None
        if not self.auction.auto_add_images:
            return None
        return find_image(self.lot_name, self.user, self.auction)

    @cached_property
    def thumbnail(self):
        """The list thumbnail or None, cached (templates ask three times)."""
        for image in self.images:
            if image.is_primary:
                return image
        return self.auto_image

    def get_absolute_url(self):
        if self.slug:
            return reverse("lot_by_pk_and_slug", kwargs={"pk": self.lot_number, "slug": self.slug})
        return reverse("lot_by_pk", kwargs={"pk": self.lot_number})

    @property
    def lot_number_display(self):
        if self.auction and self.auction.use_seller_dash_lot_numbering and self.custom_lot_number:
            return self.custom_lot_number
        # note that custom lot numbers are effectively disabled here
        if self.auction and self.lot_number_int:
            return self.lot_number_int
        return self.lot_number

    @cached_property
    def lot_link(self):
        """Simplest link to access this lot with"""
        # Real pk URLs; lot_number only for unsaved instances.
        lot_pk = self.pk if self.pk is not None else self.lot_number
        if self.auction:
            lot_number_display = self.lot_number_display
            try:
                if self.slug:
                    return reverse(
                        "lot_in_auction_with_slug",
                        kwargs={
                            "slug": self.auction.slug,
                            "custom_lot_number": lot_number_display,
                            "lot_slug": self.slug,
                        },
                    )
                return reverse(
                    "lot_in_auction",
                    kwargs={
                        "slug": self.auction.slug,
                        "custom_lot_number": lot_number_display,
                    },
                )
            except NoReverseMatch:
                # Invalid route pieces: fall back to a pk URL.
                logger.debug("Falling back to PK lot URL for lot=%s auction=%s", self.pk, self.auction_id)
        if self.slug:
            return reverse("lot_by_pk_and_slug", kwargs={"pk": lot_pk, "slug": self.slug})
        return reverse("lot_by_pk", kwargs={"pk": lot_pk})

    @cached_property
    def full_lot_link(self):
        """Full domain name URL for this lot"""
        current_site = Site.objects.get_current()
        return f"{current_site.domain}{self.lot_link}"

    @cached_property
    def qr_code(self):
        """Full domain name URL used to for QR codes"""
        current_site = Site.objects.get_current()
        return f"https://{current_site.domain}{reverse('lot_by_pk_qr', kwargs={'pk': self.pk})}"

    @property
    def seller_string(self):
        if self.auctiontos_seller:
            return f"Seller: {self.auctiontos_seller.name}"
        return ""

    @property
    def reserve_and_buy_now_info(self):
        result = ""
        if self.reserve_price > self.auction.minimum_bid and not self.sold:
            result += f" Min bid: {self.currency_symbol}{self.reserve_price}"
        if self.buy_now_price and not self.sold:
            result += f" Buy now: {self.currency_symbol}{self.buy_now_price}"
        return result

    @property
    def label_line_0(self):
        """Used for printed labels"""
        result = f"<b>LOT: {self.lot_number_display}</b>"
        if not self.winning_price:
            if self.donation:
                result += " (D) "
            if self.auction.advanced_lot_adding or self.quantity > 1:
                result += f" QTY: {self.quantity}"
        return result

    @property
    def label_line_1(self):
        """Used for printed labels"""
        result = f"{self.lot_name}"
        return result

    @property
    def label_line_2(self):
        """Used for printed labels"""
        if self.auctiontos_winner:
            return f"Winner: {self.auctiontos_winner.name}"
        if self.auction and self.auction.multi_location:
            return self.reserve_and_buy_now_info
        return self.seller_string

    @cached_property
    def label_line_3(self):
        """Used for printed labels"""
        result = ""
        if self.auction and self.auction.multi_location:
            if self.auctiontos_winner:
                return self.auctiontos_winner.pickup_location
            else:
                # Unsold: let the auctioneer choose the pickup location.
                locations = self.auction.location_qs
                for location in locations:
                    result += "  __" + location.short_name
        else:
            return self.reserve_and_buy_now_info
        return result

    @property
    def seller_ip(self):
        try:
            return self.user.userdata.last_ip_address
        except:
            return None

    @cached_property
    def bidder_ip_same_as_seller(self):
        if self.seller_ip:
            bids = (
                Bid.objects.exclude(is_deleted=True)
                .filter(lot_number__pk=self.pk, user__userdata__last_ip_address=self.seller_ip)
                .count()
            )
            if bids:
                return bids
        return None

    @property
    def reference_link_domain(self):
        if self.reference_link:
            pattern = r"https?://(?:www\.)?([a-zA-Z0-9.-]+)\.([a-zA-Z]{2,6})"
            # Use the regex pattern to find the matches in the URL
            match = re.search(pattern, self.reference_link)
            if match:
                base_domain = match.group(1)
                extension = match.group(2)
                return f"{base_domain}.{extension}"
        return ""

    @property
    def video_link(self):
        if self.reference_link:
            pattern = r"(?:https?://)?(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([\w-]{11})"
            match = re.search(pattern, self.reference_link)
            if match:
                return match.group(1)
        return None

    def create_update_invoices(self):
        """Call whenever ending this lot, or when creating it"""
        if self.auction and self.winner and not self.auctiontos_winner:
            winner_email = (self.winner.email or "").strip()
            tos_filter = Q(user=self.winner)
            if winner_email:
                tos_filter |= Q(email__iexact=winner_email)
            tos = AuctionTOS.objects.filter(tos_filter, auction=self.auction).order_by("-createdon").first()
            self.auctiontos_winner = tos
            self.save()
        if self.auction and self.auctiontos_winner:
            invoice = Invoice.objects.filter(auctiontos_user=self.auctiontos_winner, auction=self.auction).first()
            if not invoice:
                invoice = Invoice.objects.create(auctiontos_user=self.auctiontos_winner, auction=self.auction)
            invoice.recalculate()
        if self.auction and self.auctiontos_seller:
            invoice = Invoice.objects.filter(auctiontos_user=self.auctiontos_seller, auction=self.auction).first()
            if not invoice:
                invoice = Invoice.objects.create(auctiontos_user=self.auctiontos_seller, auction=self.auction)
            invoice.recalculate()
            if self.auction.use_check_in_mode and not self.auctiontos_seller.checked_in:
                seller = self.auctiontos_seller
                seller.checked_in = timezone.now()
                update_fields = ["checked_in"]
                if not seller.bidding_allowed:
                    seller.bidding_allowed = True
                    update_fields.append("bidding_allowed")
                seller.save(update_fields=update_fields)
                self.auction.create_history(
                    applies_to="USERS",
                    action=f"Checked in {seller.name} (lot sold)",
                )

    @property
    def category(self):
        """Short category name for labels; usually use `lot.species_category`."""
        if self.species_category and self.species_category.name != "Uncategorized":
            return self.species_category.name_on_label or self.species_category
        return ""

    @property
    def scientific_name(self):
        """The lot's scientific name, or "" (no species, or the auction has the field off).

        One rule for the lot page, AR, map, CSV and label. The species is kept when the setting is off.
        Includes the cultivar.
        """
        if not self.species:
            return ""
        if self.auction and not self.auction.use_scientific_name:
            return ""
        return self.species.full_scientific_name

    @property
    def lot_name_says_the_species(self):
        """True when the lot name already contains the scientific name (whole normalized words; genus-only
        species count; cultivar optional).
        """
        if not self.scientific_name or not self.lot_name:
            return False
        typed = normalize_species_name(self.lot_name)
        name = normalize_species_name(self.species.scientific_name)
        if not typed or not name:
            return False
        return f" {name} " in f" {typed} "

    @property
    def scientific_name_line(self):
        """The scientific name to print under the lot name, or "" when it would repeat it. Storage and exports
        use :attr:`scientific_name`.
        """
        return "" if self.lot_name_says_the_species else self.scientific_name

    @property
    def common_name_line(self):
        """The common name to print under a lot named after its species; blank when the seller used a common
        name. Cultivars fall back to the parent's name.
        """
        if not self.scientific_name or not self.lot_name_says_the_species:
            return ""
        species = self.species
        name = species.common_name or (species.parent.common_name if species.parent_id else "")
        # Don't print the words already on the lot.
        if name and normalize_species_name(name) in normalize_species_name(self.lot_name):
            return ""
        return name

    @property
    def category_from_species(self):
        """The category the species implies, or None. Varieties inherit their parent's; Uncategorized counts as none."""
        if not self.species_id:
            return None
        category = self.species.category or (self.species.parent.category if self.species.parent_id else None)
        if category and category.name != "Uncategorized":
            return category
        return None

    @property
    def donation_label(self):
        if self.donation and self.auction.use_donation_field:
            return "(D)"
        return ""

    @property
    def min_bid_label(self):
        if self.reserve_price > self.auction.minimum_bid and not self.sold:
            return f"Min: {self.currency_symbol}{self.reserve_price}"
        return ""

    @property
    def buy_now_label(self):
        if self.buy_now_price and not self.sold:
            return f"Buy: {self.currency_symbol}{self.buy_now_price}"
        return ""

    @property
    def quantity_label(self):
        if self.auction and self.auction.use_quantity_field or self.quantity > 1:
            return f"QTY: {self.quantity}"
        return ""

    @property
    def custom_checkbox_label(self):
        if self.auction.custom_checkbox_name and self.auction.use_custom_checkbox_field and self.custom_checkbox:
            return self.auction.custom_checkbox_name
        return ""

    @property
    def custom_dropdown_label(self):
        if self.auction.use_custom_dropdown_field != "disable" and self.custom_dropdown:
            return self.custom_dropdown
        return ""

    @property
    def i_bred_this_fish_label(self):
        if self.i_bred_this_fish and self.auction.use_i_bred_this_fish_field and not self.sold:
            return "(B)"
        return ""

    @property
    def auction_date(self):
        return self.auction.date_start.strftime("%b %Y")

    @property
    def description_label(self):
        """Strip all html except <br> from summernote description"""
        return re.sub(r"(?!<br\s*/?>)<.*?>", "", self.summernote_description)


class BapAward(models.Model):
    """A record of BAP/HAP/CAP points awarded to a club member for a lot."""

    club_member = models.ForeignKey(ClubMember, on_delete=models.CASCADE, related_name="bap_awards")
    date = models.DateField()
    points = models.IntegerField(default=0, help_text="BAP points awarded.")
    hap_points = models.IntegerField(default=0, help_text="HAP points awarded.")
    cap_points = models.IntegerField(default=0, help_text="CAP (culture) points awarded.")
    lot = models.OneToOneField(
        Lot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bap_award",
    )
    awarded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bap_awards_given",
        help_text="The user who manually awarded these points. Null if auto-awarded.",
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        parts = []
        if self.points:
            parts.append(f"{self.points} BAP")
        if self.hap_points:
            parts.append(f"{self.hap_points} HAP")
        if self.cap_points:
            parts.append(f"{self.cap_points} CAP")
        label = "/".join(parts) if parts else "0"
        result = f"{label} points"
        if self.lot_id:
            result += f" for {self.lot}"
        if self.notes:
            result += f" ({self.notes})"
        return result

    @staticmethod
    def recalculate_member_points(member):
        """Recalculate a member's all-time and YTD BAP/HAP/CAP totals."""
        from django.utils import timezone

        # localtime: `date` is a DateField in the site's calendar (matches reset_yearly_bap_counters).
        this_year = timezone.localtime().year
        awards = BapAward.objects.filter(club_member=member).exclude(lot__is_deleted=True).exclude(lot__banned=True)
        bap = hap = cap = bap_ytd = hap_ytd = cap_ytd = 0
        for a in awards:
            is_ytd = a.date.year == this_year
            bap += a.points
            hap += a.hap_points
            cap += a.cap_points
            if is_ytd:
                bap_ytd += a.points
                hap_ytd += a.hap_points
                cap_ytd += a.cap_points
        ClubMember.objects.filter(pk=member.pk).update(
            bap_points=bap,
            hap_points=hap,
            culture_points=cap,
            bap_points_ytd=bap_ytd,
            hap_points_ytd=hap_ytd,
            culture_points_ytd=cap_ytd,
        )
        member.refresh_from_db()
        member.maybe_assign_discord_role()

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        BapAward.recalculate_member_points(self.club_member)

    def delete(self, *args, **kwargs):
        member = self.club_member
        result = super().delete(*args, **kwargs)
        BapAward.recalculate_member_points(member)
        return result


class ClubBapCategoryOverride(models.Model):
    """Per-club, per-category point overrides; precede Club.points_per_lot."""

    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="bap_category_overrides")
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="bap_overrides")
    points = models.IntegerField(default=0)
    created_on = models.DateField(auto_now_add=True)

    class Meta:
        unique_together = ("club", "category")

    def __str__(self):
        return f"{self.club} — {self.category}: {self.points} pts"


class ClubBapGenusOverride(models.Model):
    """Per-club, per-genus point overrides (a genus rule beats a category rule).

    A separate model, not a nullable genus column on the category override: MariaDB treats NULLs as
    distinct in unique constraints. Matches :attr:`Species.genus`.
    """

    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="bap_genus_overrides")
    genus = models.CharField(max_length=100)
    genus.help_text = "The first half of a scientific name, e.g. Tropheus.  Applies to every species in it."
    points = models.IntegerField(default=0)
    created_on = models.DateField(auto_now_add=True)

    def save(self, *args, **kwargs):
        # Capitalized as FishBase writes it.
        self.genus = self.genus.strip().capitalize()
        super().save(*args, **kwargs)

    class Meta:
        unique_together = ("club", "genus")
        ordering = ["genus"]

    def __str__(self):
        return f"{self.club} — {self.genus}: {self.points} pts"


class Invoice(CachedPropertiesMixin, models.Model):
    """The amount you get paid or owe the club for an auction."""

    auction = models.ForeignKey(Auction, blank=True, null=True, on_delete=models.SET_NULL)
    auctiontos_user = models.ForeignKey(
        AuctionTOS, blank=True, null=True, on_delete=models.CASCADE, related_name="auctiontos"
    )
    club = models.ForeignKey(
        "Club", blank=True, null=True, on_delete=models.SET_NULL, related_name="membership_invoices"
    )
    club_member = models.ForeignKey(
        "ClubMember", blank=True, null=True, on_delete=models.SET_NULL, related_name="invoices"
    )
    buyer = models.ForeignKey(
        settings.AUTH_USER_MODEL, blank=True, null=True, on_delete=models.SET_NULL, related_name="membership_invoices"
    )
    date = models.DateTimeField(auto_now_add=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=(
            ("DRAFT", "Open"),
            ("UNPAID", "Ready"),
            ("PAID", "Paid"),
        ),
        default="DRAFT",
    )
    date_paid = models.DateTimeField(null=True, blank=True)
    date_paid.help_text = (
        "When this invoice first became PAID, i.e. when the cash actually changed hands. Set "
        "automatically the first time the invoice is saved as PAID and never overwritten "
        "afterwards, so the club ledger (ClubMoney) can book on a cash basis. Left unset until "
        "then; recorded online payments (InvoicePayment) take precedence over this as the cash date."
    )
    opened = models.BooleanField(default=False)
    printed = models.BooleanField(default=False)
    email_sent = models.BooleanField(default=False)
    invoice_notification_due = models.DateTimeField(null=True, blank=True)
    invoice_notification_due.help_text = (
        "When set, a celery task will send an invoice notification email after this time"
    )
    no_login_link = models.CharField(
        max_length=255,
        default=uuid_module.uuid4,
        blank=True,
        verbose_name="This link will be emailed to the user, allowing them to view their invoice directly without logging in",
    )
    calculated_total = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    calculated_total.help_text = "This field is set automatically, you shouldn't need to manually change it"
    memo = models.CharField(max_length=500, blank=True, null=True, default="")
    memo.help_text = "Only other auction admins can see this"
    renewal_needed = models.BooleanField(default=False)
    renewal_manually_set = models.BooleanField(default=False)
    renewal_processed = models.BooleanField(default=False)

    class Meta:
        indexes = [
            # "Most recent invoice" without a filesort.
            models.Index(fields=["auctiontos_user", "-date"], name="invoice_tos_recent_idx"),
        ]

    @cached_property
    def currency(self):
        """This invoice's currency, from the club's seller or the auction creator."""
        # Club invoices use the club seller's currency for PayPal/Square orders.
        club = self.club or (self.auction.club if self.auction else None)
        if club:
            seller = club.effective_paypal_seller or club.effective_square_seller
            if seller and seller.user:
                return seller.user.userdata.currency
        if self.auction and self.auction.created_by:
            return self.auction.created_by.userdata.currency
        return "USD"

    @cached_property
    def currency_symbol(self):
        """Get the currency symbol for this invoice"""
        return get_currency_symbol(self.currency)

    @cached_property
    def paypal_credentials(self):
        """The club's own PayPal credentials governing this invoice, or ``None`` (use the site app)."""
        club = self.club or (self.auction.club if self.auction else None)
        return club.paypal_credentials if club else None

    @cached_property
    def show_payment_button(self):
        """True if we can show the PayPal or Square button"""
        # A club's own credentials count even without site PayPal keys.
        paypal_configured = bool((settings.PAYPAL_CLIENT_ID and settings.PAYPAL_SECRET) or self.paypal_credentials)
        # Square now requires OAuth - just check if OAuth is configured
        square_configured = getattr(settings, "SQUARE_APPLICATION_ID", None) and getattr(
            settings, "SQUARE_CLIENT_SECRET", None
        )

        if not (paypal_configured or square_configured):
            return False
        if self.status == "PAID":
            return False
        # A rounded-away residual owes nothing.
        if self.rounded_net_after_payments >= 0:
            return False
        if self.club:
            return self.show_paypal_button or self.show_square_button
        if not self.auction:
            return False
        # Club auctions use club-level payment config, not the per-auction flag.
        if self.auction.club:
            return self.show_paypal_button or self.show_square_button

        # Check if auction allows any payment method
        has_payment_method = False
        if self.auction.enable_online_payments:
            if not self.auction.created_by.userdata.is_trusted:
                return False
            if (
                not self.auction.created_by.is_superuser
                and not self.auction.created_by.userdata.paypal_enabled
                and not self.auction.paypal_information
            ):
                pass  # Check Square
            else:
                has_payment_method = True

        if self.auction.enable_square_payments:
            if not self.auction.created_by.userdata.is_trusted:
                return False
            # Square requires OAuth - check if seller has linked account
            if not self.auction.created_by.userdata.square_enabled or not self.auction.square_information:
                pass
            else:
                has_payment_method = True

        return has_payment_method

    @cached_property
    def show_paypal_button(self):
        """True if we can show specifically the PayPal button"""
        # Site app or club credentials required.
        if not (settings.PAYPAL_CLIENT_ID and settings.PAYPAL_SECRET) and not self.paypal_credentials:
            return False
        if self.status == "PAID":
            return False
        # A rounded-away residual owes nothing.
        if self.rounded_net_after_payments >= 0:
            return False
        if self.club:
            if self.club.uses_site_paypal or self.club.uses_own_paypal_credentials:
                return True
            seller = self.club.effective_paypal_seller
            if not seller or not seller.paypal_merchant_id:
                return False
            if not seller.user.userdata.is_trusted:
                return False
            return True
        if not self.auction:
            return False
        # Club site or own-credential PayPal supersedes the per-auction flag.
        if self.auction.club and (self.auction.club.uses_site_paypal or self.auction.club.uses_own_paypal_credentials):
            return True
        if not self.auction.enable_online_payments:
            return False
        if not self.auction.created_by.userdata.is_trusted:
            return False
        if (
            not self.auction.created_by.is_superuser
            and not self.auction.created_by.userdata.paypal_enabled
            and not self.auction.paypal_information
        ):
            return False
        return True

    @cached_property
    def show_square_button(self):
        """True if the Square button can show (OAuth-linked seller required)."""
        if not (getattr(settings, "SQUARE_APPLICATION_ID", None) and getattr(settings, "SQUARE_CLIENT_SECRET", None)):
            return False
        if self.status == "PAID":
            return False
        # A rounded-away residual owes nothing.
        if self.rounded_net_after_payments >= 0:
            return False
        if self.club:
            seller = self.club.effective_square_seller
            if not seller or not seller.square_merchant_id:
                return False
            if not seller.user.userdata.is_trusted:
                return False
            return True
        if not self.auction:
            return False
        # A club's linked Square seller supersedes the per-auction flag.
        if self.auction.club:
            seller = self.auction.club.effective_square_seller
            if seller and seller.square_merchant_id and seller.user.userdata.is_trusted:
                return True
        if not self.auction.enable_square_payments:
            return False
        if not self.auction.created_by.userdata.is_trusted:
            return False
        if not self.auction.created_by.userdata.square_enabled:
            return False
        if not self.auction.square_information:
            return False
        return True

    @cached_property
    def reason_for_payment_not_available(self):
        """Why payment isn't available (use after show_payment_button, when the button is greyed out)."""
        if not self.auction:
            return None
        if self.auction.is_online and not self.auction.closed and self.status == "DRAFT":
            timedelta = self.dynamic_end - timezone.now()
            seconds = timedelta.total_seconds()
            if seconds > 0:
                minutes = seconds // 60
                return f"This auction hasn't ended yet.  You'll be able to pay in {minutes} minutes."
        if not self.auction.is_online:
            # In-person invoices always show a pay button.
            pass

    @cached_property
    def soft_descriptor(self):
        """Short merchant descriptor for PayPal (soft descriptor)."""
        if self.auction and self.auction.paypal_information == "admin":
            return settings.NAVBAR_BRAND
        return None

    def sum_adjusments(self, adjustment_type):
        return self.adjustment_totals.get(adjustment_type) or 0

    @cached_property
    def adjustment_totals(self):
        """Every adjustment type's total in one GROUP BY."""
        return {
            row["adjustment_type"]: row["total"]
            for row in self.adjustments.values("adjustment_type").order_by().annotate(total=Sum("amount"))
        }

    @cached_property
    def adjustments(self):
        return InvoiceAdjustment.objects.filter(invoice=self).order_by("-createdon")

    @cached_property
    def flat_value_adjustments(self):
        return self.sum_adjusments("DISCOUNT") - self.sum_adjusments("ADD")

    @cached_property
    def percent_value_adjustments(self):
        return self.sum_adjusments("ADD_PERCENT") - self.sum_adjusments("DISCOUNT_PERCENT")

    @cached_property
    def changed_adjustments(self):
        """Non-zero adjustments with ``invoice`` set, so their display doesn't re-fetch this invoice."""
        adjustments = list(self.adjustments.exclude(amount=0))
        for adjustment in adjustments:
            adjustment.invoice = self
        return adjustments

    @cached_property
    def membership_fee_amount(self):
        club = self.club or (self.auction.club if self.auction else None)
        if not (club and self.renewal_needed and club.membership_annual_fee):
            return Decimal("0.00")
        return Decimal(club.membership_annual_fee)

    @cached_property
    def club_member_for_auction(self):
        """The ClubMember for this invoice's user in the auction's club, or None."""
        if not self.auction or not self.auction.club or not self.auctiontos_user:
            return None
        return self.auctiontos_user.club_member_record

    @cached_property
    def member_has_paypal_subscription(self):
        """True when the member auto-renews through PayPal (disables the renewal checkbox)."""
        member = self.club_member_for_auction
        return bool(member and member.paypal_subscription_id)

    @cached_property
    def treat_as_club_member(self):
        """True when club member benefits apply: membership current, or this invoice renews it."""
        if not self.auction or not self.auction.club:
            return False
        if self.renewal_needed:
            return True
        member = self.club_member_for_auction
        return bool(member and member.is_paid_member)

    @cached_property
    def membership_status_for_invoice(self):
        if not self.auction or not self.auction.club:
            return "No club"
        if not self.auctiontos_user:
            return "No bidder"
        member = self.club_member_for_auction
        if not member:
            return "No membership"
        if not member.membership_last_paid:
            return "Expired"
        expiration_date = member.membership_expiration_date
        if not expiration_date:
            return "Unknown"
        days_until_expiration = (expiration_date - timezone.now().date()).days
        if days_until_expiration < 0:
            return f"Expired {abs(days_until_expiration)} day(s) ago"
        if days_until_expiration <= 14:
            return f"Expires in {days_until_expiration} day(s)"
        return f"Active (expires in {days_until_expiration} day(s))"

    def recalculate(self):
        """Store the current net in calculated_total, unless the invoice is PAID (frozen).

        Call it whenever lots change; it saves. A PAID total is booked history and isn't re-derived from
        current settings; only un-paying thaws it. Refunds are unaffected: they're negative payments, and
        ``calculated_total`` never included payments.
        """
        if self.pk and Invoice.objects.filter(pk=self.pk, status="PAID").exists():
            return
        # Everything is cached on the instance, and the caller is here because something changed.
        self.invalidate_cached_properties()
        self.calculated_total = self.rounded_net
        self.save()

    @cached_property
    def total_adjustment_amount(self):
        """Subtotal minus rounded net: rounding, adjustments, first-bid payouts."""
        return Decimal(self.subtotal) - Decimal(self.rounded_net)

    @cached_property
    def subtotal(self):
        """don't call this directly, use self.net or another property instead"""
        return Decimal(self.total_sold) - Decimal(self.total_bought)

    @cached_property
    def first_bid_payout(self):
        try:
            if self.auction.first_bid_payout:
                if self.lots_bought:
                    return self.auction.first_bid_payout
        except:
            pass
        return 0

    @cached_property
    def club_member_discount(self):
        """Like first_bid_payout, for paid (or renewing) club members who bought at least one lot."""
        if not self.auction or not self.auction.club_member_discount:
            return 0
        if not self.lots_bought:
            return 0
        if not self.treat_as_club_member:
            return 0
        return self.auction.club_member_discount

    @cached_property
    def registration_fee_amount(self):
        """Flat registration fee on every invoice; the alternate fee for alternate-split users. Applies whether
        or not they bought.
        """
        auction = self.auction
        if not auction:
            return Decimal("0.00")
        tos = self.auctiontos_user
        if auction.uses_alternate_split and tos and tos.is_club_member:
            return Decimal(auction.registration_fee_for_club_members or 0)
        return Decimal(auction.registration_fee or 0)

    @cached_property
    def tax(self):
        totals = self.bought_lots_queryset.aggregate(
            total_final=Coalesce(
                Sum(
                    "final_price",
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                ),
                Value(Decimal(0.00)),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            )
        )
        total_final = totals["total_final"] or Decimal(0.00)
        rate = Decimal(self.auction.tax or 0 if self.auction else 0) / Decimal(100)
        tax_amount = total_final * rate
        return tax_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @cached_property
    def manual_adjustment_amount(self):
        """Net value of manual adjustments (flat plus legacy percent). The percent applies to the running base
        ``net`` builds (subtotal, first-bid payout, member discount, flat adjustments), shared with
        ``sync_club_money`` so they agree. Decimal.
        """
        percent_base = (
            Decimal(self.subtotal)
            + Decimal(self.first_bid_payout)
            + Decimal(self.club_member_discount)
            + Decimal(self.flat_value_adjustments)
        )
        return Decimal(self.flat_value_adjustments) + (
            percent_base * Decimal(self.percent_value_adjustments) / Decimal(100)
        )

    @cached_property
    def net(self):
        """Total bought and sold, payout promotions, member discount, manual adjustments, membership and
        registration fees.
        """
        subtotal = Decimal(self.subtotal)
        subtotal += Decimal(self.first_bid_payout)
        subtotal += Decimal(self.club_member_discount)
        # Flat and legacy percent adjustments on the running base (see manual_adjustment_amount).
        subtotal += Decimal(self.manual_adjustment_amount)
        subtotal -= Decimal(self.membership_fee_amount)
        subtotal -= Decimal(self.tax)
        subtotal -= Decimal(self.registration_fee_amount)
        if not subtotal:
            subtotal = 0
        return Decimal(subtotal)

    @cached_property
    def net_after_payments(self):
        """negative number means they owe the club payment"""
        return self.net + self.total_payments

    @cached_property
    def user_should_be_paid(self):
        """True when the club owes the user (positive net)."""
        if self.net > 0:
            return True
        else:
            return False

    @cached_property
    def rounded_net(self):
        """Rounded in the customer's favor, so the club handles whole dollars only."""
        if not self.auction or not self.auction.invoice_rounding:
            return self.net
        rounded = round(self.net)
        if self.user_should_be_paid:
            if self.net > rounded:
                # we rounded down against the customer
                return Decimal(rounded + 1)
            else:
                return Decimal(rounded)
        else:
            if self.net <= rounded:
                return Decimal(rounded)
            else:
                return Decimal(rounded + 1)

    @cached_property
    def absolute_amount(self):
        """Give the absolute value of the invoice's net amount"""
        return abs(self.rounded_net)

    @cached_property
    def sold_lots_queryset(self):
        """Simple qs containing all lots SOLD by this user in this auction"""
        if not self.auctiontos_user:
            return add_price_info(Lot.objects.none())
        return add_price_info(
            Lot.objects.filter(
                auctiontos_seller=self.auctiontos_user,
                auction=self.auction,
                is_deleted=False,
            )
            # Each row prints a lot number, category and winner's pickup location.
            .select_related(
                "auction",
                "species_category",
                "auctiontos_winner__pickup_location",
                "auctiontos_seller__pickup_location",
            )
            .order_by("pk")
        )

    @cached_property
    def bought_lots_queryset(self):
        """Simple qs containing all lots BOUGHT by this user in this auction"""
        base = (
            Lot.objects.filter(
                winning_price__isnull=False,
                auctiontos_winner=self.auctiontos_user,
                is_deleted=False,
                # Banned lots are never charged.
                banned=False,
            )
            .select_related(
                "auction",
                "species_category",
                "auctiontos_seller__pickup_location",
                "auctiontos_winner__pickup_location",
            )
            .order_by("pk")
            if self.auctiontos_user
            else Lot.objects.none()
        )
        return (
            base
            # Use Decimal math to avoid float rounding
            .annotate(
                final_price=ExpressionWrapper(
                    Cast(F("winning_price"), DecimalField(max_digits=12, decimal_places=2))
                    * (
                        (
                            Value(Decimal("100.00"))
                            - Cast(F("partial_refund_percent"), DecimalField(max_digits=5, decimal_places=2))
                        )
                        / Value(Decimal("100.00"))
                    ),
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                )
            ).annotate(
                tax=ExpressionWrapper(
                    Cast(F("final_price"), DecimalField(max_digits=12, decimal_places=2))
                    * Coalesce(
                        Cast(F("auction__tax"), DecimalField(max_digits=5, decimal_places=2)),
                        Value(Decimal(0)),
                        output_field=DecimalField(max_digits=5, decimal_places=2),
                    )
                    / Value(Decimal("100.00")),
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                )
            )
        )

    @cached_property
    def sold_lots_queryset_sorted(self):
        try:
            return sorted(self.sold_lots_queryset, key=lambda t: str(t.winner_location))
        except:
            return self.sold_lots_queryset

    @cached_property
    def lots_sold(self):
        """Lots the user tried to sell on this invoice, unsold included."""
        return len(self.sold_lots_queryset)

    @cached_property
    def lots_sold_successfully(self):
        """Queryset of lots the user sold in this invoice (unsold lots not included)"""
        return self.sold_lots_queryset.filter(auctiontos_winner__isnull=False)

    @cached_property
    def lots_sold_successfully_count(self):
        """Lots the user sold on this invoice."""
        return self.lots_sold_successfully.count()

    @cached_property
    def lot_labels(self):
        """Online auctions label only sold lots; in-person, all submitted lots."""
        if self.is_online:
            return self.lots_sold_successfully
        else:
            return self.sold_lots_queryset

    @cached_property
    def unsold_lots(self):
        """Lots not sold (possibly winner not set yet)."""
        return self.sold_lots_queryset.exclude(auctiontos_winner__isnull=False).count()

    @cached_property
    def unsold_non_donation_lots(self):
        """In person only: unsold non-donation lots (possibly winner not set yet)."""
        if self.is_online:
            return 0
        # active=True: marking unsold deactivates a lot; this drives the invoice warning.
        return self.sold_lots_queryset.filter(
            active=True, auctiontos_winner__isnull=True, donation=False, banned=False
        ).count()

    @cached_property
    def total_sold_gross(self):
        """Total winning price of all lots sold"""
        return self.sold_lots_queryset.aggregate(total=Sum("winning_price"))["total"] or 0

    @cached_property
    def total_sold(self):
        """Seller's cut of all lots sold"""
        return self.sold_lots_queryset.aggregate(total_sold=Sum("your_cut"))["total_sold"] or 0

    @cached_property
    def total_sold_club_cut(self):
        """Club's cut of all lots sold"""
        return self.sold_lots_queryset.aggregate(total=Sum("club_cut"))["total"] or 0

    @cached_property
    def lots_bought(self):
        """Return number of lots the user bought in this invoice"""
        return len(self.bought_lots_queryset)

    @cached_property
    def total_bought(self):
        return self.bought_lots_queryset.aggregate(total_bought=Sum("final_price"))["total_bought"] or 0

    @cached_property
    def total_donations(self):
        """Total value of all donated lots"""
        return (
            self.sold_lots_queryset.filter(winning_price__isnull=False, donation=True).aggregate(
                total=Sum("winning_price")
            )["total"]
            or 0
        )

    @cached_property
    def location(self):
        """Pickup location selected by the user"""
        if self.auctiontos_user:
            return self.auctiontos_user.pickup_location
        return None

    @cached_property
    def contact_email(self):
        if self.location:
            if self.location.pickup_location_contact_email:
                return self.location.pickup_location_contact_email
        if self.auction:
            return self.auction.created_by.email
        if self.club:
            seller = self.club.effective_paypal_seller or self.club.effective_square_seller
            if seller and seller.user:
                return seller.user.email
            if self.club.contact_email:
                return self.club.contact_email
        return None

    @cached_property
    def has_refunds(self):
        """Check if this invoice has any refunds (negative payment amounts)"""
        return self.payments.filter(amount__lt=0).exists()

    @cached_property
    def rounded_net_after_payments(self):
        """net_after_payments rounded for cash: with refunds and under $1 it's $0; otherwise rounded in the
        customer's favor.
        """
        if not self.auction or not self.auction.invoice_rounding:
            return self.net_after_payments

        if self.has_refunds and abs(self.net_after_payments) < 1:
            return Decimal("0.00")

        # round() is banker's rounding.
        rounded = round(self.net_after_payments)

        if self.net_after_payments > 0:  # Club owes user (positive)
            # Round up in customer's favor (they get more)
            if self.net_after_payments > rounded:
                return Decimal(rounded + 1)
            else:
                return Decimal(rounded)
        else:  # User owes club (negative)
            # Round up (towards zero) in customer's favor (they owe less)
            if self.net_after_payments <= rounded:
                return Decimal(rounded)
            else:
                return Decimal(rounded + 1)

    @cached_property
    def rounding_adjustment(self):
        """The rounding adjustment line item, or None."""
        if not self.auction or not self.auction.invoice_rounding:
            return None

        # Only with refunds and a fraction under $1.
        if self.has_refunds and abs(self.net_after_payments) < 1 and self.net_after_payments != 0:
            # The adjustment is the difference between exact and rounded
            return self.net_after_payments - self.rounded_net_after_payments

        return None

    @cached_property
    def invoice_summary_short(self):
        result = ""
        # Use rounded value for display when invoice_rounding is enabled
        display_amount = (
            self.rounded_net_after_payments
            if (self.auction and self.auction.invoice_rounding)
            else self.net_after_payments
        )

        if display_amount > 0:
            return result + f"needs to be paid ${abs(display_amount):.2f}"
        if display_amount < 0:
            return result + f"owes the club ${abs(display_amount):.2f}"
        # A $0 invoice is settled, not "owes $0.00".
        return result + "is settled up"

    @cached_property
    def invoice_summary(self):
        if self.auctiontos_user:
            return f"{self.auctiontos_user.name} {self.invoice_summary_short}"
        if self.buyer:
            return f"{self.buyer.get_full_name() or self.buyer.username} {self.invoice_summary_short}"
        return self.invoice_summary_short

    @property
    def label(self):
        return self.auction

    def __str__(self):
        if self.auctiontos_user:
            return f"{self.auctiontos_user.name}'s invoice for {self.auctiontos_user.auction}"
        if self.club and self.buyer:
            return f"{self.buyer.get_full_name() or self.buyer.username}'s membership invoice for {self.club.name}"
        return f"Invoice #{self.pk}"

    def get_absolute_url(self):
        return reverse("invoice_by_pk", kwargs={"pk": self.pk})

    @cached_property
    def is_online(self):
        """Based on the auction associated with this invoice"""
        if self.auctiontos_user:
            return self.auctiontos_user.auction.is_online
        if self.auction:
            return self.auction.is_online
        return False

    @cached_property
    def unsold_lot_warning(self):
        if self.unsold_non_donation_lots:
            return f"{self.unsold_non_donation_lots} unsold lot(s), sell these before setting this paid"
        return ""

    @cached_property
    def pre_register_used(self):
        return self.sold_lots_queryset.filter(pre_register_discount__gt=0).exists()

    @cached_property
    def total_payments(self):
        """Sum of payments recorded against this invoice (Decimal)."""
        total = self.payments.aggregate(total=Coalesce(Sum("amount"), Value(Decimal("0.00"))))["total"]
        if total is None:
            return Decimal("0.00")
        # Ensure Decimal return type
        return Decimal(total)

    def save(self, *args, **kwargs):
        previous_status = None
        if self.pk:
            previous_status = Invoice.objects.filter(pk=self.pk).values_list("status", flat=True).first()
        if not self.auction and self.auctiontos_user:
            self.auction = self.auctiontos_user.auction
        # Entering PAID snapshots the total and stamps the paid date once; afterwards nothing
        # re-derives them from live settings. ``self.pk`` guards the first insert, when related rows
        # can't be queried.
        entering_paid = self.status == "PAID" and previous_status != "PAID" and self.pk is not None
        newly_written_fields = []
        if entering_paid:
            self.calculated_total = self.rounded_net
            newly_written_fields.append("calculated_total")
        # First paid date only, so PAID -> UNPAID -> PAID books to one stable date.
        if self.status == "PAID" and self.date_paid is None:
            self.date_paid = timezone.now()
            newly_written_fields.append("date_paid")
        # Persist the new fields with a restricted update_fields.
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and newly_written_fields:
            update_fields = list(update_fields)
            kwargs["update_fields"] = update_fields + [f for f in newly_written_fields if f not in update_fields]
        super().save(*args, **kwargs)
        # One invoice per AuctionTOS: keep the oldest, move payments and adjustments in, delete this
        # one. Club-only invoices skip this but still reach the ledger sync below.
        if self.auctiontos_user:
            oldest = Invoice.objects.filter(auctiontos_user=self.auctiontos_user).order_by("date").first()
            if oldest and oldest.pk != self.pk:
                # Newer duplicate: migrate into the older invoice.
                duplicate_pk = self.pk
                InvoiceAdjustment.objects.filter(invoice=self).update(invoice=oldest)
                InvoicePayment.objects.filter(invoice=self).update(invoice=oldest)
                oldest._absorb_duplicate_ledger(self)
                Invoice.objects.filter(pk=duplicate_pk).delete()
                # Rebind to the canonical invoice.
                self.pk = oldest.pk
                self.id = oldest.pk
                self._state.adding = False
                self._state.db = oldest._state.db
                self.refresh_from_db()
                oldest.recalculate()
                return
            # self is the oldest — clean up any newer duplicates that may exist
            newer = Invoice.objects.filter(auctiontos_user=self.auctiontos_user).exclude(pk=self.pk)
            if newer.exists():
                for dup in newer:
                    InvoiceAdjustment.objects.filter(invoice=dup).update(invoice=self)
                    InvoicePayment.objects.filter(invoice=dup).update(invoice=self)
                    self._absorb_duplicate_ledger(dup)
                newer.delete()
                self.recalculate()
        # Sync the ledger only on a status transition; re-syncing a PAID invoice would rewrite booked
        # accounting from current settings.
        if previous_status != self.status or previous_status is None:
            self.sync_club_money()

    def _absorb_duplicate_ledger(self, duplicate):
        """Re-point a duplicate invoice's ClubMoney rows at this invoice and append an exact reversal, so rows
        aren't orphaned and nothing is double-booked. Mirrors the duplicate's rows rather than re-deriving,
        so a frozen canonical ledger isn't rewritten.
        """
        rows = list(ClubMoney.objects.filter(invoice=duplicate))
        if not rows:
            return
        reversals = [
            ClubMoney(
                club=row.club,
                invoice=self,
                source_auction=row.source_auction,
                date=row.date,
                amount=-row.amount,
                description=f"Duplicate invoice reversal: {row.description}"[: ClubMoney.DESCRIPTION_MAX_LENGTH],
                category=row.category,
            )
            for row in rows
        ]
        ClubMoney.objects.filter(invoice=duplicate).update(invoice=self)
        ClubMoney.objects.bulk_create(reversals)

    def _ledger_date(self):
        """The cash-basis date for this invoice's ledger entries: an existing entry's date, else the latest
        payment, else ``date_paid``, else today. Stable across re-syncs.
        """
        booked_date = (
            ClubMoney.objects.filter(invoice=self).order_by("date", "pk").values_list("date", flat=True).first()
        )
        if booked_date:
            return booked_date
        latest_payment = self.payments.order_by("-createdon", "-pk").values_list("createdon", flat=True).first()
        if latest_payment:
            return timezone.localtime(latest_payment).date()
        if self.date_paid:
            return timezone.localtime(self.date_paid).date()
        return timezone.localdate()

    def sync_club_money(self, acting_user=None):
        """Reconcile this invoice's ClubMoney entries with its state (cash basis, ``+`` into the club).

        A PAID invoice books sale, seller payout, tax, dues, adjustments, first-bid payout, member discount
        and rounding, summing to the rounded total. Commission is ``sales - payouts``, not stored. Unpaid
        invoices book nothing. Club-only dues invoices book their dues entry.

        Only the per-category delta is appended: self-correcting, reversible, append-only.
        """
        auction = self.auction or (self.auctiontos_user.auction if self.auctiontos_user else None)
        if auction and auction.club_id:
            club = auction.club
        else:
            # Club-only dues invoice: reconcile against its own club.
            auction = None
            club = self.club
        if not club:
            return []
        event_date = self._ledger_date()
        cents = Decimal("0.01")

        def _q(value):
            return Decimal(value or 0).quantize(cents)

        if self.auctiontos_user and self.auctiontos_user.name:
            who = self.auctiontos_user.name
        elif self.club_member:
            who = self.club_member.display_name
        elif self.buyer:
            who = self.buyer.get_full_name().strip() or self.buyer.username
        else:
            who = f"invoice #{self.pk}"
        where = f" in {auction}" if auction else ""

        # What the ledger should show now, per category.
        desired = {}
        descriptions = {}
        if self.status == "PAID" and auction:
            # Club-cash amounts summing to the rounded total; rounding is the remainder.
            sale = _q(self.total_bought)
            payout = -_q(self.total_sold)
            tax = _q(self.tax)
            membership = _q(self.membership_fee_amount)
            first_bid = -_q(self.first_bid_payout)
            member_discount = -_q(self.club_member_discount)
            # Registration fee is cash in.
            registration = _q(self.registration_fee_amount)
            # Same base as net (manual_adjustment_amount), so rounding doesn't absorb a mismatch.
            adjustment = -_q(self.manual_adjustment_amount)
            rounding = -_q(self.rounded_net) - (
                sale + payout + tax + membership + first_bid + member_discount + registration + adjustment
            )
            desired = {
                ClubMoney.CATEGORY_AUCTION_SALE: sale,
                ClubMoney.CATEGORY_AUCTION_SELLER_PAYOUT: payout,
                ClubMoney.CATEGORY_TAX: tax,
                ClubMoney.CATEGORY_MEMBERSHIP: membership,
                ClubMoney.CATEGORY_INVOICE_ADJUSTMENT: adjustment,
                ClubMoney.CATEGORY_FIRST_BID_PAYOUT: first_bid,
                ClubMoney.CATEGORY_CLUB_MEMBER_DISCOUNT: member_discount,
                ClubMoney.CATEGORY_REGISTRATION_FEE: registration,
                ClubMoney.CATEGORY_ROUNDING: rounding,
            }
            descriptions = {
                ClubMoney.CATEGORY_AUCTION_SALE: f"Payment from {who} for lots purchased in {auction}",
                ClubMoney.CATEGORY_AUCTION_SELLER_PAYOUT: f"Seller payout to {who} for lots sold in {auction}",
                ClubMoney.CATEGORY_TAX: f"Sales tax collected from {who} in {auction}",
                ClubMoney.CATEGORY_MEMBERSHIP: f"Membership dues from {who} in {auction}",
                ClubMoney.CATEGORY_INVOICE_ADJUSTMENT: f"Invoice adjustment for {who} in {auction}",
                ClubMoney.CATEGORY_FIRST_BID_PAYOUT: f"First-bid payout to {who} in {auction}",
                ClubMoney.CATEGORY_CLUB_MEMBER_DISCOUNT: f"Club member discount for {who} in {auction}",
                ClubMoney.CATEGORY_REGISTRATION_FEE: f"Registration fee from {who} in {auction}",
                ClubMoney.CATEGORY_ROUNDING: f"Invoice rounding for {who} in {auction}",
            }
        elif self.status == "PAID":
            # Club-only: only the renewal fee, booked and reversed by delta.
            membership = _q(self.membership_fee_amount)
            if membership:
                desired = {ClubMoney.CATEGORY_MEMBERSHIP: membership}
                descriptions = {ClubMoney.CATEGORY_MEMBERSHIP: f"Membership dues from {who}"}

        # What the ledger ALREADY shows for this invoice, per category.
        booked = {}
        for row in ClubMoney.objects.filter(invoice=self).values("category").annotate(total=Sum("amount")):
            booked[row["category"]] = row["total"] or Decimal("0.00")

        # Only current categories; legacy rows are left alone (migration 0305 rebuilds them).
        known_categories = {choice[0] for choice in ClubMoney.CATEGORY_CHOICES}
        default_description = f"Ledger correction for {who}{where}"
        entries = []
        for category in (set(desired) | set(booked)) & known_categories:
            delta = _q(desired.get(category, Decimal("0.00"))) - _q(booked.get(category, Decimal("0.00")))
            if not delta:
                continue
            entries.append(
                ClubMoney(
                    club=club,
                    invoice=self,
                    date=event_date,
                    amount=delta,
                    description=descriptions.get(category, default_description)[: ClubMoney.DESCRIPTION_MAX_LENGTH],
                    category=category,
                    created_by=acting_user,
                )
            )
        if entries:
            ClubMoney.objects.bulk_create(entries)
        return entries


class InvoiceAdjustment(InvalidatesRelatedCache, models.Model):
    """Alteration to a specific invoice"""

    # every number on the invoice is derived from these
    invalidates_cache_on = ("invoice",)

    invoice = models.ForeignKey("Invoice", null=True, blank=True, on_delete=models.CASCADE)
    user = models.ForeignKey(User, blank=True, null=True, on_delete=models.SET_NULL)
    user.help_text = "The auction admin who created this adjustment"
    createdon = models.DateTimeField(auto_now_add=True, blank=True)
    adjustment_type = models.CharField(
        max_length=20,
        choices=(
            ("ADD", "Charge extra"),
            ("DISCOUNT", "Discount"),
        ),
        default="ADD",
    )
    amount = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)])
    notes = models.CharField(max_length=150, default="")

    @property
    def formatted_float_value(self):
        return f"{self.amount:.2f}"

    @property
    def display(self):
        """for templates"""
        result = ""
        # Explicit sign: "+$10.00" added, "-$10.00" subtracted.
        if self.adjustment_type in ["ADD", "ADD_PERCENT"]:
            result += "+"
        if self.adjustment_type in ["DISCOUNT", "DISCOUNT_PERCENT"]:
            result += "-"
        if self.adjustment_type in ["ADD", "DISCOUNT"]:
            # Get currency symbol from the invoice
            currency_symbol = self.invoice.currency_symbol if self.invoice else "$"
            result += f"{currency_symbol}{self.formatted_float_value}"
        else:
            result += f"{self.amount}%"
        return result


class InvoicePayment(InvalidatesRelatedCache, models.Model):
    """A payment applied to an Invoice (partial payments allowed), separate from adjustments."""

    # total_payments, and everything downstream of it
    invalidates_cache_on = ("invoice",)

    PAYMENT_STATUS = (
        ("PENDING", "Pending"),
        ("COMPLETED", "Completed"),
        ("FAILED", "Failed"),
        ("REFUNDED", "Refunded"),
    )
    PAYMENT_TARGET_CHOICES = (
        ("INVOICE", "Invoice"),
        ("CLUB_MEMBER", "Club member"),
    )

    invoice = models.ForeignKey("Invoice", related_name="payments", on_delete=models.CASCADE, null=True, blank=True)
    club_member = models.ForeignKey(
        "ClubMember", related_name="payments", on_delete=models.CASCADE, null=True, blank=True
    )
    payment_target = models.CharField(max_length=20, choices=PAYMENT_TARGET_CHOICES, default="INVOICE")
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    amount_available_to_refund = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(max_length=10, default="USD")
    external_id = models.CharField(max_length=255, blank=True, null=True, help_text="Provider transaction id")
    receipt_number = models.CharField(
        max_length=10, blank=True, null=True, help_text="Short receipt number (4 chars for Square)", db_index=True
    )
    payer_name = models.CharField(max_length=200, blank=True, null=True)
    payer_email = models.CharField(max_length=200, blank=True, null=True)
    payer_address = models.CharField(max_length=500, blank=True, null=True)
    memo = models.CharField(max_length=500, blank=True, null=True)
    payment_method = models.CharField(
        max_length=50, blank=True, null=True, default="PayPal"
    )  # e.g. 'paypal', 'stripe', 'cash'
    createdon = models.DateTimeField(auto_now_add=True)


class TapToPayAttempt(models.Model):
    """One on-device Tap to Pay attempt, from create until the app reports how it ended.

    The SDK charges the card on the device, so a capture whose confirm never arrives would leave the
    invoice unpaid and open to a second charge. A stable per-invoice key once blocked that by accident,
    but broke declined-card retries (``payment_attempt_id_reused``). While an attempt is open, create
    refuses with a 409 saying to check Square. Attempts age out after ``OPEN_ATTEMPT_TIMEOUT``.
    """

    #: Outcomes. "" means still open -- the state the refusal is about.
    OUTCOME_CAPTURED = "captured"
    OUTCOME_CANCELED = "canceled"
    OUTCOME_FAILED = "failed"
    OUTCOME_EXPIRED = "expired"
    OUTCOME_CHOICES = (
        (OUTCOME_CAPTURED, "Captured"),
        (OUTCOME_CANCELED, "Canceled"),
        (OUTCOME_FAILED, "Failed"),
        (OUTCOME_EXPIRED, "Expired"),
    )

    # Square's 45-character cap; unique so two devices can't share an attempt.
    attempt_id = models.CharField(max_length=45, unique=True)
    invoice = models.ForeignKey("Invoice", related_name="tap_to_pay_attempts", on_delete=models.CASCADE)
    # Kept if the account goes: evidence about money.
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    createdon = models.DateTimeField(auto_now_add=True, db_index=True)
    closed_at = models.DateTimeField(blank=True, null=True)
    outcome = models.CharField(max_length=20, choices=OUTCOME_CHOICES, blank=True, default="")
    # Square's payment id once confirm verifies it.
    payment_id = models.CharField(max_length=255, blank=True, default="")

    def __str__(self):
        return f"Tap to Pay attempt {self.attempt_id} ({self.outcome or 'open'})"


class ClubMoney(models.Model):
    DESCRIPTION_MAX_LENGTH = 500

    # Booked from invoices by Invoice.sync_club_money.
    CATEGORY_AUCTION_SALE = "auction_sale"
    CATEGORY_AUCTION_SELLER_PAYOUT = "auction_seller_payout"
    CATEGORY_TAX = "tax"
    CATEGORY_MEMBERSHIP = "membership"
    CATEGORY_INVOICE_ADJUSTMENT = "invoice_adjustment"
    CATEGORY_FIRST_BID_PAYOUT = "first_bid_payout"
    CATEGORY_CLUB_MEMBER_DISCOUNT = "club_member_discount"
    CATEGORY_REGISTRATION_FEE = "registration_fee"
    CATEGORY_ROUNDING = "rounding"
    # Entered by hand by a treasurer.
    CATEGORY_DONATION = "donation"
    CATEGORY_SPEAKER_COSTS = "speaker_costs"
    CATEGORY_MEETING_LOCATION_COST = "meeting_location_cost"
    CATEGORY_REFUNDS = "refunds"
    CATEGORY_ADJUSTMENT = "adjustment"

    # Reconciled from invoices, so not enterable by hand.
    AUTO_CATEGORIES = (
        CATEGORY_AUCTION_SALE,
        CATEGORY_AUCTION_SELLER_PAYOUT,
        CATEGORY_TAX,
        CATEGORY_INVOICE_ADJUSTMENT,
        CATEGORY_FIRST_BID_PAYOUT,
        CATEGORY_CLUB_MEMBER_DISCOUNT,
        CATEGORY_ROUNDING,
    )

    CATEGORY_CHOICES = (
        (CATEGORY_AUCTION_SALE, "Auction sale (buyer payment)"),
        (CATEGORY_AUCTION_SELLER_PAYOUT, "Seller payout"),
        (CATEGORY_TAX, "Sales tax collected"),
        (CATEGORY_MEMBERSHIP, "Membership dues"),
        (CATEGORY_INVOICE_ADJUSTMENT, "Invoice adjustment"),
        (CATEGORY_FIRST_BID_PAYOUT, "First-bid payout"),
        (CATEGORY_CLUB_MEMBER_DISCOUNT, "Club member discount"),
        (CATEGORY_REGISTRATION_FEE, "Registration fee"),
        (CATEGORY_ROUNDING, "Invoice rounding"),
        (CATEGORY_DONATION, "Donation"),
        (CATEGORY_SPEAKER_COSTS, "Speaker costs"),
        (CATEGORY_MEETING_LOCATION_COST, "Meeting location cost"),
        (CATEGORY_REFUNDS, "Refund"),
        (CATEGORY_ADJUSTMENT, "Balance adjustment"),
    )

    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="money")
    invoice = models.ForeignKey("Invoice", null=True, blank=True, on_delete=models.SET_NULL, related_name="club_money")
    source_auction = models.ForeignKey(
        "Auction", null=True, blank=True, on_delete=models.SET_NULL, related_name="club_money"
    )
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    date = models.DateField(db_index=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    description = models.CharField(max_length=DESCRIPTION_MAX_LENGTH, blank=True, default="")
    category = models.CharField(max_length=40, choices=CATEGORY_CHOICES)
    createdon = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-pk"]
        verbose_name_plural = "Club money"

    def __str__(self):
        return f"{self.club}: {self.date} {self.amount} {self.get_category_display()}"


class Bid(InvalidatesRelatedCache, models.Model):
    """Bids apply to lots"""

    # Saving a Bid must drop lot.bids.
    invalidates_cache_on = ("lot_number",)

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    lot_number = models.ForeignKey(Lot, on_delete=models.CASCADE)
    bid_time = models.DateTimeField(auto_now_add=True, blank=True)
    last_bid_time = models.DateTimeField(auto_now_add=True, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    was_high_bid = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)
    # Bids come from Users only; AuctionTOS can win without bidding.

    def __str__(self):
        return str(self.user) + " bid " + str(self.amount) + " on lot " + str(self.lot_number)

    def delete(self, *args, **kwargs):
        self.is_deleted = True
        self.save()


class Watch(InvalidatesRelatedCache, models.Model):
    """Users can watch lots: listed on their page, with an email before the end."""

    # Lot.number_of_watchers
    invalidates_cache_on = ("lot_number",)

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    lot_number = models.ForeignKey(Lot, on_delete=models.CASCADE)
    createdon = models.DateTimeField(auto_now_add=True, blank=True)

    def __str__(self):
        return str(self.user) + " watching " + str(self.lot_number)

    class Meta:
        verbose_name_plural = "Users watching"


class UserBan(models.Model):
    """A user's ban on another user bidding on their lots and in any auction they administer
    (Auction.user_banned_by_admins).
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    banned_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="banned_user")
    createdon = models.DateTimeField(auto_now_add=True, blank=True)

    def __str__(self):
        return str(self.user) + " has banned " + str(self.banned_user)


class UserIgnoreCategory(models.Model):
    """A category a user hides from all lot views."""

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    category = models.ForeignKey(Category, on_delete=models.CASCADE)
    createdon = models.DateTimeField(auto_now_add=True, blank=True)

    def __str__(self):
        return str(self.user) + " hates " + str(self.category)


class PageView(CachedPropertiesMixin, models.Model):
    """One row per page opened.

    **Repeat views are history, not duplicates.** ``remove_duplicate_views`` merged them and was removed:
    it only reached anonymous rows and had no time window. Nothing purges this table; its readers carry
    a window and an owner.

    Inert columns: ``total_time`` and ``counter`` (disabled heartbeat), ``notification_sent`` (no
    writer), ``duplicate_check_completed`` (the removed job).
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    auction = models.ForeignKey(Auction, null=True, blank=True, on_delete=models.CASCADE)
    auction.help_text = "Set when a visitor views the auction's rules page, its lot list or one of its lots, and deliberately left empty on organizer-facing pages so that view counts stay visitor counts. Rows written before 2026-09-09 have it on the rules page only."
    lot_number = models.ForeignKey(Lot, null=True, blank=True, on_delete=models.CASCADE)
    lot_number.help_text = "Only filled out when a user views a specific lot's page"
    date_start = models.DateTimeField(auto_now_add=True, db_index=True)
    date_end = models.DateTimeField(null=True, blank=True, default=timezone.now, db_index=True)
    total_time = models.PositiveIntegerField(default=0)
    total_time.help_text = "The total time in seconds the user has spent on the lot page"
    source = models.CharField(max_length=200, blank=True, null=True, default="", db_index=True)
    counter = models.PositiveIntegerField(default=0)
    url = models.CharField(max_length=600, blank=True, null=True)
    title = models.CharField(max_length=600, blank=True, null=True)
    referrer = models.CharField(max_length=600, blank=True, null=True)
    session_id = models.CharField(max_length=600, blank=True, null=True, db_index=True)
    notification_sent = models.BooleanField(default=False)
    duplicate_check_completed = models.BooleanField(default=False)
    latitude = models.FloatField(default=0, db_index=True)
    longitude = models.FloatField(default=0, db_index=True)
    ip_address = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    user_agent = models.CharField(max_length=200, blank=True, null=True)
    platform = models.CharField(max_length=200, default="", blank=True, null=True)
    os = models.CharField(
        max_length=20,
        choices=(
            ("UNKNOWN", "Unknown"),
            ("ANDROID", "Android"),
            ("IPHONE", "iPhone"),
            ("WINDOWS", "Windows"),
            ("OSX", "OS X"),
        ),
        default="UNKNOWN",
    )

    def __str__(self):
        thing = self.url
        # thing = self.title
        return f"User {self.user} viewed {thing} for {self.total_time} seconds"

    def save(self, *args, **kwargs):
        if not self.latitude and self.ip_address:
            # values_list: two floats, not a hydrated row. The (ip_address, -date_start) index keeps it cheap.
            nearby = (
                PageView.objects.exclude(latitude=0, longitude=0)
                .filter(ip_address=self.ip_address)
                .order_by("-date_start")
                .values_list("latitude", "longitude")
                .first()
            )
            if nearby:
                self.latitude, self.longitude = nearby
            elif self.user and self.user.userdata.latitude:
                self.latitude = self.user.userdata.latitude
                self.longitude = self.user.userdata.longitude
        super().save(*args, **kwargs)

    class Meta:
        indexes = [
            # For the save() lookup above, without a filesort.
            models.Index(fields=["ip_address", "-date_start"], name="pageview_ip_recent_idx"),
            # Each lot list asks for the user's latest view.
            models.Index(fields=["user", "-date_start"], name="pageview_user_recent_idx"),
        ]


class ChunkedJobState(models.Model):
    """Where a chunked job got to, so it resumes. A table, not a cache key: finding the position otherwise
    means scanning PageView, and a flushed cache would restart a days-long walk. Delete with the job.
    """

    name = models.CharField(max_length=100, primary_key=True)
    cursor = models.BigIntegerField(default=0)
    cursor.help_text = "The next primary key to look at. Everything below this has been handled."
    ceiling = models.BigIntegerField(default=0)
    ceiling.help_text = (
        "The primary key the job stops at, captured on its first run. Rows written after that were "
        "written by code that already does the right thing, so chasing them would never finish."
    )
    finished = models.DateTimeField(null=True, blank=True)
    updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        state = "finished" if self.finished else f"at {self.cursor} of {self.ceiling}"
        return f"{self.name} ({state})"


class UserLabelPrefs(models.Model):
    """Dimensions used for the label PDF"""

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    empty_labels = models.IntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])
    empty_labels.help_text = "To print on partially used label sheets, print this many blank labels before printing the actual labels.  Just remember to set this back to 0 when starting a new sheet of labels!"
    print_border = models.BooleanField(default=True)
    print_border.help_text = (
        "Uncheck if you plant to use peel and stick labels.  Has no effect if you select thermal labels."
    )
    page_width = models.FloatField(default=8.5, validators=[MinValueValidator(1), MaxValueValidator(100.0)])
    page_height = models.FloatField(default=11, validators=[MinValueValidator(1), MaxValueValidator(100.0)])
    label_width = models.FloatField(default=2.51, validators=[MinValueValidator(1), MaxValueValidator(100.0)])
    label_height = models.FloatField(default=0.98, validators=[MinValueValidator(0.4), MaxValueValidator(50.0)])
    label_margin_right = models.FloatField(default=0.2, validators=[MinValueValidator(0.0), MaxValueValidator(5.0)])
    label_margin_bottom = models.FloatField(default=0.02, validators=[MinValueValidator(0.0), MaxValueValidator(5.0)])
    page_margin_top = models.FloatField(default=0.55, validators=[MinValueValidator(0.0)])
    page_margin_bottom = models.FloatField(default=0.45, validators=[MinValueValidator(0.0)])
    page_margin_left = models.FloatField(default=0.18, validators=[MinValueValidator(0.0)])
    page_margin_right = models.FloatField(default=0.18, validators=[MinValueValidator(0.0)])
    font_size = models.FloatField(default=8, validators=[MinValueValidator(5), MaxValueValidator(14)])
    UNITS = (
        ("in", "Inches"),
        ("cm", "Centimeters"),
    )
    unit = models.CharField(max_length=20, choices=UNITS, blank=False, null=False, default="in")
    PRESETS = (
        ("sm", "Small (Avery 5160) (Not recommended)"),
        ("lg", "Large (Avery 18262)"),
        ("thermal_sm", 'Thermal 3"x2"'),
        ("thermal_very_sm", 'Thermal 1⅛" x 3½" (Dymo 30252)'),
        ("custom", "Custom"),
    )
    preset = models.CharField(
        max_length=20,
        choices=PRESETS,
        blank=False,
        null=False,
        default="lg",
        verbose_name="Label size",
    )
    PRINT_METHODS = (
        ("pdf", "PDF download"),
        ("system", "System printer"),
        ("bluetooth", "Bluetooth label printer"),
    )
    print_method = models.CharField(max_length=20, choices=PRINT_METHODS, default="pdf")
    print_method.help_text = (
        "PDF downloads a file to print later. System printer sends the PDF straight "
        "to a printer configured on your phone. Bluetooth prints directly to a "
        "thermal label printer. System printer and Bluetooth only work in the app."
    )
    # Printing from a desktop to the phone's Bluetooth printer, separate from print_method (it's about
    # the other device). Only offered with a phone that has reported a paired printer.
    print_from_computer = models.BooleanField(default=False, verbose_name="Print from my computer to my phone")
    print_from_computer.help_text = (
        "When you print labels on a computer, send them to the printer paired with your phone "
        "instead of making a PDF. <b>The app has to be open on your phone.</b>"
    )


def get_default_can_create_auctions():
    return settings.ALLOW_USERS_TO_CREATE_AUCTIONS
    # return getattr(settings, "ALLOW_USERS_TO_CREATE_AUCTIONS", True)


def get_default_can_submit_lots():
    return settings.ALLOW_USERS_TO_CREATE_LOTS
    # return getattr(settings, "ALLOW_USERS_TO_CREATE_LOTS", True)


def get_default_paypal_enabled():
    return settings.PAYPAL_ENABLED_FOR_USERS


def get_default_use_llm_search():
    """Whether new users get the assistant (ASSISTANT_ENABLED_FOR_USERS)."""
    return getattr(settings, "ASSISTANT_ENABLED_FOR_USERS", True)


def get_default_square_enabled():
    return getattr(settings, "SQUARE_ENABLED_FOR_USERS", False)


def get_default_is_trusted():
    return settings.USERS_ARE_TRUSTED_BY_DEFAULT


class UserData(CachedPropertiesMixin, models.Model):
    """Additional per-user data."""

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    address = models.CharField(max_length=500, blank=True, null=True)
    address.help_text = (
        "Your complete mailing address.  If you sell lots in an auction, your check will be mailed here."
    )
    location = models.ForeignKey(Location, blank=True, null=True, on_delete=models.SET_NULL)
    club = models.ForeignKey(Club, blank=True, null=True, on_delete=models.SET_NULL)
    use_dark_theme = models.BooleanField(default=True)
    use_dark_theme.help_text = "Uncheck to use the blindingly bright light theme"
    use_list_view = models.BooleanField(default=False)
    use_list_view.help_text = "Show a list of all lots instead of showing pictures"
    email_visible = models.BooleanField(default=False)
    email_visible.help_text = "Show your email address on your user page.  This will be visible only to logged in users.  <a href='/blog/privacy/' target='_blank'>Privacy information</a>"
    last_auction_used = models.ForeignKey(Auction, blank=True, null=True, on_delete=models.SET_NULL)
    last_club_used = models.ForeignKey(
        Club,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="The most recent club whose page this user viewed while a member. Used to scope command palette shortcuts.",
    )
    last_activity = models.DateTimeField(auto_now_add=True)
    latitude = models.FloatField(default=0)
    longitude = models.FloatField(default=0)
    location_coordinates = PlainLocationField(
        based_fields=["address"], zoom=11, blank=True, null=True, verbose_name="Map"
    )
    location_coordinates.help_text = (
        "Make sure your map marker is correctly placed - you will get notifications about nearby auctions"
    )
    last_ip_address = models.CharField(max_length=100, blank=True, null=True)
    email_me_when_people_comment_on_my_lots = models.BooleanField(default=True, blank=True)
    email_me_when_people_comment_on_my_lots.help_text = "Notifications will be sent once a day, only for messages you haven't seen.  If you'd like to get a notification right away, <a href='https://github.com/iragm/fishauctions/issues/224'>leave a comment here</a>"
    email_me_about_new_auctions = models.BooleanField(
        default=True, blank=True, verbose_name="Email me about new online auctions"
    )
    email_me_about_new_auctions.help_text = (
        "When new online auctions are created with pickup locations near my location, notify me"
    )
    email_me_about_new_auctions_distance = models.PositiveIntegerField(
        null=True, blank=True, default=100, verbose_name="Nearby online auction distance"
    )
    email_me_about_new_auctions_distance.help_text = (
        "miles, from your address. Also used to filter the auction list when your location is set."
    )
    email_me_about_new_in_person_auctions = models.BooleanField(default=True, blank=True)
    email_me_about_new_in_person_auctions.help_text = (
        "When new in-person auctions are created near my location, notify me"
    )
    email_me_about_new_in_person_auctions_distance = models.PositiveIntegerField(
        null=True,
        blank=True,
        default=100,
        verbose_name="Nearby in-person auction distance",
    )
    email_me_about_new_in_person_auctions_distance.help_text = (
        "miles, from your address. Also used to filter the auction list when your location is set."
    )
    show_nearby_auctions = models.BooleanField(default=True, blank=True, verbose_name="Only show nearby auctions")
    show_nearby_auctions.help_text = (
        "When your location is set, only show auctions near you on the auction list. "
        "Auctions you've joined or created are always shown."
    )
    email_me_about_new_local_lots = models.BooleanField(default=True, blank=True)
    email_me_about_new_local_lots.help_text = (
        "When new nearby lots (that aren't part of an auction) are created, notify me"
    )
    local_distance = models.PositiveIntegerField(
        null=True, blank=True, default=60, verbose_name="New local lot distance"
    )
    local_distance.help_text = "miles, from your address"
    email_me_about_new_lots_ship_to_location = models.BooleanField(
        default=True, blank=True, verbose_name="Email me about lots that can be shipped"
    )
    email_me_about_new_lots_ship_to_location.help_text = (
        "Email me when new lots are created that can be shipped to my location"
    )
    push_notifications_instead_of_email = models.BooleanField(default=False, blank=True)
    push_notifications_instead_of_email.help_text = (
        "Get notifications in the app instead of emails, for everything "
        "except account emails like password resets. Requires the app to be installed "
        "and signed in. The weekly promo email is replaced by a notification for "
        "promoted auctions near you."
    )
    paypal_email_address = models.CharField(max_length=200, blank=True, null=True, verbose_name="PayPal Address")
    paypal_email_address.help_text = "If different from your email address"
    unsubscribe_link = models.CharField(max_length=255, default=uuid_module.uuid4, blank=True)
    has_unsubscribed = models.BooleanField(default=False, blank=True)
    account_deletion_requested = models.DateTimeField(null=True, blank=True)
    account_deletion_requested.help_text = (
        "When the user asked us to delete their account.  The account keeps working until the grace "
        "period is up (see auctions.account_deletion); signing in again cancels the request."
    )
    banned_from_chat_until = models.DateTimeField(null=True, blank=True)
    banned_from_chat_until.help_text = (
        "After this date, the user can post chats again.  Being banned from chatting does not block bidding"
    )
    can_submit_standalone_lots = models.BooleanField(default=get_default_can_submit_lots)
    can_create_club_auctions = models.BooleanField(default=get_default_can_create_auctions)
    paypal_enabled = models.BooleanField(default=get_default_paypal_enabled)
    square_enabled = models.BooleanField(default=get_default_square_enabled)
    is_trusted = models.BooleanField(default=get_default_is_trusted)
    is_trusted.help_text = "Trusted users can promote auctions, accept payments, and send invoice notification emails"
    dismissed_cookies_tos = models.BooleanField(default=False)
    show_ad_controls = models.BooleanField(default=False, blank=True)
    show_ad_controls.help_text = "Show a tab for ads on all pages"
    use_llm_search = models.BooleanField(
        default=get_default_use_llm_search, blank=True, verbose_name="AI command palette"
    )
    use_llm_search.help_text = (
        "Let this user talk to the site in plain English by typing or speaking into the command "
        "palette.  On for everyone by default; uncheck it to take the palette away from this one "
        "user (they abused it, say), or turn it off site-wide with `manage.py change_assistant "
        "off`.  Also needs an LLM configured site-wide (auctions.llm.assist_enabled), because "
        "every command spends this site's own budget.  This does NOT gate connecting Claude or "
        "another assistant over MCP (/ai/), which is open to everyone: an agent brings its own "
        "model and costs this site nothing."
    )
    credit = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    credit.help_text = "The total balance in your account"
    show_ads = models.BooleanField(default=True, blank=True)
    show_ads.help_text = "Ads have been disabled site-wide indefinitely, so this option doesn't do anything right now."
    preferred_bidder_number = models.CharField(max_length=4, default="", blank=True)
    timezone = models.CharField(max_length=100, null=True, blank=True)
    username_visible = models.BooleanField(default=True, blank=True)
    username_visible.help_text = "Uncheck to bid anonymously.  Your username will still be visible on lots you sell, chat messages, and to the people running any auctions you've joined."
    show_email_warning_sent = models.BooleanField(default=False, blank=True)
    show_email_warning_sent.help_text = "When a user has their email address hidden and sells a lot, this is checked"
    send_reminder_emails_about_joining_auctions = models.BooleanField(default=True, blank=True)
    send_reminder_emails_about_joining_auctions.help_text = (
        "Get an annoying reminder email when you view an auction but don't join it"
    )
    email_me_about_new_chat_replies = models.BooleanField(default=True, blank=True)
    email_me_about_new_chat_replies.help_text = (
        "When you comment on lots you don't own, send any new messages about that lot to your email"
    )
    share_lot_images = models.BooleanField("Allow my lot images to be used on other lots", default=True, blank=True)
    share_lot_images.help_text = "Images will be added to other lots without an image that have the same name"
    auto_add_images = models.BooleanField("Automatically add images to my lots", default=True, blank=True)
    auto_add_images.help_text = "If another lot with the same name has been added previously.  Images are only added to lots that are part of an auction."
    push_notifications_when_lots_sell = models.BooleanField(default=False, blank=True)
    push_notifications_when_lots_sell.help_text = "For in-person auctions, get a notification when bidding starts on a lot that you've watched<span class='d-none' id='subscribe_message_area'></span>"
    show_running_total_notification = models.BooleanField(
        default=True, blank=True, verbose_name="Show running total notification"
    )
    show_running_total_notification.help_text = (
        "Show your total amount purchased in real time in the app.  In person auctions only."
    )
    # The running-total tip is sent once, not after every lot.
    running_total_tip_sent = models.BooleanField(default=False)
    running_total_tip_sent.help_text = (
        "Whether this user has been told, once, where to turn the running total notification off"
    )
    distance_unit = models.CharField(
        max_length=10,
        choices=[("mi", "Miles"), ("km", "Kilometers")],
        default="mi",
        verbose_name="Distance unit",
    )
    distance_unit.help_text = "Unit for displaying distances"
    preferred_currency = models.CharField(
        max_length=10,
        choices=[
            ("USD", "US Dollar ($)"),
            ("CAD", "Canadian Dollar ($)"),
            ("GBP", "British Pound (£)"),
            ("EUR", "Euro (€)"),
            ("JPY", "Japanese Yen (¥)"),
            ("AUD", "Australian Dollar ($)"),
            ("CHF", "Swiss Franc (CHF)"),
            ("CNY", "Chinese Yuan (¥)"),
        ],
        default="USD",
        verbose_name="Preferred currency",
    )
    preferred_currency.help_text = "This currency will be used in any auctions you create"

    # breederboard info
    rank_unique_species = models.PositiveIntegerField(null=True, blank=True)
    number_unique_species = models.PositiveIntegerField(null=True, blank=True)
    rank_total_lots = models.PositiveIntegerField(null=True, blank=True)
    number_total_lots = models.PositiveIntegerField(null=True, blank=True)
    rank_total_spent = models.PositiveIntegerField(null=True, blank=True)
    number_total_spent = models.PositiveIntegerField(null=True, blank=True)
    rank_total_bids = models.PositiveIntegerField(null=True, blank=True)
    number_total_bids = models.PositiveIntegerField(null=True, blank=True)
    number_total_sold = models.PositiveIntegerField(null=True, blank=True)
    rank_total_sold = models.PositiveIntegerField(null=True, blank=True)
    total_volume = models.PositiveIntegerField(null=True, blank=True)
    rank_volume = models.PositiveIntegerField(null=True, blank=True)
    seller_percentile = models.PositiveIntegerField(null=True, blank=True)
    buyer_percentile = models.PositiveIntegerField(null=True, blank=True)
    volume_percentile = models.PositiveIntegerField(null=True, blank=True)
    has_bid = models.BooleanField(default=False)
    has_used_proxy_bidding = models.BooleanField(default=False)
    never_show_paypal_connect = models.BooleanField(default=False)
    never_show_square_connect = models.BooleanField(default=False)
    next_promo_email_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_promo_email_sent_at = models.DateTimeField(null=True, blank=True)

    @property
    def account_deletion_due(self):
        """When a pending deletion runs, or None."""
        from auctions.account_deletion import deletion_due_date

        return deletion_due_date(self)

    @cached_property
    def last_auction_created(self):
        return Auction.objects.filter(created_by=self.user).order_by("-date_posted").first()

    @cached_property
    def available_auctions_to_submit_lots(self):
        """Returns auctions that this user can submit lots to"""
        from django.utils import timezone

        return (
            Auction.objects.exclude(is_deleted=True)
            .filter(lot_submission_end_date__gte=timezone.now())
            .filter(lot_submission_start_date__lte=timezone.now())
            .filter(auctiontos__user=self.user, auctiontos__selling_allowed=True)
            .order_by("date_end")
        )

    def __str__(self):
        return f"{self.user.username}'s data"

    def merge_into(self, user_to_merge_to):
        if not user_to_merge_to or not getattr(user_to_merge_to, "pk", None):
            msg = "A saved user is required as the merge target."
            raise ValueError(msg)
        if user_to_merge_to == self.user:
            msg = "Cannot merge a user into itself."
            raise ValueError(msg)

        source_user = self.user
        target_userdata, _ = UserData.objects.get_or_create(user=user_to_merge_to)

        def merge_unique_relation(model, key_field, merge=None):
            existing = {getattr(item, key_field): item for item in model.objects.filter(user=user_to_merge_to)}
            for item in list(model.objects.filter(user=source_user)):
                key = getattr(item, key_field)
                target_item = existing.get(key)
                if target_item:
                    if merge:
                        merge(target_item, item)
                    item.delete()
                    continue
                item.user = user_to_merge_to
                item.save(update_fields=["user"])
                existing[key] = item

        def merge_chat_subscription(target_item, source_item):
            update_fields = set()
            if source_item.last_seen and (not target_item.last_seen or source_item.last_seen > target_item.last_seen):
                target_item.last_seen = source_item.last_seen
                update_fields.add("last_seen")
            if source_item.last_notification_sent and (
                not target_item.last_notification_sent
                or source_item.last_notification_sent > target_item.last_notification_sent
            ):
                target_item.last_notification_sent = source_item.last_notification_sent
                update_fields.add("last_notification_sent")
            if source_item.unsubscribed and not target_item.unsubscribed:
                target_item.unsubscribed = True
                update_fields.add("unsubscribed")
            if update_fields:
                target_item.save(update_fields=list(update_fields))

        with transaction.atomic():
            target_updates = set()
            fields_to_copy_if_missing = [
                "phone_number",
                "address",
                "location",
                "club",
                "last_auction_used",
                "last_club_used",
                "location_coordinates",
                "paypal_email_address",
                "preferred_bidder_number",
                "timezone",
            ]
            for field in fields_to_copy_if_missing:
                source_value = getattr(self, field, None)
                target_value = getattr(target_userdata, field, None)
                if target_value in (None, "") and source_value not in (None, ""):
                    setattr(target_userdata, field, source_value)
                    target_updates.add(field)
            if not target_userdata.latitude and self.latitude:
                target_userdata.latitude = self.latitude
                target_updates.add("latitude")
            if not target_userdata.longitude and self.longitude:
                target_userdata.longitude = self.longitude
                target_updates.add("longitude")
            if self.credit:
                target_userdata.credit = (target_userdata.credit or 0) + self.credit
                target_updates.add("credit")
            for field in [
                "can_submit_standalone_lots",
                "can_create_club_auctions",
                "paypal_enabled",
                "square_enabled",
                "is_trusted",
            ]:
                if getattr(self, field) and not getattr(target_userdata, field):
                    setattr(target_userdata, field, True)
                    target_updates.add(field)
            if target_updates:
                target_userdata.save(update_fields=list(target_updates))

            Auction.objects.filter(created_by=source_user).update(created_by=user_to_merge_to)
            PickupLocation.objects.filter(user=source_user).update(user=user_to_merge_to)
            Invoice.objects.filter(buyer=source_user).update(buyer=user_to_merge_to)
            Lot.objects.filter(user=source_user).update(user=user_to_merge_to)
            Lot.objects.filter(winner=source_user).update(winner=user_to_merge_to)
            Bid.objects.filter(user=source_user).update(user=user_to_merge_to)
            PageView.objects.filter(user=source_user).update(user=user_to_merge_to)
            AuctionCampaign.objects.filter(user=source_user).update(user=user_to_merge_to)
            SearchHistory.objects.filter(user=source_user).update(user=user_to_merge_to)

            merge_unique_relation(AuctionIgnore, "auction_id")
            merge_unique_relation(UserIgnoreCategory, "category_id")
            merge_unique_relation(Watch, "lot_number_id")
            merge_unique_relation(ChatSubscription, "lot_id", merge=merge_chat_subscription)
            updated_interest_ids = set()
            existing_interests = {
                item.category_id: item for item in UserInterestCategory.objects.filter(user=user_to_merge_to)
            }
            for interest in list(UserInterestCategory.objects.filter(user=source_user)):
                target_interest = existing_interests.get(interest.category_id)
                if target_interest:
                    if interest.interest > target_interest.interest:
                        target_interest.interest = interest.interest
                        target_interest.save(update_fields=["interest"])
                    updated_interest_ids.add(target_interest.pk)
                    interest.delete()
                    continue
                interest.user = user_to_merge_to
                interest.save(update_fields=["user"])
                existing_interests[interest.category_id] = interest
                updated_interest_ids.add(interest.pk)
            for interest in UserInterestCategory.objects.filter(pk__in=updated_interest_ids):
                interest.save()

            for source_tos in list(AuctionTOS.objects.filter(user=source_user).select_related("auction")):
                target_tos = (
                    AuctionTOS.objects.filter(user=user_to_merge_to, auction=source_tos.auction)
                    .exclude(pk=source_tos.pk)
                    .order_by("createdon")
                    .first()
                )
                if target_tos:
                    target_tos.merge_duplicate(
                        source_tos,
                        reason=f"merged from user account {source_user.username}",
                    )
                else:
                    source_tos.user = user_to_merge_to
                    source_tos.save()

            for source_member in list(ClubMember.objects.filter(user=source_user).select_related("club")):
                target_member = (
                    ClubMember.objects.filter(club=source_member.club, user=user_to_merge_to)
                    .exclude(pk=source_member.pk)
                    .order_by("pk")
                    .first()
                )
                if not target_member:
                    source_member.user = user_to_merge_to
                    source_member.save(update_fields=["user"])
                    continue

                member_updates = set()
                for field in [
                    "name",
                    "email",
                    "phone_number",
                    "address",
                    "discord_id",
                    "discord_username",
                    "discord_roles",
                    "membership_last_paid",
                    "membership_expiration_date",
                    "membership_expiration_reminder_due",
                    "discord_role_override",
                    "last_discord_role_assigned",
                    "bidder_number",
                ]:
                    source_value = getattr(source_member, field, None)
                    target_value = getattr(target_member, field, None)
                    if target_value in (None, "") and source_value not in (None, ""):
                        setattr(target_member, field, source_value)
                        member_updates.add(field)
                for field in [
                    "permission_admin",
                    "permission_view",
                    "permission_export",
                    "permission_add_edit",
                    "permission_edit_club",
                    "permission_money",
                    "permission_manage_auctions",
                    "permission_manage_bap",
                    "permission_manage_donations",
                    "permission_send_announcements",
                ]:
                    if getattr(source_member, field) and not getattr(target_member, field):
                        setattr(target_member, field, True)
                        member_updates.add(field)
                if source_member.bap_points and not target_member.bap_points:
                    target_member.bap_points = source_member.bap_points
                    member_updates.add("bap_points")
                if source_member.hap_points and not target_member.hap_points:
                    target_member.hap_points = source_member.hap_points
                    member_updates.add("hap_points")
                if source_member.culture_points and not target_member.culture_points:
                    target_member.culture_points = source_member.culture_points
                    member_updates.add("culture_points")
                if source_member.bap_points_ytd and not target_member.bap_points_ytd:
                    target_member.bap_points_ytd = source_member.bap_points_ytd
                    member_updates.add("bap_points_ytd")
                if source_member.hap_points_ytd and not target_member.hap_points_ytd:
                    target_member.hap_points_ytd = source_member.hap_points_ytd
                    member_updates.add("hap_points_ytd")
                if source_member.culture_points_ytd and not target_member.culture_points_ytd:
                    target_member.culture_points_ytd = source_member.culture_points_ytd
                    member_updates.add("culture_points_ytd")
                if not source_member.is_deleted and target_member.is_deleted:
                    target_member.is_deleted = False
                    member_updates.add("is_deleted")
                if member_updates:
                    target_member.save(update_fields=list(member_updates))

                BapAward.objects.filter(club_member=source_member).update(club_member=target_member)
                InvoicePayment.objects.filter(club_member=source_member).update(club_member=target_member)
                AuctionTOS.objects.filter(clubmember=source_member).update(clubmember=target_member)
                if target_member.bap_awards.exists():
                    BapAward.recalculate_member_points(target_member)
                source_member.user = None
                source_member.is_deleted = True
                source_member.save(update_fields=["user", "is_deleted"])

            for model, field_names in [
                (
                    PayPalSeller,
                    ["paypal_merchant_id", "currency", "payer_email"],
                ),
                (
                    SquareSeller,
                    [
                        "square_merchant_id",
                        "access_token",
                        "refresh_token",
                        "token_expires_at",
                        "currency",
                        "payer_email",
                    ],
                ),
            ]:
                source_record = model.objects.filter(user=source_user).first()
                if not source_record:
                    continue
                target_record = model.objects.filter(user=user_to_merge_to).first()
                if not target_record:
                    source_record.user = user_to_merge_to
                    source_record.save(update_fields=["user"])
                    continue
                payment_updates = set()
                for field in field_names:
                    source_value = getattr(source_record, field, None)
                    target_value = getattr(target_record, field, None)
                    if target_value in (None, "") and source_value not in (None, ""):
                        setattr(target_record, field, source_value)
                        payment_updates.add(field)
                if payment_updates:
                    target_record.save(update_fields=list(payment_updates))
                source_record.delete()

            self.phone_number = None
            self.address = None
            self.location = None
            self.club = None
            self.last_auction_used = None
            self.last_club_used = None
            self.latitude = 0
            self.longitude = 0
            self.location_coordinates = None
            self.paypal_email_address = None
            self.credit = 0
            self.preferred_bidder_number = ""
            self.can_submit_standalone_lots = get_default_can_submit_lots()
            self.can_create_club_auctions = get_default_can_create_auctions()
            self.paypal_enabled = get_default_paypal_enabled()
            self.square_enabled = get_default_square_enabled()
            self.is_trusted = get_default_is_trusted()
            self.save(
                update_fields=[
                    "phone_number",
                    "address",
                    "location",
                    "club",
                    "last_auction_used",
                    "last_club_used",
                    "latitude",
                    "longitude",
                    "location_coordinates",
                    "paypal_email_address",
                    "credit",
                    "preferred_bidder_number",
                    "can_submit_standalone_lots",
                    "can_create_club_auctions",
                    "paypal_enabled",
                    "square_enabled",
                    "is_trusted",
                ]
            )

        return target_userdata

    def set_next_promo(self):
        """Next Wednesday 10 AM in the user's time, or the existing value plus 7 days, in the future."""
        try:
            tz = pytz_timezone(self.timezone)
        except pytz.exceptions.UnknownTimeZoneError:
            tz = pytz_timezone(settings.TIME_ZONE)

        if self.next_promo_email_at is None:
            now_local = timezone.now().astimezone(tz)
            days_ahead = 2 - now_local.weekday()  # Wednesday is weekday 2
            if days_ahead <= 0:
                days_ahead += 7
            next_wednesday = now_local.date() + datetime.timedelta(days=days_ahead)
            naive_next_promo = datetime.datetime(  # noqa: DTZ001
                next_wednesday.year, next_wednesday.month, next_wednesday.day, 10, 0
            )
            self.next_promo_email_at = tz.localize(naive_next_promo, is_dst=False)
        else:
            self.next_promo_email_at = self.next_promo_email_at + datetime.timedelta(days=7)
            now = timezone.now()
            while self.next_promo_email_at <= now:
                self.next_promo_email_at += datetime.timedelta(days=7)
        self.save(update_fields=["next_promo_email_at"])

    def send_websocket_message(self, message):
        channel_layer = channels.layers.get_channel_layer()
        async_to_sync(channel_layer.group_send)(f"user_{self.user.pk}", message)

    @cached_property
    def my_lots_qs(self):
        """All lots this user submitted, whether in an auction, or independently"""
        return Lot.objects.filter(Q(user=self.user) | Q(auctiontos_seller__user=self.user)).exclude(is_deleted=True)

    @cached_property
    def lots_submitted(self):
        """All lots this user has submitted, including unsold"""
        return self.my_lots_qs.count()

    @cached_property
    def lots_sold(self):
        """All lots this user has sold"""
        return self.my_lots_qs.filter(winner__isnull=False).count()

    @cached_property
    def total_sold(self):
        """Total amount this user has sold on this site"""
        return self.my_lots_qs.aggregate(total=Sum("winning_price"))["total"] or 0

    @cached_property
    def species_sold(self):
        """Distinct species this user bred and sold. The breederboard columns using it are still off."""
        return self.my_lots_qs.filter(i_bred_this_fish=True, winner__isnull=False).values("species").distinct().count()

    @cached_property
    def my_won_lots_qs(self):
        """All lots won by this user, in an auction or independently"""
        return Lot.objects.filter(
            Q(winner=self.user) | Q(auctiontos_winner__user=self.user),
            winning_price__isnull=False,
        ).exclude(is_deleted=True)

    @cached_property
    def lots_bought(self):
        """Total number of lots this user has purchased"""
        return self.my_won_lots_qs.count()

    @cached_property
    def lots_bought_online(self):
        """Total number of lots this user has purchased only in online auctions"""
        return self.my_won_lots_qs.filter(auction__is_online=True).count()

    @cached_property
    def total_spent(self):
        """Total amount this user has spent on this site"""
        return self.my_won_lots_qs.aggregate(total=Sum("winning_price"))["total"] or 0

    @cached_property
    def calc_total_volume(self):
        """Bought + sold"""
        return self.total_spent + self.total_sold

    @cached_property
    def total_bids(self):
        """Total number of successful bids this user has placed (max one per lot)"""
        # return len(Bid.objects.filter(user=self.user, was_high_bid=True))
        return Bid.objects.exclude(is_deleted=True).filter(user=self.user).count()

    @cached_property
    def lots_viewed(self):
        """Lots viewed by this user (COUNT(*); PageView is huge)."""
        return PageView.objects.filter(user=self.user.pk).count()

    @cached_property
    def bought_to_sold(self):
        """Ratio of lots bought to lots sold"""
        if self.lots_sold:
            return self.lots_bought / self.lots_sold
        else:
            return 0

    @cached_property
    def bid_to_view(self):
        """Bids per lot viewed: low means browsing, high means buying."""
        if self.lots_viewed:
            return self.total_bids / self.lots_viewed
        else:
            return 0

    @cached_property
    def viewed_to_sold(self):
        """Ratio of lots viewed to lots sold"""
        if self.lots_viewed:
            return self.lots_sold / self.lots_viewed
        else:
            return 0

    @cached_property
    def dedication(self):
        """Ratio of bids to won lots, only for online auctions"""
        if self.lots_bought_online and self.total_bids:
            return self.lots_bought_online / self.total_bids
        else:
            return 0

    @cached_property
    def percent_success(self):
        """Ratio of bids to won lots, formatted"""
        return self.dedication * 100

    @cached_property
    def positive_feedback_as_seller(self):
        return self.my_lots_qs.filter(feedback_rating=1).count()

    @cached_property
    def negative_feedback_as_seller(self):
        return self.my_lots_qs.filter(feedback_rating=-1).count()

    @cached_property
    def percent_positive_feedback_as_seller(self):
        positive = self.positive_feedback_as_seller
        negative = self.negative_feedback_as_seller
        if not negative:
            return 100
        return int((positive / (positive + negative)) * 100)

    @cached_property
    def positive_feedback_as_winner(self):
        return self.my_won_lots_qs.filter(winner_feedback_rating=1).count()

    @cached_property
    def negative_feedback_as_winner(self):
        return self.my_won_lots_qs.filter(winner_feedback_rating=-1).count()

    @cached_property
    def percent_positive_feedback_as_winner(self):
        positive = self.positive_feedback_as_winner
        negative = self.negative_feedback_as_winner
        if not negative:
            return 100
        return int((positive / (positive + negative)) * 100)

    @cached_property
    def auctions_created(self):
        return Auction.objects.filter(created_by__pk=self.user.pk).count()

    @cached_property
    def auctions_admined(self):
        return Auction.objects.filter(auctiontos__email=self.user.email, auctiontos__is_admin=True).count()

    @cached_property
    def auctions_i_admin(self):
        """Every auction this user may change, as a queryset: created, TOS admin, or club permission. The set
        form of ``permission_check``. Superusers aren't special-cased.
        """
        user = self.user
        if not user.is_authenticated:
            return Auction.objects.none()
        club_ids = (
            ClubMember.objects.filter(user=user, is_deleted=False)
            .filter(Q(permission_admin=True) | Q(permission_manage_auctions=True))
            .values_list("club_id", flat=True)
        )
        return Auction.objects.filter(
            Q(created_by=user) | Q(auctiontos__is_admin=True, auctiontos__user=user) | Q(club_id__in=club_ids),
            is_deleted=False,
        ).distinct()

    @cached_property
    def only_club(self):
        """The club this user obviously belongs to (single-club mode or exactly one club), else None. Used for
        ``Species.club``; None is normal.
        """
        from .site_setup import get_single_club

        single = get_single_club()
        if single:
            return single
        clubs = list(Club.objects.filter(members__user=self.user, members__is_deleted=False).distinct()[:2])
        return clubs[0] if len(clubs) == 1 else None

    @property
    def can_take_card_payments(self):
        """True when this person administers an auction or club that could take card payments. Not a
        permission (``square_enabled`` is); shares its definition with the Tap to Pay warm-up.
        """
        from auctions.mobile.services.payments import PaymentService

        return PaymentService.user_can_take_payments(self.user)

    @property
    def square_access_request_mailto_query(self):
        """Pre-encoded mailto subject/body to request card payments. ``square_enabled`` is off by default as a
        fraud control, so the way through is a button.
        """
        admin_email = settings.ADMINS[0][1]
        subject = "Request access to accept card payments"
        body = (
            "Hello, I would like to connect a Square account so I can take card payments "
            "for my auction or club.\n\n"
            "My club's website/Facebook page is:\n\n"
            f"My username here is: {self.user}\n\n"
            "Thank you!"
        )
        return f"{admin_email}?subject={quote_plus(subject)}&body={quote_plus(body)}"

    @cached_property
    def runs_an_auction(self):
        """True when this user administers any auction: the bar for adding a species (it stays theirs until approved)."""
        return self.user.is_superuser or self.auctions_i_admin.exists()

    @property
    def is_experienced(self):
        if self.auctions_created + self.auctions_admined > 2:
            return True
        return False

    @property
    def subscriptions(self):
        return ChatSubscription.objects.filter(
            user=self.user, lot__is_deleted=False, lot__banned=False, unsubscribed=False
        ).order_by("-createdon")

    @property
    def subscriptions_with_new_message_annotation(self):
        """Subscriptions annotated with ``new_message_count``."""
        return self.subscriptions.annotate(
            new_message_count=Count(
                "lot__lothistory",
                filter=Q(
                    lot__lothistory__removed=False,
                    lot__lothistory__changed_price=False,
                    lot__lothistory__timestamp__gt=F("last_seen"),
                )
                & ~Q(lot__lothistory__user=self.user),
            )
        )

    @property
    def unnotified_subscriptions(self):
        return self.subscriptions_with_new_message_annotation.annotate(
            unnotified_message_count=Count(
                "lot__lothistory",
                filter=Q(
                    lot__lothistory__removed=False,
                    lot__lothistory__changed_price=False,
                    lot__lothistory__timestamp__gt=F("last_notification_sent"),
                )
                & ~Q(lot__lothistory__user=self.user),
            )
        ).filter(unnotified_message_count__gt=0, new_message_count__gt=0)

    @property
    def unnotified_subscriptions_count(self):
        return self.unnotified_subscriptions.count()

    @property
    def my_lot_subscriptions(self):
        return self.unnotified_subscriptions.filter(lot__user=self.user)

    @property
    def my_lot_subscriptions_count(self):
        return self.my_lot_subscriptions.count()

    @property
    def other_lot_subscriptions(self):
        return self.unnotified_subscriptions.exclude(lot__user=self.user)

    @property
    def other_lot_subscriptions_count(self):
        return self.other_lot_subscriptions.count()

    @property
    def mark_all_subscriptions_notified(self):
        for subscription in self.subscriptions:
            subscription.last_notification_sent = timezone.now()
            subscription.save()

    @property
    def mark_all_subscriptions_seen(self):
        for subscription in self.subscriptions:
            subscription.last_seen = timezone.now()
            subscription.save()

    def save(self, *args, **kwargs):
        if not self.email_me_about_new_chat_replies:
            # One UPDATE; UserData saves on ordinary page views.
            ChatSubscription.objects.exclude(lot__user=self.user).filter(user=self.user, unsubscribed=False).update(
                unsubscribed=True
            )
        super().save(*args, **kwargs)

    def unsubscribe_from_all(self):
        self.email_me_about_new_auctions = False
        self.email_me_about_new_local_lots = False
        self.email_me_about_new_lots_ship_to_location = False
        self.email_me_when_people_comment_on_my_lots = False
        self.email_me_about_new_chat_replies = False
        self.send_reminder_emails_about_joining_auctions = False
        self.email_me_about_new_in_person_auctions = False
        self.has_unsubscribed = True
        self.last_activity = timezone.now()
        self.save()
        # Also mark any club members whose email matches as do not contact
        email = self.user.email
        if email:
            members = ClubMember.objects.filter(email=email, is_deleted=False).exclude(contact_status="do_not_contact")
            for member in members:
                member.contact_status = "do_not_contact"
                member.save(update_fields=["contact_status"])
                ClubHistory.objects.create(
                    club=member.club,
                    user=None,
                    action=f"{member} marked do not contact after unsubscribing from all emails",
                    applies_to="MEMBERS",
                )

    @cached_property
    def currency(self):
        # First check if user has set a preferred currency
        if self.preferred_currency:
            return self.preferred_currency
        # Fall back to PayPalSeller if available
        paypal_seller = PayPalSeller.objects.filter(user=self.user).first()
        if paypal_seller and paypal_seller.currency:
            return paypal_seller.currency
        # Fall back to location-based currency
        if not self.location:
            return "USD"
        if self.location.name == "Canada":
            return "CAD"
        return "USD"

    @property
    def has_push_device(self):
        """True with a push-enabled device carrying an FCM token."""
        return self.user.mobile_devices.filter(push_enabled=True).exclude(fcm_token="").exists()

    @property
    def has_app_push(self):
        """True when the app can receive a notification now. Ignores
        ``push_notifications_instead_of_email``, which is about email; browser and app push on one phone
        can't be told apart.
        """
        from auctions.notifications import push_configured

        return push_configured() and self.has_push_device

    def user_prefers_push(self):
        """Whether notifications go to the app instead of email: opted in, live device, push configured."""
        from auctions.notifications import push_configured

        if not self.push_notifications_instead_of_email:
            return False
        if not push_configured():
            return False
        return self.has_push_device


class PayPalSeller(models.Model):
    """A seller's PayPal info (basically one field), deleted when they disconnect."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    club = models.OneToOneField(
        Club,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="paypal_seller",
        help_text="If set, this PayPal account is the one used for the club's payments.",
    )
    paypal_merchant_id = models.CharField(max_length=64, blank=True, null=True)
    currency = models.CharField(max_length=10, default="USD")
    payer_email = models.EmailField(blank=True, null=True)
    connected_on = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        if self.currency != "USD":
            result = f"{self.currency} to "
        else:
            result = ""
        if self.payer_email:
            result += self.payer_email
        else:
            result += f"{self.user.first_name} {self.user.last_name}'s PayPal account"
        if self.club_id:
            result += f" (linked to {self.club.name})"
        return result

    def delete(self):
        # Disable PayPal on personal auctions of the OAuth user
        personal_auctions = Auction.objects.filter(created_by=self.user, enable_online_payments=True, club__isnull=True)
        for auction in personal_auctions:
            auction.create_history(
                applies_to="INVOICES",
                action=f"PayPal partner consent from {self.payer_email} has been revoked.  Relink your PayPal account to re-enable payments.",
                user=None,
            )
            auction.enable_online_payments = False
            auction.save()
        # Disable PayPal on club auctions if linked to the club.
        if self.club_id:
            club = self.club
            club_auctions = Auction.objects.filter(club=club, enable_online_payments=True)
            for auction in club_auctions:
                auction.create_history(
                    applies_to="INVOICES",
                    action=f"PayPal account {self.payer_email or self.user} has been disconnected from {club.name}.",
                    user=None,
                )
                auction.enable_online_payments = False
                auction.save()
            ClubHistory.objects.create(
                club=club,
                user=None,
                action=f"PayPal account disconnected ({self.payer_email or self.user})",
                applies_to="SETTINGS",
            )
        return super().delete()


# Square OAuth scopes. PAYMENTS_WRITE_IN_PERSON powers Tap to Pay; older tokens lack it and must
# reconnect.
SQUARE_TAP_TO_PAY_SCOPE = "PAYMENTS_WRITE_IN_PERSON"
SQUARE_OAUTH_SCOPES = (
    "PAYMENTS_WRITE",
    "PAYMENTS_READ",
    "MERCHANT_PROFILE_READ",
    "ORDERS_READ",
    "ORDERS_WRITE",
    SQUARE_TAP_TO_PAY_SCOPE,
)


def sanitize_square_phone(raw):
    """A Square-acceptable phone for the checkout pre-fill, or "". Square rejects the whole request on a bad
    phone, so junk is dropped. "+" and 10-15 digits.
    """
    if not raw:
        return ""
    has_plus = raw.strip().startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not (10 <= len(digits) <= 15):
        return ""
    return f"+{digits}" if has_plus else digits


class SquareSeller(models.Model):
    """A seller's Square merchant info and OAuth tokens (encrypted at rest)."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    club = models.OneToOneField(
        Club,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="square_seller",
        help_text="If set, this Square account is the one used for the club's payments.",
    )
    square_merchant_id = models.CharField(max_length=64, blank=True, null=True)
    access_token = EncryptedCharField(max_length=500, blank=True, null=True)
    refresh_token = EncryptedCharField(max_length=500, blank=True, null=True)
    token_expires_at = models.DateTimeField(blank=True, null=True)
    scopes = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Space-separated OAuth scopes granted at connect time. Empty = legacy connection (pre Tap to Pay).",
    )
    currency = models.CharField(max_length=10, default="USD")
    payer_email = models.EmailField(blank=True, null=True)
    connected_on = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        if self.currency != "USD":
            result = f"{self.currency} to "
        else:
            result = ""
        if self.payer_email:
            result += self.payer_email
        else:
            result += f"{self.user.first_name} {self.user.last_name}'s Square account"
        if self.club_id:
            result += f" (linked to {self.club.name})"
        return result

    def delete(self):
        # Disable Square on personal auctions of the OAuth user
        personal_auctions = Auction.objects.filter(created_by=self.user, enable_square_payments=True, club__isnull=True)
        for auction in personal_auctions:
            auction.create_history(
                applies_to="INVOICES",
                action=f"Square account {self.payer_email or self.user} has been disconnected. Relink your Square account to re-enable payments.",
                user=None,
            )
            auction.enable_square_payments = False
            auction.save()
        # Disable Square on club auctions if linked to the club.
        if self.club_id:
            club = self.club
            club_auctions = Auction.objects.filter(club=club, enable_square_payments=True)
            for auction in club_auctions:
                auction.create_history(
                    applies_to="INVOICES",
                    action=f"Square account {self.payer_email or self.user} has been disconnected from {club.name}.",
                    user=None,
                )
                auction.enable_square_payments = False
                auction.save()
            ClubHistory.objects.create(
                club=club,
                user=None,
                action=f"Square account disconnected ({self.payer_email or self.user})",
                applies_to="SETTINGS",
            )
        return super().delete()

    @property
    def supports_tap_to_pay(self):
        """True if the stored grant includes the Tap to Pay scope; older sellers must reconnect."""
        return SQUARE_TAP_TO_PAY_SCOPE in (self.scopes or "").split()

    def is_token_expired(self):
        """Check if the access token is expired or will expire soon (within 1 hour)"""
        if not self.token_expires_at:
            return False
        from datetime import timedelta

        buffer_time = timedelta(hours=1)
        return timezone.now() + buffer_time >= self.token_expires_at

    def refresh_access_token(self):
        """Refresh the Square access token. True on success."""
        if not self.refresh_token:
            logger.error("Cannot refresh Square token: no refresh_token available for user %s", self.user.pk)
            return False

        try:
            from square import Square
            from square.client import SquareEnvironment

            # Determine environment
            env = (
                SquareEnvironment.SANDBOX if settings.SQUARE_ENVIRONMENT == "sandbox" else SquareEnvironment.PRODUCTION
            )

            # Create client without authentication
            client = Square(environment=env)

            # Request new access token using refresh token
            result = client.o_auth.obtain_token(
                client_id=settings.SQUARE_APPLICATION_ID,
                client_secret=settings.SQUARE_CLIENT_SECRET,
                grant_type="refresh_token",
                refresh_token=self.refresh_token,
            )

            # Update tokens
            self.access_token = result.access_token
            # Code flow returns the same refresh token; PKCE a new one.
            if hasattr(result, "refresh_token") and result.refresh_token:
                self.refresh_token = result.refresh_token
            if hasattr(result, "expires_at") and result.expires_at:
                from datetime import datetime

                try:
                    self.token_expires_at = datetime.fromisoformat(result.expires_at.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    if isinstance(result.expires_at, datetime):
                        self.token_expires_at = result.expires_at

            self.save()
            logger.info("Successfully refreshed Square access token for user %s", self.user.pk)
            return True

        except Exception as e:
            logger.exception("Error refreshing Square access token for user %s: %s", self.user.pk, e)
            return False

    def get_valid_access_token(self):
        """A valid access token, refreshing if needed, or None."""
        if self.is_token_expired():
            logger.info("Square token expired for user %s, attempting refresh", self.user.pk)
            if not self.refresh_access_token():
                logger.error("Failed to refresh Square token for user %s", self.user.pk)
                return None
        return self.access_token

    def get_square_client(self):
        """A Square client using this seller's token, or None."""
        access_token = self.get_valid_access_token()
        if not access_token:
            logger.error("No valid OAuth token for user %s", self.user.pk)
            return None

        try:
            from square import Square
            from square.client import SquareEnvironment

            env = (
                SquareEnvironment.SANDBOX if settings.SQUARE_ENVIRONMENT == "sandbox" else SquareEnvironment.PRODUCTION
            )
            return Square(token=access_token, environment=env)
        except Exception as e:
            logger.exception("Error initializing Square client for user %s: %s", self.user.pk, e)
            return None

    def get_location_id(self):
        """The first active location id, or None."""
        client = self.get_square_client()
        if not client:
            return None

        try:
            loc_resp = client.locations.list()
            if getattr(loc_resp, "errors", None):
                logger.error("Failed to fetch Square locations for user %s: %s", self.user.pk, loc_resp.errors)
                return None

            locations = getattr(loc_resp, "locations", []) or []
            active_locations = [loc for loc in locations if getattr(loc, "status", None) == "ACTIVE"]

            if not active_locations:
                logger.error("No ACTIVE Square locations found for user %s", self.user.pk)
                return None

            location_id = getattr(active_locations[0], "id", None)
            if not location_id:
                logger.error("Could not determine location id for user %s", self.user.pk)
                return None

            return location_id
        except Exception as e:
            logger.exception("Error fetching Square location for user %s: %s", self.user.pk, e)
            return None

    def create_payment_link(self, invoice, request, member_pk=""):
        """Create a Square payment link for an invoice. ``member_pk`` sends a club invoice's redirect to that
        member's page. Returns ``(payment_url, error_message)``.
        """
        client = self.get_square_client()
        if not client:
            return None, "Failed to initialize Square client"

        location_id = self.get_location_id()
        if not location_id:
            logger.error("No location ID available for user %s", self.user.pk)
            return None, "Square location not configured"

        try:
            from decimal import Decimal

            # The rounded balance the buyer sees.
            amount_decimal = Decimal("0.00") - Decimal(invoice.rounded_net_after_payments)
            amount_cents = int(max(amount_decimal, Decimal("0.00")) * 100)
        except Exception:
            logger.exception("Failed to compute payment amount for invoice %s", invoice.pk)
            return None, "Failed to calculate payment amount"

        if amount_cents <= 0:
            logger.error("Computed amount invalid for invoice %s: %s cents", invoice.pk, amount_cents)
            return None, "Invalid payment amount"

        try:
            from django.urls import reverse

            if invoice.club:
                payment_note = f"Club membership fee for {invoice.club.name}"[:500]
            elif invoice.auctiontos_user and invoice.auction:
                payment_note = f"Bidder {invoice.auctiontos_user.bidder_number} in {invoice.auction.title}"[:500]
            else:
                payment_note = "Membership fee"[:500]

            # Get and validate buyer email
            if invoice.club and invoice.buyer:
                buyer_email = invoice.buyer.email
            else:
                buyer_email = getattr(getattr(invoice, "auctiontos_user", None), "email", None)

            # Square blocks some domains, like example.com.
            if buyer_email:
                from django.conf import settings

                email_domain = buyer_email.split("@")[-1].lower() if "@" in buyer_email else ""
                blocked_domains = settings.SQUARE_BLOCKED_EMAIL_DOMAINS
                if email_domain in blocked_domains:
                    buyer_email = None  # Don't send blocked email to Square

            # Check if pickup by mail - require shipping address
            ask_for_shipping_address = False
            if invoice.auctiontos_user and invoice.auctiontos_user.pickup_location:
                if invoice.auctiontos_user.pickup_location.pickup_by_mail:
                    ask_for_shipping_address = True

            # Pre-fill hints, truncated to Square's limits.
            pre_populated_data = {}
            if buyer_email:
                pre_populated_data["buyer_email"] = buyer_email
            if invoice.auctiontos_user:
                # Add buyer name if available (50 char limit per Square API)
                if invoice.auctiontos_user.name:
                    name_parts = invoice.auctiontos_user.name.split(None, 1)
                    if name_parts:
                        buyer_name = {"given_name": name_parts[0][:50]}
                        if len(name_parts) >= 2:
                            buyer_name["family_name"] = name_parts[1][:50]
                        pre_populated_data["buyer_name"] = buyer_name
                # Only a plausible phone; a bad one fails the whole request.
                buyer_phone = sanitize_square_phone(invoice.auctiontos_user.phone_number)
                if buyer_phone:
                    pre_populated_data["buyer_phone_number"] = buyer_phone
                # Add address if available (500 char limit per Square API)
                if invoice.auctiontos_user.address:
                    pre_populated_data["buyer_address"] = {
                        "address_line_1": invoice.auctiontos_user.address[:500],
                    }

            if invoice.club:
                club_path = None
                if member_pk:
                    member = ClubMember.objects.filter(pk=member_pk, club=invoice.club, is_deleted=False).first()
                    if member and member.membership_number:
                        club_path = reverse(
                            "club_member_by_number",
                            kwargs={"slug": invoice.club.slug, "number": member.membership_number},
                        )
                    elif member:
                        club_path = reverse(
                            "club_member_by_uuid",
                            kwargs={"slug": invoice.club.slug, "uuid": member.uuid},
                        )
                if not club_path:
                    club_path = reverse("club_detail", kwargs={"slug": invoice.club.slug})
                redirect_url = request.build_absolute_uri(club_path)
            else:
                redirect_url = request.build_absolute_uri(
                    reverse("square_payment_success", kwargs={"uuid": invoice.no_login_link})
                )
            link_resp = client.checkout.payment_links.create(
                idempotency_key=str(uuid_module.uuid4()),
                checkout_options={
                    "redirect_url": redirect_url,
                    "ask_for_shipping_address": ask_for_shipping_address,
                },
                pre_populated_data=pre_populated_data or {},
                order={
                    "location_id": location_id,
                    "reference_id": str(invoice.pk),
                    "line_items": [
                        {
                            "name": payment_note,
                            "quantity": "1",
                            "base_price_money": {"amount": amount_cents, "currency": self.currency},
                        }
                    ],
                },
            )

            payment_link_obj = getattr(link_resp, "payment_link", None)
            payment_url = getattr(payment_link_obj, "url", None)
            if not payment_url:
                logger.error("Payment link response missing URL for invoice %s: %s", invoice.pk, link_resp)
                return None, "Square did not return a payment link"

            return payment_url, None

        except Exception as e:
            logger.exception("Error creating Square payment link for invoice %s", invoice.pk)
            # Try to extract error details from Square API error
            error_msg = "Failed to create Square payment link"
            if hasattr(e, "body") and isinstance(e.body, dict):
                errors = e.body.get("errors", [])
                if errors and isinstance(errors, list) and len(errors) > 0:
                    error_detail = errors[0].get("detail", "")
                    error_code = errors[0].get("code", "")
                    if error_code == "INVALID_EMAIL_ADDRESS":
                        error_msg = "The email address on your account is not valid for Square payments. Please contact the auction organizer to update your email address."
                    elif error_code == "INVALID_PHONE_NUMBER":
                        # Backstop: explain a phone problem rather than show Square's code.
                        error_msg = "The phone number on your account is not valid for Square payments. Please contact the auction organizer to update your phone number."
                    elif error_detail:
                        error_msg = f"Square error: {error_detail}"
            return None, error_msg

    def process_refund(self, payment, refund_amount, reason):
        """Process a Square refund. Error string or None."""
        client = self.get_square_client()
        if not client:
            return "Failed to initialize Square client"

        try:
            from decimal import Decimal

            # Convert amount to cents
            refund_amount_cents = int(Decimal(str(refund_amount)) * 100)

            client.refunds.refund_payment(
                payment_id=payment.external_id,
                idempotency_key=str(uuid_module.uuid4()),
                amount_money={
                    "amount": refund_amount_cents,
                    "currency": payment.currency,
                },
                reason=reason,
            )
            # Webhook will handle creating the negative InvoicePayment record
            return None

        except Exception as e:
            error_msg = str(e)
            if hasattr(e, "body") and isinstance(e.body, dict):
                error_msg = e.body.get("message", str(e))
            logger.exception("Square refund failed for payment %s: %s", payment.external_id, error_msg)
            return f"Square refund failed: {error_msg}"


class UserInterestCategory(models.Model):
    """How interested a user is in a category."""

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    category = models.ForeignKey(Category, on_delete=models.CASCADE)
    interest = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)])
    as_percent = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])

    def __str__(self):
        return f"{self.user} interest level in {self.category} is {self.as_percent}"

    @classmethod
    def add_interest(cls, user, category, weight):
        """Increment a user's interest in a category, creating the row if needed. filter().first(), not
        get_or_create: duplicates are tolerated (no unique constraint) and merged by
        deduplicate_user_interest.
        """
        interest = cls.objects.filter(user=user, category=category).first()
        if interest is None:
            interest = cls(user=user, category=category, interest=weight)
        else:
            interest.interest += weight
        interest.save()
        return interest

    def save(self, *args, **kwargs):
        """Normalize interest relative to the user's strongest interest."""
        try:
            maxInterest = UserInterestCategory.objects.filter(user=self.user).order_by("-interest")[0].interest
            self.as_percent = int(((self.interest + 1) / maxInterest) * 100)  # + 1 for the times maxInterest is 0
            self.as_percent = min(self.as_percent, 100)
        except Exception:
            self.as_percent = 100
        super().save(*args, **kwargs)


class LotHistory(models.Model):
    lot = models.ForeignKey(Lot, blank=True, null=True, on_delete=models.CASCADE)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    user.help_text = "The user who posted this message."
    message = models.CharField(max_length=400, blank=True, null=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    seen = models.BooleanField(default=False)
    seen.help_text = "Has the lot submitter seen this message?"
    current_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    current_price.help_text = "Price of the lot immediately AFTER this message"
    changed_price = models.BooleanField(default=False)
    changed_price.help_text = (
        "Was this a bid that changed the price?  If False, this lot will show up in the admin chat system"
    )
    notification_sent = models.BooleanField(default=False)
    notification_sent.help_text = "Set to true automatically when the notification email is sent"
    bid_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    bid_amount.help_text = "For any kind of debugging"
    removed = models.BooleanField(default=False)

    def __str__(self):
        if self.message:
            return f"{self.message}"
        else:
            return "message"

    class Meta:
        verbose_name_plural = "Chat history"
        verbose_name = "Chat history"
        ordering = ["timestamp"]


class AuctionHistory(models.Model):
    """Changelog of changes made to an auction by admin users"""

    auction = models.ForeignKey(Auction, on_delete=models.CASCADE)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=800, blank=True, null=True)
    # {field_name: {"from": x, "to": y}}; see auctions/history.py.
    changed_fields = models.JSONField(default=dict, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    applies_to = models.CharField(
        null=True,
        blank=True,
        max_length=100,
        choices=(
            ("RULES", "Rules"),
            ("USERS", "Users"),
            ("INVOICES", "Invoices"),
            ("LOTS", "Lots"),
            # LOT_WINNERS removed (sales are logged under LOTS); STATS added (written by
            # update_auction_stats, previously undeclared).
            ("STATS", "Stats"),
        ),
    )

    def __str__(self):
        if self.user:
            return f"{self.user.first_name} {self.user.last_name} {self.action}"
        else:
            return f"System {self.action}"


class AdCampaignGroup(CachedPropertiesMixin, models.Model):
    title = models.CharField(max_length=100, default="Untitled campaign")
    contact_user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    paid = models.BooleanField(default=False)
    total_cost = models.FloatField(default=0)

    def __str__(self):
        return f"{self.title}"

    @staticmethod
    def annotate_totals(queryset):
        """Add campaign, impression and click counts as subqueries for the changelist."""
        campaigns = AdCampaign.objects.filter(campaign_group=OuterRef("pk"))
        responses = AdCampaignResponse.objects.filter(campaign__campaign_group=OuterRef("pk"))

        def count_of(rows, group_by, **extra):
            return Coalesce(
                Subquery(
                    rows.filter(**extra).order_by().values(group_by).annotate(total=Count("pk")).values("total")[:1],
                    output_field=IntegerField(),
                ),
                Value(0),
            )

        return queryset.annotate(
            annotated_campaigns=count_of(campaigns, "campaign_group"),
            annotated_impressions=count_of(responses, "campaign__campaign_group"),
            annotated_clicks=count_of(responses, "campaign__campaign_group", clicked=True),
        )

    @cached_property
    def number_of_clicks(self):
        """From the annotation when there is one -- see ``annotate_totals``."""
        annotated = getattr(self, "annotated_clicks", None)
        if annotated is not None:
            return annotated
        return AdCampaignResponse.objects.filter(campaign__campaign_group=self.pk, clicked=True).count()

    @cached_property
    def number_of_impressions(self):
        """How many times ads in this campaign group have been viewed."""
        annotated = getattr(self, "annotated_impressions", None)
        if annotated is not None:
            return annotated
        return AdCampaignResponse.objects.filter(campaign__campaign_group=self.pk).count()

    @property
    def click_rate(self):
        """What percent of views result in a click"""
        return (self.number_of_clicks / (self.number_of_impressions + 1)) * 100

    @cached_property
    def number_of_campaigns(self):
        """How many campaigns are there in this group"""
        annotated = getattr(self, "annotated_campaigns", None)
        if annotated is not None:
            return annotated
        return AdCampaign.objects.filter(campaign_group=self.pk).count()


class AdCampaign(CachedPropertiesMixin, CloudflareImageMixin, models.Model):
    image = ThumbnailerImageField(upload_to="images/", blank=True)
    campaign_group = models.ForeignKey(AdCampaignGroup, null=True, blank=True, on_delete=models.SET_NULL)
    title = models.CharField(max_length=50, default="Click here")
    text = models.CharField(max_length=40, blank=True, null=True)
    body_html = models.CharField(max_length=300, default="")
    external_url = models.URLField(max_length=300)
    begin_date = models.DateTimeField(blank=True, null=True)
    end_date = models.DateTimeField(blank=True, null=True)
    max_ads = models.PositiveIntegerField(
        default=10000000, validators=[MinValueValidator(0), MaxValueValidator(10000000)]
    )
    max_clicks = models.PositiveIntegerField(
        default=10000000, validators=[MinValueValidator(0), MaxValueValidator(10000000)]
    )
    category = models.ForeignKey(
        Category,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        verbose_name="Category",
    )
    category.help_text = "If set, this ad will only be shown to users interested in this particular category"
    auction = models.ForeignKey(Auction, blank=True, null=True, on_delete=models.SET_NULL)
    auction.help_text = "If set, this campaign will only be run on a particular auction (leave blank for site-wide)"
    bid = models.FloatField(default=1)
    bid.help_text = "At the moment, this is not actually the cost per click, it's the percent chance of showing this ad.  If the top ad fails, the next one will be selected.  If there are none left, google ads will be loaded.  Expects 0-1"

    def __str__(self):
        if self.campaign_group:
            return f"{self.campaign_group.title} - {self.title} ({self.click_rate:.2f}% clicked)"
        return f"{self.title}"

    @staticmethod
    def annotate_response_counts(queryset):
        """Add impression and click counts as subqueries for the changelist."""
        responses = AdCampaignResponse.objects.filter(campaign=OuterRef("pk"))

        def count_of(**extra):
            return Coalesce(
                Subquery(
                    responses.filter(**extra)
                    .order_by()
                    .values("campaign")
                    .annotate(total=Count("pk"))
                    .values("total")[:1],
                    output_field=IntegerField(),
                ),
                Value(0),
            )

        return queryset.annotate(annotated_impressions=count_of(), annotated_clicks=count_of(clicked=True))

    @property
    def image_display_url(self):
        """Ad-sized (250x150 max) image URL; from Cloudflare when migrated"""
        return cloudflare_images.image_url(self.image, self.cloudflare_image_id, "ad")

    @cached_property
    def number_of_clicks(self):
        """..."""
        annotated = getattr(self, "annotated_clicks", None)
        if annotated is not None:
            return annotated
        return AdCampaignResponse.objects.filter(campaign=self.pk, clicked=True).count()

    @cached_property
    def number_of_impressions(self):
        """Times this ad was viewed, from the annotation when present."""
        annotated = getattr(self, "annotated_impressions", None)
        if annotated is not None:
            return annotated
        return AdCampaignResponse.objects.filter(campaign=self.pk).count()

    @property
    def click_rate(self):
        """What percent of views result in a click"""
        return (self.number_of_clicks / (self.number_of_impressions + 1)) * 100


class AdCampaignResponse(models.Model):
    campaign = models.ForeignKey(AdCampaign, on_delete=models.CASCADE)
    responseid = models.CharField(max_length=255, default=uuid_module.uuid4, blank=True)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    session = models.CharField(max_length=250, blank=True, null=True)
    text = models.CharField(max_length=250, blank=True, null=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    clicked = models.BooleanField(default=False)

    def __str__(self):
        if self.user:
            user = self.user
        else:
            user = "Anonymous"
        if self.clicked:
            action = "clicked"
        else:
            action = "viewed"
        return f"{user} {action}"


class AuctionCampaign(CachedPropertiesMixin, models.Model):
    auction = models.ForeignKey(Auction, null=True, blank=True, on_delete=models.SET_NULL)
    uuid = models.CharField(max_length=255, default=uuid_module.uuid4, blank=True)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    email = models.CharField(max_length=255, default="", blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    source = models.CharField(max_length=200, blank=True, null=True, default="")
    result = models.CharField(
        max_length=20,
        choices=(
            ("ERR", "No email sent"),
            ("NONE", "No response"),
            ("VIEWED", "Clicked"),
            ("JOINED", "Joined"),
        ),
        default="NONE",
        db_index=True,
    )
    email_sent = models.BooleanField(default=False)

    @cached_property
    def link(self):
        current_site = Site.objects.get_current()
        return f"{current_site.domain}/auctions/{self.uuid}"

    @property
    def update(self):
        """Manually update the result of this campaign"""
        if self.user and self.auction and self.result == "NONE":
            pageview = PageView.objects.filter(
                user=self.user, auction=self.auction, date_start__gte=self.timestamp
            ).first()
            if pageview:
                self.result = "VIEWED"
            tos = AuctionTOS.objects.filter(user=self.user, auction=self.auction, createdon__gte=self.timestamp).first()
            if tos:
                self.result = "JOINED"
            if pageview or tos:
                self.save()

    def save(self, *args, **kwargs):
        # duplicate check on initial creation
        if not self.pk:
            duplicate = AuctionCampaign.objects.filter(auction=self.auction)
            if self.user:
                duplicate = duplicate.filter(user=self.user)
            if self.email:
                duplicate = duplicate.filter(email=self.email)
            if self.user or self.email:
                duplicate = duplicate.first()
                if duplicate:
                    msg = "A campaign with this auction and user/email already exists."
                    raise ValidationError(msg)
        super().save(*args, **kwargs)


class LotImage(InvalidatesRelatedCache, CloudflareImageMixin, models.Model):
    """An image that belongs to a lot.  Each lot can have multiple images"""

    # Lot.images is a cached list, and image_count and thumbnail read it
    invalidates_cache_on = ("lot_number",)

    PIC_CATEGORIES = (
        ("ACTUAL", "My photo of this exact item"),
        (
            "REPRESENTATIVE",
            "My photo, but not of this exact item.  e.x. This is the parents of these fry",
        ),
        # Was "This picture is from the internet"; see LotImage.
        ("RANDOM", "Not my photo - I have permission to use it"),
    )
    lot_number = models.ForeignKey(Lot, on_delete=models.CASCADE)
    caption = models.CharField(max_length=60, blank=True, null=True)
    caption.help_text = "Optional"
    image = ThumbnailerImageField(
        upload_to="images/",
        blank=True,
        null=True,
        resize_source={"size": (600, 600), "quality": 85},
    )
    image.help_text = "Select an image to upload"
    url = models.URLField(blank=True, null=True)
    url.help_text = "Or enter a URL to an image instead of uploading one"
    image_source = models.CharField(max_length=20, choices=PIC_CATEGORIES, blank=True)
    is_primary = models.BooleanField(default=False, blank=True)
    createdon = models.DateTimeField(auto_now_add=True)

    @property
    def display_url(self):
        """The display URL: the uploaded image (Cloudflare when migrated), else the url field."""
        return cloudflare_images.image_url(self.image, self.cloudflare_image_id) or self.url or None

    @property
    def thumbnail_url(self):
        """Small (250x150) version of display_url for lot tiles and carousel previews"""
        return cloudflare_images.image_url(self.image, self.cloudflare_image_id, "lot_list") or self.url or None

    @property
    def source_display(self):
        """The source label shown under a picture. ``RANDOM`` shows nothing: it's also the blank default and
        tells a bidder nothing.
        """
        if self.image_source == "RANDOM":
            return ""
        return self.get_image_source_display()


class FAQ(models.Model):
    """FAQ entries, maintained in the admin."""

    category_text = models.CharField(max_length=100)
    question = models.CharField(max_length=200)
    answer = MarkdownField(
        rendered_field="answer_rendered",
        validator=VALIDATOR_STANDARD,
        blank=True,
        null=True,
    )
    answer.help_text = "To add a link: [Link text](https://www.google.com)"
    answer_rendered = RenderedMarkdownField(blank=True, null=True)
    slug = AutoSlugField(populate_from="question", unique=True)
    createdon = models.DateTimeField(auto_now_add=True)
    include_in_auctiontos_confirm_email = models.BooleanField(default=False, blank=True)
    agent_only = models.BooleanField(default=False, blank=True)
    agent_only.help_text = (
        "Keep this off the FAQ page, but let the site's assistant answer out of it. "
        "For answers worth having written down that are not worth a heading on a public page: "
        "an edge case, something only an auction admin ever hits, or a question people ask an "
        "assistant and never a page. It is not private -- anyone can reach it by asking."
    )


class SearchHistory(models.Model):
    """To keep track of what people are searching for"""

    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    search = models.CharField(max_length=600)
    createdon = models.DateTimeField(auto_now_add=True)
    auction = models.ForeignKey(Auction, null=True, blank=True, on_delete=models.SET_NULL)


class CommandPalettePage(models.Model):
    """Maps a phrase typed in the command palette to destination pages. Managed in migrations and the admin."""

    search_term = models.CharField(
        max_length=200, db_index=True, help_text="The phrase people type, e.g. 'sell lots' or 'address'."
    )
    synonyms = models.CharField(
        max_length=500,
        blank=True,
        help_text="Extra phrases that map here too (comma- or newline-separated). Good for typos and alternate wording.",
    )
    target = models.CharField(
        max_length=100,
        blank=True,
        help_text=(
            "Dynamic destination key resolved against the user's context, e.g. "
            "'last_auction:set_winners' or 'clubs:brevo'. Leave blank to use the URL field instead."
        ),
    )
    url = models.CharField(
        max_length=500, blank=True, help_text="Literal path used when target is blank, e.g. '/selling/'."
    )
    title = models.CharField(
        max_length=200, blank=True, help_text="Optional label override. Leave blank to use a sensible default."
    )
    description = models.CharField(max_length=400, blank=True)
    icon = models.CharField(max_length=50, blank=True, help_text="Bootstrap icon class, e.g. 'bi-cash-coin'.")
    model = models.CharField(
        max_length=20, blank=True, help_text="Optional hint: auction, club, or lot. Not currently required."
    )
    hits = models.PositiveIntegerField(default=0, help_text="Number of times this shortcut has been clicked.")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-hits", "search_term"]

    def __str__(self):
        return f"{self.search_term} -> {self.target or self.url}"


class CommandPaletteSearch(models.Model):
    """One row per command-palette search session, updated as the query is refined, recording click,
    abandon or bounce. Signed-in users only.
    """

    RESULT_PENDING = "pending"
    RESULT_CLICKED = "clicked"
    RESULT_ABANDONED = "abandoned"
    RESULT_BOUNCE = "bounce"
    RESULT_CHOICES = [
        (RESULT_PENDING, "In progress"),
        (RESULT_CLICKED, "Clicked a result"),
        (RESULT_ABANDONED, "Cleared or left"),
        (RESULT_BOUNCE, "No results found"),
    ]

    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    search = models.CharField(max_length=600, blank=True)
    createdon = models.DateTimeField(auto_now_add=True)
    updatedon = models.DateTimeField(auto_now=True)
    result = models.CharField(max_length=20, choices=RESULT_CHOICES, default=RESULT_PENDING)
    result_type = models.CharField(
        max_length=50, blank=True, help_text="auction, lot, club, clubmember, page, or default."
    )
    result_url = models.CharField(max_length=500, blank=True)
    result_object_id = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-createdon"]

    def __str__(self):
        return f"{self.user} searched '{self.search}' ({self.result})"


class LLMUsage(models.Model):
    """One row per command palette language-model call, including failures, for cost and success analytics."""

    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    createdon = models.DateTimeField(auto_now_add=True)
    model = models.CharField(max_length=100, blank=True, help_text="Model string reported by the provider.")
    prompt_tokens = models.PositiveIntegerField(default=0)
    cached_prompt_tokens = models.PositiveIntegerField(
        default=0,
        help_text=(
            "How many of the prompt tokens the provider served from its own cache. Billed at a "
            "fraction of the normal input rate, so a total that ignores this badly overstates the "
            "bill: the system prompt is the same ~3k tokens on every call and 90%+ of it is a "
            "cache hit."
        ),
    )
    completion_tokens = models.PositiveIntegerField(default=0)
    total_tokens = models.PositiveIntegerField(default=0)
    query = models.CharField(max_length=600, blank=True, help_text="What the user typed or said.")
    response_kind = models.CharField(
        max_length=30,
        blank=True,
        help_text="navigate, countdown, clarify, done, error, or lookup for an intermediate round.",
    )
    action = models.CharField(max_length=50, blank=True, help_text="Registry action name, when one was chosen.")
    destination = models.CharField(
        max_length=100,
        blank=True,
        db_index=True,
        help_text=(
            "For a navigation, the palette_routes key it landed on. This is what "
            "`manage.py mine_palette_shortcuts` reads: a query that resolves to the same "
            "destination every time never needs to be asked about again."
        ),
    )
    success = models.BooleanField(default=True)
    cancelled = models.BooleanField(
        default=False,
        db_index=True,
        help_text=(
            "The user hit Cancel during the confirm countdown instead of letting the action run. "
            "This is the only signal we get that we understood the words but picked the wrong "
            "thing to do -- the action never ran, so nothing else records it. A query that is "
            "repeatedly cancelled is a bad match worth fixing."
        ),
    )
    reported = models.BooleanField(
        default=False,
        db_index=True,
        help_text=(
            "The user pressed 'tell the site owner' after this command failed. Every other failure "
            "signal here is inferred; this one is a person deciding it was worth saying so, which "
            "makes it the shortest queue on the analytics page and the first one worth reading."
        ),
    )

    class Meta:
        ordering = ["-createdon"]
        verbose_name = "LLM usage"
        verbose_name_plural = "LLM usage"

    def __str__(self):
        return f"{self.user} · {self.model} · {self.total_tokens} tokens ({self.response_kind})"


class ChatSubscription(models.Model):
    """Get notifications about new chat messages on lots"""

    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    createdon = models.DateTimeField(auto_now_add=True)
    lot = models.ForeignKey(Lot, on_delete=models.CASCADE)
    last_notification_sent = models.DateTimeField(blank=True, null=True)
    last_seen = models.DateTimeField(blank=True, null=True)
    unsubscribed = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        if not self.last_notification_sent:
            self.last_notification_sent = timezone.now()
        if not self.last_seen:
            self.last_seen = timezone.now()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user} on lot {self.lot} Unsubscribed: ({self.unsubscribed})"


class MobileDevice(models.Model):
    """Registered mobile device for a user."""

    PLATFORM_IOS = "ios"
    PLATFORM_ANDROID = "android"
    PLATFORM_CHOICES = [
        (PLATFORM_IOS, "iOS"),
        (PLATFORM_ANDROID, "Android"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="mobile_devices")
    device_uuid = models.UUIDField(unique=True, db_index=True)
    device_name = models.CharField(max_length=200, blank=True)
    platform = models.CharField(max_length=10, choices=PLATFORM_CHOICES, blank=True)
    app_version = models.CharField(max_length=50, blank=True)
    # FCM token; blank means no push target. Follows the install; signing out clears it.
    fcm_token = models.TextField(blank=True, default="", db_index=False)
    fcm_token_updated_at = models.DateTimeField(null=True, blank=True)
    push_enabled = models.BooleanField(default=True)  # per-device kill switch
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)
    # Presence for remote printing: the phone can't be summoned, so the app must be open. Posted at
    # mount, on resume, and every 5 minutes (POST /api/mobile/devices/heartbeat/).
    last_heartbeat = models.DateTimeField(null=True, blank=True, db_index=True)
    # The app says a printer is paired and a profile resolves now. Not derived from print_method.
    print_ready = models.BooleanField(default=False)
    printer_name = models.CharField(max_length=100, blank=True, default="")
    # Sticky: has it ever been print-ready? Decides whether /printing/ offers remote printing.
    ever_print_ready = models.BooleanField(default=False)

    # One missed beat of slack.
    HEARTBEAT_GRACE = datetime.timedelta(minutes=6)

    class Meta:
        ordering = ["-last_seen"]

    def __str__(self):
        return f"{self.user} — {self.platform or 'unknown'} device ({self.device_uuid})"

    @property
    def is_reachable_for_printing(self):
        """``print_ready`` and heartbeating: the phone can print something right now."""
        if not self.print_ready or not self.last_heartbeat:
            return False
        return self.last_heartbeat >= timezone.now() - self.HEARTBEAT_GRACE

    @classmethod
    def reachable_printers_for(cls, user):
        """The user's phones that could print now, freshest heartbeat first."""
        if not user or not user.is_authenticated:
            return cls.objects.none()
        return cls.objects.filter(
            user=user,
            print_ready=True,
            last_heartbeat__gte=timezone.now() - cls.HEARTBEAT_GRACE,
        ).order_by("-last_heartbeat")

    @classmethod
    def print_presence_for(cls, user):
        """``(device, last_seen_or_None)`` for "your phone was last seen...": the reachable device, else the
        last print-ready one.
        """
        if not user or not user.is_authenticated:
            return None, None
        device = cls.reachable_printers_for(user).first()
        if device is None:
            device = (
                cls.objects.filter(user=user, ever_print_ready=True)
                .exclude(last_heartbeat=None)
                .order_by("-last_heartbeat")
                .first()
            )
        return device, (device.last_heartbeat if device else None)


class RemotePrintJob(models.Model):
    """A request, from a computer, to print labels on the phone's paired Bluetooth printer.

    The phone can't be summoned (Android and iOS both prevent it), so the app must already be open;
    ``MobileDevice`` measures that, and this row lets the computer report what really happened. The
    website creates and pushes it, the phone posts progress and a result, the page polls. ``message`` is
    the app's own text, shown verbatim.
    """

    STATUS_QUEUED = "queued"
    STATUS_SENT = "sent"
    STATUS_PRINTING = "printing"
    STATUS_PRINTED = "printed"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_UNREACHABLE = "unreachable"
    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_SENT, "Sent to the phone"),
        (STATUS_PRINTING, "Printing"),
        (STATUS_PRINTED, "Printed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_UNREACHABLE, "Couldn't reach the phone"),
    ]
    # Statuses nothing further happens to.
    TERMINAL_STATUSES = {STATUS_PRINTED, STATUS_FAILED, STATUS_CANCELLED, STATUS_UNREACHABLE}
    # Silence after a push before giving up: the app posts progress per label.
    SILENCE_BEFORE_UNREACHABLE = datetime.timedelta(seconds=20)

    uuid = models.UUIDField(primary_key=True, default=uuid_module.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="remote_print_jobs")
    # SET_NULL: keep the record if the phone unregisters.
    device = models.ForeignKey(
        MobileDevice, on_delete=models.SET_NULL, null=True, blank=True, related_name="print_jobs"
    )
    # Lot pks in print order.
    lots = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    printed_count = models.IntegerField(default=0)
    total_count = models.IntegerField(default=0)
    # The app's failure text, verbatim. Never written by the server.
    message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self):
        return f"{self.total_count} labels to {self.device or 'no device'} ({self.status})"

    @property
    def is_terminal(self):
        return self.status in self.TERMINAL_STATUSES

    @property
    def has_gone_quiet(self):
        """Pushed, never answered, and out of time. Only from ``sent``."""
        if self.status != self.STATUS_SENT:
            return False
        return timezone.now() - self.updated_at > self.SILENCE_BEFORE_UNREACHABLE

    def lots_qs(self):
        """The job's lots in stored print order."""
        by_pk = Lot.objects.in_bulk(self.lots)
        return [by_pk[pk] for pk in self.lots if pk in by_pk]

    def mark_labels_printed(self, count):
        """Mark the first *count* lots printed, from the app's result post."""
        if count <= 0:
            return 0
        lots = [lot for lot in self.lots_qs()[:count] if not lot.is_deleted]
        for lot in lots:
            lot.label_printed = True
            lot.label_needs_reprinting = False
        Lot.objects.bulk_update(lots, ["label_printed", "label_needs_reprinting"])
        return len(lots)


class MobileOfflineOp(models.Model):
    """Idempotency ledger for offline-sync ops (POST /api/mobile/offline/sync/).

    The app replays its queue, possibly more than once; a row per applied ``op_id`` returns the original
    result and resolves ``op:<op_id>`` references. Conflicts aren't recorded, so they re-evaluate.
    """

    op_id = models.CharField(max_length=64, unique=True, db_index=True)
    auction = models.ForeignKey(Auction, on_delete=models.CASCADE, related_name="offline_ops")
    # The syncing admin.
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    op_type = models.CharField(max_length=20)
    # pk created (AuctionTOS for add_user, Lot for add_lot); null for set_winner.
    result_pk = models.IntegerField(null=True, blank=True)
    # Echoed result fields, returned again on replay.
    result_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["auction", "op_type"])]

    def __str__(self):
        return f"{self.op_type} {self.op_id} (auction {self.auction_id})"


class ThermalPrinterProfile(models.Model):
    """A Bluetooth thermal printer the app can drive. Every byte sent is defined here; a new printer is a
    row, not an app release.
    """

    # Name the printer, not the protocol: the person choosing is looking at a box.
    slug = models.SlugField(unique=True)  # stable id the app caches/reports
    name = models.CharField(max_length=100)  # "Fichero / AiYin D11s"
    enabled = models.BooleanField(default=True)
    priority = models.PositiveIntegerField(default=100)  # match order, low wins
    schema_version = models.PositiveIntegerField(default=1)  # command-program schema
    # The print program's language; stating it lets a language probe auto-select this profile.
    command_language = models.CharField(
        max_length=20, choices=printer_programs.COMMAND_LANGUAGE_CHOICES, blank=True, default=""
    )

    # ── Matching ──
    # Case-insensitive regexes against the advertised BLE name (empty = manual only). Names are
    # unreliable, so the app also matches GATT Device Information model (0x2A24) and manufacturer
    # (0x2A29) against the next two lists.
    ble_name_patterns = models.JSONField(default=list, blank=True)
    model_patterns = models.JSONField(default=list, blank=True)
    manufacturer_patterns = models.JSONField(default=list, blank=True)
    # Optional exact GATT ids; blank discovers the first writable characteristic.
    service_uuid = models.CharField(max_length=40, blank=True, default="")
    write_characteristic_uuid = models.CharField(max_length=40, blank=True, default="")
    notify_characteristic_uuid = models.CharField(max_length=40, blank=True, default="")

    # ── Transport pacing ──
    chunk_size = models.PositiveIntegerField(default=200)  # bytes per BLE write
    chunk_delay_ms = models.PositiveIntegerField(default=20)  # gap between chunks
    prefer_write_with_response = models.BooleanField(default=True)

    # ── Raster geometry ──
    print_width_px = models.PositiveIntegerField(default=96)  # printhead dots
    dpi = models.PositiveIntegerField(default=203)
    invert_raster = models.BooleanField(default=False)  # 1 = white printers
    max_label_width_mm = models.FloatField(null=True, blank=True)
    max_label_height_mm = models.FloatField(null=True, blank=True)

    # ── Command programs (JSON, see auctions.printer_programs) ──
    print_program = models.JSONField()  # required
    status_program = models.JSONField(default=list, blank=True)  # optional pre-flight
    status_flags = models.JSONField(default=dict, blank=True)  # byte/bit → condition
    label_size_program = models.JSONField(default=list, blank=True)  # optional size read
    label_size_parse = models.JSONField(default=dict, blank=True)

    notes = models.TextField(blank=True, default="")  # admin-facing: quirks, sources

    class Meta:
        ordering = ["priority", "name"]

    def __str__(self):
        return f"{self.name} ({self.slug})"

    def clean(self):
        """Validate command programs so typos are rejected here, not on the printer."""
        from auctions.printer_programs import (
            ProgramValidationError,
            validate_match_patterns,
            validate_profile_programs,
        )

        super().clean()
        try:
            validate_profile_programs(
                print_program=self.print_program,
                status_program=self.status_program,
                label_size_program=self.label_size_program,
                status_flags=self.status_flags,
                label_size_parse=self.label_size_parse,
            )
            for field in ("ble_name_patterns", "model_patterns", "manufacturer_patterns"):
                validate_match_patterns(getattr(self, field), field)
        except ProgramValidationError as exc:
            raise ValidationError({exc.field or "print_program": str(exc)}) from exc


class ObservedPrinter(models.Model):
    """A Bluetooth printer a user paired and how the app identified it: a work queue for printer support.

    ``matched_by="manual"``: no profile matched; its model/manufacturer belong in a profile's patterns.
    Blank ``profile_slug`` or ``model``: needs a BLE-name pattern or a new profile. ``characterized``
    rows carry enough to draft a profile.

    One row per (user, ble_name, model, profile_slug); re-pairing bumps times_seen.
    """

    MATCHED_BY_CHOICES = [
        # The app's wire strings, verbatim.
        ("bleName", "BLE name pattern"),
        ("deviceInfo", "Device Information Service (model/manufacturer)"),
        ("serviceUuid", "Service UUID"),
        # Identified by probing its command language, distinct from GATT deviceInfo.
        ("probe", "Command-language probe"),
        ("manual", "User picked it manually"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    ble_name = models.CharField(max_length=100, blank=True, default="")  # advertised, user-editable
    # ── GATT 0x180A; blank = not reported ──
    manufacturer = models.CharField(max_length=100, blank=True, default="")  # 0x2A29
    model = models.CharField(max_length=100, blank=True, default="")  # 0x2A24
    firmware = models.CharField(max_length=100, blank=True, default="")  # 0x2A26
    hardware = models.CharField(max_length=100, blank=True, default="")  # 0x2A27
    service_uuids = models.JSONField(default=list, blank=True)  # advertised GATT services

    # ── Command-language probe replies ──
    # DIS often names the radio module, so the app sends each language's status query; the one that
    # answers is the language. ``{"tspl_status": {"hex": "00", "ascii": "."}}``.
    probe_replies = models.JSONField(default=dict, blank=True)
    # The answering language: which profile family a printer belongs in.
    probed_language = models.CharField(max_length=20, blank=True, default="", db_index=True)
    # The full GATT tree, needed to pick service/write/notify UUIDs (the first writable one can be
    # the radio's control channel).
    gatt = models.JSONField(default=list, blank=True)

    # ── Characterization ──
    # Status replies captured in four known physical states, so the status map is derived, not
    # guessed. ``{"cover_open": {"tspl_status": {"hex": "01"}}}``.
    status_captures = models.JSONField(default=dict, blank=True)
    # The derived status_flags.values map, ready for a profile.
    derived_status_values = models.JSONField(default=dict, blank=True)
    # States this printer can't distinguish; carry into the profile's notes.
    status_ambiguities = models.JSONField(default=list, blank=True)
    # Set when status_captures exist: the admin's work queue filter.
    characterized = models.BooleanField(default=False, db_index=True)
    # Whether this user was told their printer is now supported.
    support_notified = models.BooleanField(default=False)

    # A slug, not a FK: survives profile renames and unknown slugs. Blank = cancelled.
    profile_slug = models.CharField(max_length=50, blank=True, default="", db_index=True)
    matched_by = models.CharField(max_length=20, choices=MATCHED_BY_CHOICES, db_index=True)
    printed_ok = models.BooleanField(default=False)  # a label actually came out, not just paired
    times_seen = models.PositiveIntegerField(default=1)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_seen"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "ble_name", "model", "profile_slug"],
                name="unique_observed_printer_per_user",
            )
        ]

    def __str__(self):
        label = self.model or self.ble_name or "unidentified printer"
        return f"{label} → {self.profile_slug or 'no profile'} ({self.matched_by})"


class PushNotificationSent(models.Model):
    """One row per push actually handed to FCM — dedupe + stats."""

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    device = models.ForeignKey(MobileDevice, null=True, on_delete=models.SET_NULL)
    category = models.CharField(max_length=40, db_index=True)
    auction = models.ForeignKey(Auction, null=True, blank=True, on_delete=models.SET_NULL)
    invoice = models.ForeignKey(Invoice, null=True, blank=True, on_delete=models.SET_NULL)
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["user", "category", "auction"])]

    def __str__(self):
        return f"push[{self.category}] to {self.user} at {self.sent_at:%Y-%m-%d %H:%M}"


class LotObservation(models.Model):
    """One AR sighting of a lot label in a camera frame: a rolling solver buffer, pruned. Detections sharing
    (session_id, frame_id) constrain each other.
    """

    auction = models.ForeignKey(Auction, on_delete=models.CASCADE, related_name="ar_observations")
    lot = models.ForeignKey(Lot, on_delete=models.CASCADE, related_name="ar_observations")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    # A CharField, not UUIDField: MariaDB's native uuid rejects variant nibbles 0-7 (error 1292),
    # which killed half the app's session ids.
    session_id = models.CharField(max_length=36)
    frame_id = models.CharField(max_length=32)  # unique per camera frame within a session
    captured_at = models.DateTimeField()  # client clock, clamped to <= now on ingest
    created_at = models.DateTimeField(auto_now_add=True)
    bearing_deg = models.FloatField()  # horizontal angle in that frame's camera coords, +right
    depression_deg = models.FloatField()  # ray angle below horizontal (gravity-referenced), +down
    quality = models.FloatField(default=1.0)  # 0..1, detection sharpness
    fov_calibrated = models.BooleanField(default=False)  # bearings from device-reported FOV?
    # Cumulative gyro heading (deg, ccw, zero at session start). Null = no gyro data.
    yaw_deg = models.FloatField(null=True, blank=True)
    # Compass heading (deg CW from magnetic north, camera forward). Null = no reading. Fixes island
    # orientation; see ar_mapping.
    heading_deg = models.FloatField(null=True, blank=True)
    # GPS fix; used only for magnetic declination, never to place lots.
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    # Cumulative dead-reckoning displacement (m) in the yaw session frame (+x forward at yaw 0, +y
    # left). Null = no tracking. Translation odometry; see ar_mapping.
    odo_x_m = models.FloatField(null=True, blank=True)
    odo_y_m = models.FloatField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["auction", "captured_at"]),
            models.Index(fields=["session_id", "frame_id"]),
        ]

    def __str__(self):
        return f"obs lot={self.lot_id} @{self.bearing_deg:.1f}°/{self.depression_deg:.1f}°"


class LotPosition(models.Model):
    """A lot's solved 2D position in an auction-local frame (metres, stable between solves). Bearing
    accurate; scale is roughly ±30%.
    """

    lot = models.OneToOneField(Lot, on_delete=models.CASCADE, related_name="ar_position")
    auction = models.ForeignKey(Auction, on_delete=models.CASCADE, related_name="ar_positions")
    x = models.FloatField()
    y = models.FloatField()
    confidence = models.FloatField(default=0)  # 0..1
    observation_count = models.IntegerField(default=0)
    # Persistent island id; merged islands take the smaller id. The app treats other components as
    # unmapped.
    component = models.IntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"pos lot={self.lot_id} ({self.x:.2f}, {self.y:.2f})"


class CheckinNudge(models.Model):
    """Which proximity nudge was already issued to whom, so a dismissed sheet isn't shown again."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    auction = models.ForeignKey(Auction, on_delete=models.CASCADE)
    kind = models.CharField(max_length=20)  # join_offer | checked_in | set_location_offer
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "auction", "kind"], name="one_nudge_per_kind")]

    def __str__(self):
        return f"nudge {self.kind} user={self.user_id} auction={self.auction_id}"


class VolunteerJob(CachedPropertiesMixin, models.Model):
    """A job an auction admin needs help with, announced to app users. An optional bounty becomes an invoice
    discount; first come, first served.
    """

    auction = models.ForeignKey(Auction, on_delete=models.CASCADE, related_name="volunteer_jobs")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    description = models.CharField(max_length=200)  # "Job" on the form
    bounty = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    people_needed = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    canceled = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"volunteer job: {self.description}"

    @cached_property
    def signups_count(self):
        return self.signups.count()

    @property
    def is_full(self):
        return self.signups_count >= self.people_needed

    @property
    def spots_remaining(self):
        return max(0, self.people_needed - self.signups_count)


class VolunteerSignup(InvalidatesRelatedCache, models.Model):
    """One signup for a VolunteerJob, on AuctionTOS because the bounty is an invoice adjustment."""

    # The job caches signups_count.
    invalidates_cache_on = ("job",)

    job = models.ForeignKey(VolunteerJob, on_delete=models.CASCADE, related_name="signups")
    auctiontos = models.ForeignKey(AuctionTOS, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    invoice_adjustment = models.ForeignKey("InvoiceAdjustment", null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "auctiontos"], name="one_signup_per_job")]

    def __str__(self):
        return f"{self.auctiontos} signed up for {self.job}"


class LotQueueEntry(models.Model):
    """An in-person auction's ordered queue of lots about to be sold, built by scanning. Set winners pulls
    the head; watchers get "coming up" and "about to be sold" pushes (deduped per lot).
    """

    auction = models.ForeignKey(Auction, on_delete=models.CASCADE, related_name="lot_queue_entries")
    lot = models.OneToOneField(Lot, on_delete=models.CASCADE, related_name="queue_entry")
    order = models.PositiveIntegerField()
    added_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    createdon = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["auction", "order"]

    def __str__(self):
        return f"queue entry lot={self.lot_id} order={self.order}"


class VoiceGrammar(models.Model):
    """The grammar the app listens with on set winners: one row, site-wide, served in ``/api/mobile/config/``
    and merged over the app's defaults (:mod:`auctions.voice`).

    ``save()`` pins the pk (singleton). No row means app defaults. ``enabled=False`` hides the microphone.
    """

    SINGLETON_PK = 1

    enabled = models.BooleanField(default=True)
    enabled.help_text = "Uncheck to turn voice off everywhere; the app hides the microphone button."
    # The vocabulary-biased recognizer, falling back to the platform one where unavailable.
    backend = models.CharField(max_length=20, choices=voice.BACKEND_CHOICES, default=voice.BACKEND_BIASED)
    backend.help_text = "What the app should listen with, if it can. It reports what it actually managed."
    locale = models.CharField(max_length=20, default="en_US")
    prefer_on_device = models.BooleanField(default=True)
    prefer_on_device.help_text = "On-device recognition keeps working when the hall's wifi doesn't."

    # Merged over the app's defaults.
    anchors = models.JSONField(default=voice.default_anchors, blank=True)
    anchors.help_text = 'Slot name → the words that introduce it, e.g. {"lot": ["lot", "item"]}. Lowercase.'
    number_words = models.JSONField(default=voice.default_number_words, blank=True)
    number_words.help_text = 'Spoken number → digits, e.g. {"seventeen": 17}.'
    homophones = models.JSONField(default=voice.default_homophones, blank=True)
    homophones.help_text = 'Pairs that sound alike, e.g. [["15", "50"]]. The app offers both when it cannot tell.'
    weights = models.JSONField(default=voice.default_weights, blank=True)
    weights.help_text = "How much each signal counts toward confidence: asr, keyword, snap, agreement."
    thresholds = models.JSONField(default=voice.default_thresholds, blank=True)
    thresholds.help_text = "Score cutoffs: at/above 'confident' fills green, at/above 'unsure' asks, below is ignored."
    commit_after_ms = models.PositiveIntegerField(
        default=voice.DEFAULT_COMMIT_AFTER_MS, validators=[MaxValueValidator(2500)]
    )
    commit_after_ms.help_text = (
        "Milliseconds a heard lot, bidder or price must stop changing before the app fills the field. "
        "0 waits for the recognizer's final result instead -- slower by seconds, and the kill switch "
        "if early values misbehave. Under 200 the app raises it to 200."
    )

    auto_submit_on_sold = models.BooleanField(default=True)
    auto_submit_on_sold.help_text = "Saying 'sold' saves the lot, instead of only filling the fields."
    block_auto_submit_when_unsure = models.BooleanField(default=True)
    block_auto_submit_when_unsure.help_text = (
        "Refuse to save while any field is unsure. Turning this off sells lots to bidders nobody confirmed."
    )

    updatedon = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Voice grammar"
        verbose_name_plural = "Voice grammar"

    def __str__(self):
        return "Voice grammar" if self.enabled else "Voice grammar (disabled)"

    def save(self, *args, **kwargs):
        self.pk = self.SINGLETON_PK
        # Forced insert over the singleton would fail.
        kwargs.pop("force_insert", None)
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        """The configured grammar, or None when nobody has set one up."""
        return cls.objects.filter(pk=cls.SINGLETON_PK).first()


class VoiceCommandLog(models.Model):
    """One voice command the set-winners page acted on, and any operator correction: the tuning data.

    A blank ``slot`` is an utterance that matched nothing; group those by ``heard`` to find words to add
    to ``anchors``. Written by the page, session-authenticated, auction admins only.
    """

    auction = models.ForeignKey(Auction, on_delete=models.CASCADE)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    slot = models.CharField(max_length=20, choices=voice.SLOT_CHOICES, blank=True, default="")
    slot.help_text = "Which field was filled. Blank means nothing matched — those are the rows to mine for new words."
    heard = models.CharField(max_length=300, blank=True, default="")
    heard.help_text = "The recognizer's transcript of what was said."
    chosen = models.CharField(max_length=100, blank=True, default="")
    chosen.help_text = "The value the app matched it to and put in the field."
    confidence = models.FloatField(null=True, blank=True)
    corrected_to = models.CharField(max_length=100, blank=True, default="")
    corrected_to.help_text = "What the operator changed the field to before saving. Blank means the match stood."
    createdon = models.DateTimeField(auto_now_add=True)
    updatedon = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-createdon"]

    def __str__(self):
        if self.nothing_matched:
            return f"nothing matched: heard '{self.heard}'"
        result = f"{self.slot}: heard '{self.heard}' → {self.chosen}"
        if self.corrected_to:
            result += f" (corrected to {self.corrected_to})"
        return result

    @property
    def was_corrected(self):
        return bool(self.corrected_to)

    @property
    def nothing_matched(self):
        """True when no slot opened."""
        return not self.slot


#: How long a speaker counts as new.
NEW_SPEAKER_DAYS = 30


class SpeakerTopic(models.Model):
    """A shared talk topic. A canonical list from auctions/speaker_topics.py (the NEC export had three
    spellings of "cichlids"); nothing in the UI adds to it.
    """

    name = models.CharField(max_length=100, unique=True)
    slug = AutoSlugField(populate_from="name", unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return str(self.name)


class Speaker(CachedPropertiesMixin, CloudflareImageMixin, models.Model):
    """Someone who gives talks to aquarium clubs.

    Seeded from the NEC WordPress database; anyone with a permission in an NEC club can add more,
    including people without accounts. `nec_only` keeps a speaker to NEC clubs.
    """

    IMAGE_FIELD_NAME = "image"

    name = models.CharField(max_length=200, db_index=True)
    name.help_text = "The speaker's name, as you'd print it on a meeting flyer."
    slug = AutoSlugField(populate_from="name", unique=True)
    bio = models.TextField(blank=True, default="")
    bio.help_text = "A paragraph or two about the speaker."
    programs = models.TextField(blank=True, default="", verbose_name="Talks")
    programs.help_text = "The talks this speaker offers."
    # One photo: upload or URL. Uploads are validated in SpeakerForm.
    image = ThumbnailerImageField(
        upload_to="speakers/",
        blank=True,
        null=True,
        resize_source={"size": (600, 600), "quality": 85},
        verbose_name="Photo",
    )
    image.help_text = "Select a photo to upload"
    url = models.URLField(max_length=500, blank=True, null=True, verbose_name="Photo URL")
    url.help_text = "Or enter a URL to a photo instead of uploading one"
    topics = models.ManyToManyField(SpeakerTopic, blank=True, related_name="speakers")
    email = models.EmailField(max_length=255, blank=True, default="")
    email.help_text = "Only shown to people who can see the speaker directory."
    phone = models.CharField(max_length=30, blank=True, default="")
    website = models.URLField(max_length=500, blank=True, default="")
    facebook_page = models.URLField(max_length=500, blank=True, default="")

    location = models.CharField(max_length=500, blank=True, default="")
    location.help_text = "Roughly where the speaker travels from — a town and state is plenty."
    latitude = models.FloatField(blank=True, null=True, db_index=True)
    longitude = models.FloatField(blank=True, null=True, db_index=True)
    location_coordinates = PlainLocationField(based_fields=["location"], blank=True, null=True, verbose_name="Map")

    nec_only = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name="Only show to NEC member clubs",
        help_text=(
            "Keeps this speaker out of the directory for clubs that aren't NEC members.  "
            "Everything imported from the NEC speaker database is set this way."
        ),
    )
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="speaker_profiles",
        help_text=(
            "The site account this speaker is, when they have one.  Linked by email address on "
            "import.  Distinct from `created_by`, which is whoever typed the record in."
        ),
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="added_speakers"
    )
    created_by.help_text = "Blank for the rows imported from the NEC speaker database."
    club = models.ForeignKey(
        Club,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="added_speakers",
        help_text="The club the person adding this speaker was representing, if they picked one.",
    )
    topics_need_review = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name="Topics need review",
        help_text=(
            "Set when this speaker's topics landed on 'Other' because the topic they were "
            "filed under has been retired.  Filter on it in the admin to work through them; "
            "untick it once the topics are right."
        ),
    )
    topic_review_note = models.CharField(max_length=255, blank=True, default="")
    topic_review_note.help_text = "Which retired topic this speaker was on, so the fix doesn't need guesswork."
    imported_from_nec = models.BooleanField(default=False, editable=False, db_index=True)
    source_url = models.URLField(max_length=500, blank=True, default="", editable=False)
    source_url.help_text = "Where this record came from on the old NEC website."
    wordpress_post_id = models.PositiveIntegerField(
        null=True,
        blank=True,
        editable=False,
        unique=True,
        help_text="WordPress post id, so re-running the import updates rows instead of duplicating them.",
    )
    is_deleted = models.BooleanField(default=False, db_index=True)
    createdon = models.DateTimeField(auto_now_add=True)
    lastmodified = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return str(self.name)

    def get_absolute_url(self):
        return reverse("speaker_detail", kwargs={"slug": self.slug})

    @property
    def display_url(self):
        """Full-size photo: the upload, else the pasted URL."""
        return cloudflare_images.image_url(self.image, self.cloudflare_image_id) or self.url or None

    @property
    def thumbnail_url(self):
        """Small square photo for the list, map and panel."""
        return cloudflare_images.image_url(self.image, self.cloudflare_image_id, "speaker") or self.url or None

    @property
    def has_coordinates(self):
        return self.latitude is not None and self.longitude is not None

    @property
    def is_recently_added(self):
        """Whether to show "New": added in the last NEW_SPEAKER_DAYS, excluding the NEC import (one batch would
        badge everyone).
        """
        if not self.createdon or self.imported_from_nec:
            return False
        return self.createdon >= timezone.now() - datetime.timedelta(days=NEW_SPEAKER_DAYS)

    @property
    def attribution(self):
        """Where this record came from; imported rows have no `created_by`."""
        if not self.created_by:
            return "Added from the NEC speaker database"
        name = self.created_by.get_full_name() or self.created_by.username
        if self.club:
            return f"Added by {name} ({self.club.name})"
        return f"Added by {name}"

    @cached_property
    def display_name(self):
        """ "Last, First" as "First Last"."""
        if self.name.count(",") == 1:
            last, first = (part.strip() for part in self.name.split(","))
            if last and first:
                return f"{first} {last}"
        return self.name

    def tag_counts(self):
        """[(value, label, group, count)] for tags with at least one vote."""
        counts = dict(
            SpeakerTag.objects.filter(speaker=self)
            .values_list("tag")
            .annotate(total=Count("pk"))
            .values_list("tag", "total")
        )
        result = []
        for value, label, group in SpeakerTag.TAG_DEFINITIONS:
            if counts.get(value):
                result.append((value, label, group, counts[value]))
        result.sort(key=lambda row: -row[3])
        return result

    def tags_by_user(self, user):
        """Tag values this user already applied."""
        if not user or not user.is_authenticated:
            return set()
        return set(SpeakerTag.objects.filter(speaker=self, user=user).values_list("tag", flat=True))

    def can_be_deleted_by(self, user):
        """Only the creator or a superuser may delete; imported rows only a superuser."""
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return bool(self.created_by_id and self.created_by_id == user.pk)


class SpeakerTag(models.Model):
    """One user's vote that a tag applies to a speaker. Fixed choices, not rows."""

    GROUP_TALK = "How the talk went"
    GROUP_LOGISTICS = "Logistics"

    #: (value, label, group), in render order.
    TAG_DEFINITIONS = (
        ("engaging", "Engaging presenter", GROUP_TALK),
        ("visuals", "Great photos / visuals", GROUP_TALK),
        ("funny", "Funny", GROUP_TALK),
        ("beginners", "Good for beginners", GROUP_TALK),
        ("technical", "In-depth / technical", GROUP_TALK),
        ("workshop", "Hands-on workshop", GROUP_TALK),
        ("big_crowd", "Drew a big crowd", GROUP_TALK),
        ("book_again", "Would book again", GROUP_TALK),
        ("remote", "Presents remotely", GROUP_LOGISTICS),
        ("travels", "Willing to travel", GROUP_LOGISTICS),
        ("brings_items", "Brings items for the auction", GROUP_LOGISTICS),
        # Last: the one warning tag.
        ("no_longer_speaking", "No longer speaking", GROUP_LOGISTICS),
    )
    TAG_CHOICES = tuple((value, label) for value, label, _group in TAG_DEFINITIONS)
    TAG_LABELS = dict(TAG_CHOICES)

    speaker = models.ForeignKey(Speaker, on_delete=models.CASCADE, related_name="tags")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="speaker_tags")
    tag = models.CharField(max_length=30, choices=TAG_CHOICES, db_index=True)
    createdon = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["speaker", "user", "tag"], name="unique_speaker_tag_per_user")]
        ordering = ["tag"]

    def __str__(self):
        return f"{self.speaker}: {self.get_tag_display()}"

    @classmethod
    def grouped_definitions(cls):
        """[(group_name, [(value, label)])] in TAG_DEFINITIONS order."""
        groups = {}
        for value, label, group in cls.TAG_DEFINITIONS:
            groups.setdefault(group, []).append((value, label))
        return list(groups.items())


class SpeakerComment(models.Model):
    """A note one club left about a speaker, shown on the speaker's panel."""

    speaker = models.ForeignKey(Speaker, on_delete=models.CASCADE, related_name="comments")
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="speaker_comments")
    club = models.ForeignKey(Club, on_delete=models.SET_NULL, null=True, blank=True, related_name="speaker_comments")
    body = models.TextField(max_length=2000)
    createdon = models.DateTimeField(auto_now_add=True)
    is_deleted = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-createdon"]

    def __str__(self):
        return f"{self.user} on {self.speaker}"

    @property
    def author_display(self):
        """Just the person."""
        if not self.user:
            return "Deleted user"
        return self.user.get_full_name() or self.user.username

    def can_be_deleted_by(self, user):
        if not user or not user.is_authenticated:
            return False
        return bool(user.is_superuser or (self.user_id and self.user_id == user.pk))


class AssistantSkillRequest(CachedPropertiesMixin, models.Model):
    """Something an agent tried to do and couldn't, in its own words.

    Written by ``request_a_skill``, read on ``/admin-dashboard/assistant-requests/``. Duplicates are
    evidence and are counted. Content is model-written: displayed escaped, never executed or matched.
    """

    STATUS_NEW = "new"
    STATUS_PLANNED = "planned"
    STATUS_DONE = "done"
    STATUS_DECLINED = "declined"
    STATUS_CHOICES = (
        (STATUS_NEW, "New"),
        (STATUS_PLANNED, "Planned"),
        (STATUS_DONE, "Built"),
        (STATUS_DECLINED, "Not doing"),
    )

    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    skill = models.CharField(max_length=100, db_index=True)
    skill.help_text = "What the tool would be called, in the caller's words."
    params = models.TextField(blank=True, default="")
    params.help_text = "What it would need to be told."
    reason = models.TextField(blank=True, default="")
    reason.help_text = "What the user was actually trying to do, and what happened instead."
    surface = models.CharField(max_length=100, blank=True, default="")
    surface.help_text = "Which assistant asked: the OAuth application's name, the API key's, or the command palette."
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_NEW, db_index=True)
    notes = models.TextField(blank=True, default="")
    notes.help_text = "Site admin's note. Not shown to the person who asked."
    createdon = models.DateTimeField(auto_now_add=True, db_index=True)
    updatedon = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-createdon"]
        verbose_name = "Assistant skill request"

    def __str__(self):
        return f"{self.skill} ({self.get_status_display()})"

    @cached_property
    def others_asking(self):
        """Other people asking for the same skill name (case-insensitive name only)."""
        return (
            AssistantSkillRequest.objects.filter(skill__iexact=self.skill.strip())
            .exclude(pk=self.pk)
            .values("user_id")
            .distinct()
            .count()
        )


class SignInStitch(models.Model):
    """The anonymous session a person held when they signed in.

    PageView splits signed-in and anonymous views, so one person's visit is two actors. This session key
    bridges that; not a fingerprint (IP and UA merges fail on shared venue wifi). **Forwards only**:
    compare across :func:`auctions.lifecycle.stitching_began` with care. The key comes from
    ``request.COOKIES``, since ``login()`` cycles the session key before the signal.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sign_in_stitches")
    session_id = models.CharField(max_length=600, db_index=True)
    session_id.help_text = "The session key the browser was holding before this sign-in."
    createdon = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        # One row per user and session.
        constraints = [models.UniqueConstraint(fields=["user", "session_id"], name="one_stitch_per_user_session")]
        verbose_name = "Sign-in stitch"
        verbose_name_plural = "Sign-in stitches"

    def __str__(self):
        return f"{self.user} signed in holding {self.session_id[:12]}"
