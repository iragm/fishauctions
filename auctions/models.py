import datetime
import logging
import re
import uuid
from datetime import time
from decimal import ROUND_HALF_UP, Decimal
from random import randint
from urllib.parse import quote_plus

import channels.layers
from asgiref.sync import async_to_sync
from autoslug import AutoSlugField
from bs4 import BeautifulSoup
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import (
    BooleanField,
    Case,
    Count,
    DecimalField,
    Exists,
    ExpressionWrapper,
    F,
    FloatField,
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
from django.urls import reverse
from django.utils import html, timezone
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

from .helper_functions import bin_data, get_currency_symbol

logger = logging.getLogger(__name__)


def nearby_auctions(
    latitude,
    longitude,
    distance=100,
    include_already_joined=False,
    user=None,
    return_slugs=False,
):
    """Return a list of auctions or auction slugs that are within a specified distance of the given location"""
    auctions = []
    slugs = []
    distances = []
    locations = (
        PickupLocation.objects.annotate(distance=distance_to(latitude, longitude))
        .exclude(distance__gt=distance)
        .filter(
            auction__date_end__gte=timezone.now(),
            auction__date_start__lte=timezone.now(),
        )
        .exclude(auction__promote_this_auction=False)
        .exclude(auction__isnull=True)
    )
    if user:
        if user.is_authenticated and not include_already_joined:
            locations = locations.exclude(auction__auctiontos__user=user)
        locations = locations.exclude(auction__auctionignore__user=user)
    for location in locations:
        if location.auction.slug not in slugs:
            auctions.append(location.auction)
            slugs.append(location.auction.slug)
            distances.append(location.distance)
    if return_slugs:
        return slugs
    else:
        return auctions, distances


def median_value(queryset, term):
    count = queryset.count()
    return queryset.values_list(term, flat=True).order_by(term)[int(round(count / 2))]


def add_price_info(qs):
    """Add fields `pre_register_discount`, `your_cut` and `club_cut` to a given Lot queryset."""
    if not (isinstance(qs, QuerySet) and qs.model == Lot):
        msg = "must be passed a queryset of the Lot model"
        raise TypeError(msg)
    return qs.annotate(
        pre_register_discount=Case(
            When(auctiontos_seller__isnull=True, then=Value(0.0)),
            When(
                added_by=F("user"),
                then=F("auctiontos_seller__auction__pre_register_lot_discount_percent"),
            ),
            default=Value(0.0),
            output_field=FloatField(),
        ),
        your_cut=ExpressionWrapper(
            Case(
                When(
                    Q(auctiontos_seller__isnull=True, winning_price__isnull=False),
                    then=F("winning_price"),
                ),
                When(
                    Q(auctiontos_seller__isnull=True, winning_price__isnull=True),
                    then=Value(0.0),
                ),
                When(donation=True, then=Value(0.0)),
                When(
                    Q(winning_price__isnull=False, active=False),
                    then=(
                        (
                            F("winning_price")
                            * Case(
                                When(
                                    auctiontos_seller__is_club_member=True,
                                    then=(
                                        (
                                            100
                                            - F(
                                                "auctiontos_seller__auction__winning_bid_percent_to_club_for_club_members"
                                            )
                                            + F("pre_register_discount")
                                        )
                                        / 100
                                    ),
                                ),
                                default=(
                                    (
                                        100
                                        - F("auctiontos_seller__auction__winning_bid_percent_to_club")
                                        + F("pre_register_discount")
                                    )
                                    / 100
                                ),
                                output_field=FloatField(),
                            )
                        )
                        - Case(
                            When(
                                auctiontos_seller__is_club_member=True,
                                then=F("auction__lot_entry_fee_for_club_members"),
                            ),
                            default=F("auction__lot_entry_fee"),
                            output_field=FloatField(),
                        )
                    )
                    * (100 - F("partial_refund_percent"))
                    / 100,
                ),
                When(
                    Q(winning_price__isnull=True, active=False),
                    then=Case(
                        When(donation=True, then=Value(0.0)),
                        default=Value(0.0) - F("auctiontos_seller__auction__unsold_lot_fee"),
                    ),
                ),
                default=Value(0.0),
                output_field=FloatField(),
            ),
            output_field=FloatField(),
        ),
        club_cut=ExpressionWrapper(
            Case(
                When(Q(active=False, winning_price__isnull=True), then=Value(0.0)),
                When(winning_price__isnull=True, then=Value(0.0)),
                default=(F("winning_price") * (100 - F("partial_refund_percent")) / 100) - F("your_cut"),
            ),
            output_field=FloatField(),
        ),
    )


def find_image(name, user, auction):
    """Find an image from the most recent lot with a given name"""
    qs = LotImage.objects.filter(
        (Q(lot_number__user__userdata__share_lot_images=True) | Q(lot_number__user__isnull=True)),
        lot_number__lot_name=name,
        lot_number__is_deleted=False,
        lot_number__banned=False,
        is_primary=True,
        lot_number__auction__created_by__pk__in=auction.auction_admins_pks,
    ).order_by("-lot_number__date_posted")
    if user:
        image_from_user = qs.filter(lot_number__user=user).first()
        if image_from_user:
            return image_from_user
    return qs.first()


def distance_to(
    latitude,
    longitude,
    unit="miles",
    lat_field_name="latitude",
    lng_field_name="longitude",
    approximate_distance_to=10,
):
    """
    GeoDjango has been fustrating with MySQL and Point objects.
    This function is a workaound done using raw SQL.

    Given a latitude and longitude, it will return raw SQL that can be used to annotate a queryset

    The model being annotated must have fields named 'latitude' and 'longitude' for this to work

    For example:

    qs = model.objects.all()\
            .annotate(distance=distance_to(latitude, longitude))\
            .order_by('distance')
    """
    if unit == "miles":
        correction = 0.6213712  # close enough
    else:
        correction = 1  # km
    for i in [
        latitude,
        longitude,
        lat_field_name,
        lng_field_name,
        approximate_distance_to,
    ]:
        if '"' in str(i) or "'" in str(i):
            msg = "invalid character passed to distance_to, possible sql injection risk"
            raise TypeError(msg)
    # Great circle distance formula, CEILING is used to keep people from triangulating locations
    gcd_formula = f"CEILING( 6371 * acos(least(greatest( \
        cos(radians({latitude})) * cos(radians({lat_field_name})) \
        * cos(radians({lng_field_name}) - radians({longitude})) + \
        sin(radians({latitude})) * sin(radians({lat_field_name})) \
        , -1), 1)) * {correction} / {approximate_distance_to}) * {approximate_distance_to}"
    distance_raw_sql = RawSQL(gcd_formula, ())
    # This one works fine when I print qs.query and run the output in SQL but does not work when Django runs the qs
    # Seems to be an issue with annotating on related entities
    # Injection attacks don't seem possible here because latitude and longitude can only contain a float as set in update_user_location()
    # gcd_formula = f"CEILING( 6371 * acos(least(greatest( \
    #     cos(radians(%s)) * cos(radians({lat_field_name})) \
    #     * cos(radians({lng_field_name}) - radians(%s)) + \
    #     sin(radians(%s)) * sin(radians({lat_field_name})) \
    #     , -1), 1)) * %s / {approximate_distance_to}) * {approximate_distance_to}"
    # distance_raw_sql = RawSQL(
    #     gcd_formula,
    #     (latitude, longitude, latitude, correction)
    # )
    return distance_raw_sql


def add_tos_info(qs):
    """Add fields to a given AuctionTOS queryset."""
    if not (isinstance(qs, QuerySet) and qs.model == AuctionTOS):
        msg = "must be passed a queryset of the AuctionTOS model"
        raise TypeError(msg)

    # Add has_ever_granted_permission annotation if not already present
    # This checks if the user has ever joined an auction (manually_added=False)
    # for the same auction creator
    qs = qs.annotate(
        has_ever_granted_permission=Case(
            When(
                Q(user__isnull=False)
                & Exists(
                    AuctionTOS.objects.filter(
                        user=OuterRef("user"), auction__created_by=OuterRef("auction__created_by"), manually_added=False
                    )
                ),
                then=Value(True),
            ),
            default=Value(False),
            output_field=BooleanField(),
        )
    )

    return qs.annotate(
        lots_bid_actual=Coalesce(
            Subquery(
                Bid.objects.exclude(is_deleted=True)
                .filter(user=OuterRef("user"), lot_number__auction=OuterRef("auction"))
                .values("user")
                .annotate(count=Count("pk", distinct=True))
                .values("count"),
                output_field=IntegerField(),
            ),
            0,
        ),
        lots_bid=Case(When(Q(has_ever_granted_permission=False), then=Value(0)), default=F("lots_bid_actual")),
        lots_viewed_actual=Coalesce(
            Subquery(
                PageView.objects.filter(user=OuterRef("user"), lot_number__auction=OuterRef("auction"))
                .values("user")
                .annotate(count=Count("lot_number", distinct=True))
                .values("count"),
                output_field=IntegerField(),
            ),
            0,
        ),
        lots_viewed=Case(When(Q(has_ever_granted_permission=False), then=Value(0)), default=F("lots_viewed_actual")),
        lots_won=Count("auctiontos_winner", distinct=True),
        lots_submitted=Count("auctiontos_seller", distinct=True),
        other_auctions=Coalesce(
            Subquery(
                AuctionTOS.objects.filter(email=OuterRef("email"))
                .exclude(id=OuterRef("id"))
                .values("email")
                .annotate(count=Count("*"))
                .values("count"),
                output_field=IntegerField(),
            ),
            0,
        ),
        lots_outbid=Case(
            When(lots_won__gt=F("lots_bid"), then=0),
            default=F("lots_bid") - F("lots_won"),
            output_field=IntegerField(),
        ),
        account_age_ms=Case(
            When(
                Q(has_ever_granted_permission=False),
                then=ExpressionWrapper(timezone.now() - F("createdon"), output_field=IntegerField()),
            ),
            default=ExpressionWrapper(timezone.now() - F("user__date_joined"), output_field=IntegerField()),
        ),
        account_age_days=ExpressionWrapper(F("account_age_ms") / 86400000000, output_field=IntegerField()),
        other_user_bans_actual=Coalesce(
            Subquery(
                UserBan.objects.filter(banned_user=OuterRef("user"))
                .values("pk")
                .annotate(count=Count("*"))
                .values("count"),
                output_field=IntegerField(),
            ),
            0,
        ),
        other_user_bans=Case(
            When(Q(has_ever_granted_permission=False), then=Value(0)),
            default=F("other_user_bans_actual"),
        ),
        trust=ExpressionWrapper(
            1 * F("lots_bid")
            + 0.2 * F("lots_viewed")
            + 2 * F("lots_won")
            + 2 * F("lots_submitted")
            + 5 * F("other_auctions")
            - 2 * F("lots_outbid")
            + 0.01 * F("account_age_days")
            - 100 * F("other_user_bans"),
            output_field=IntegerField(),
        ),
    )


def add_tos_distance_info(qs):
    """Add a distance_traveled to an auctiontos query"""
    if not (isinstance(qs, QuerySet) and qs.model == AuctionTOS):
        msg = "must be passed a queryset of the AuctionTOS model"
        raise TypeError(msg)

    # Add has_ever_granted_permission annotation if not already present
    qs = qs.annotate(
        has_ever_granted_permission=Case(
            When(
                Q(user__isnull=False)
                & Exists(
                    AuctionTOS.objects.filter(
                        user=OuterRef("user"), auction__created_by=OuterRef("auction__created_by"), manually_added=False
                    )
                ),
                then=Value(True),
            ),
            default=Value(False),
            output_field=BooleanField(),
        )
    )

    return (
        qs.select_related("user__userdata")
        .select_related("pickup_location")
        .annotate(
            new_distance_traveled=Case(
                When(Q(has_ever_granted_permission=False), then=Value(-1)),
                default=distance_to(
                    """`auctions_userdata`.`latitude`""",
                    """`auctions_userdata`.`longitude`""",
                    lat_field_name="""`auctions_pickuplocation`.`latitude`""",
                    lng_field_name="""`auctions_pickuplocation`.`longitude`""",
                    approximate_distance_to=1,
                ),
                output_field=IntegerField(),
            ),
        )
    )


def guess_category(text):
    """Given some text, look up lots with similar names and make a guess at the category this `text` belongs to based on the category used there"""
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

    # category = lot_qs.values('species_category').annotate(count=Count('species_category')).order_by('-count').first()
    # attempting this as a single-shot query is extremely difficult to debug
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


def remove_html_color_tags(text):
    """Remove only color-related styles from HTML, preserving all other attributes"""
    if not text:
        return text

    soup = BeautifulSoup(text, "html.parser")

    # Remove 'color' attribute from <font> tags
    for tag in soup.find_all("font"):
        if tag.has_attr("color"):
            del tag["color"]

    # Clean color and background-color from style attributes in <span> and others
    for tag in soup.find_all(style=True):
        # Split and filter styles
        styles = tag["style"].split(";")
        cleaned_styles = []
        for style in styles:
            if not style.strip():
                continue
            name, *_ = style.split(":", 1)
            if name.strip().lower() not in {"color", "background-color"}:
                cleaned_styles.append(style)
        if cleaned_styles:
            tag["style"] = ";".join(cleaned_styles)
        else:
            del tag["style"]

    return str(soup)


class BlogPost(models.Model):
    """
    A simple markdown blog.  At the moment, I don't feel that adding a full CMS is necessary
    """

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
    """
    Allows users to specify a region -- USA, Canada, South America, etc.
    """

    name = models.CharField(max_length=255)

    def __str__(self):
        return str(self.name)


class GeneralInterest(models.Model):
    """Clubs and products belong to a general interest"""

    name = models.CharField(max_length=255)

    def __str__(self):
        return str(self.name)


class Club(models.Model):
    """Users can self-select which club they belong to"""

    name = models.CharField(max_length=255, db_index=True)
    abbreviation = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    homepage = models.CharField(max_length=255, blank=True, null=True)
    facebook_page = models.CharField(max_length=255, blank=True, null=True)
    contact_email = models.CharField(max_length=255, blank=True, null=True)
    date_contacted = models.DateTimeField(blank=True, null=True)
    date_contacted_for_in_person_auctions = models.DateTimeField(blank=True, null=True)
    notes = models.CharField(max_length=300, blank=True, null=True)
    notes.help_text = "Only visible in the admin site, never made public"
    interests = models.ManyToManyField(GeneralInterest, blank=True)
    active = models.BooleanField(default=True)
    latitude = models.FloatField(blank=True, null=True)
    longitude = models.FloatField(blank=True, null=True)
    location = models.CharField(max_length=500, blank=True, null=True)
    location.help_text = "Search Google maps with this address"
    location_coordinates = PlainLocationField(based_fields=["location"], blank=True, null=True, verbose_name="Map")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return str(self.name)


class Category(models.Model):
    """Picklist of species.  Used for product, lot, and interest"""

    name = models.CharField(max_length=255)
    name_on_label = models.CharField(max_length=255, default="")

    def __str__(self):
        return str(self.name)

    class Meta:
        verbose_name_plural = "Categories"
        ordering = ["name"]


class Product(models.Model):
    """A species or item in the auction
    This is not really used much anymore,
    it was intended to exist for every possible species of fish, but that's just too much for the scope of this project"""

    common_name = models.CharField(max_length=255)
    common_name.help_text = "The name usually used to describe this species"
    scientific_name = models.CharField(max_length=255, blank=True)
    scientific_name.help_text = "Latin name used to describe this species"
    breeder_points = models.BooleanField(default=True)
    category = models.ForeignKey(Category, null=True, on_delete=models.SET_NULL)

    def __str__(self):
        return f"{self.common_name} ({self.scientific_name})"

    class Meta:
        verbose_name_plural = "Products and species"


class Auction(models.Model):
    """An auction is a collection of lots"""

    title = models.CharField("Auction name", max_length=255, blank=False, null=False)
    title.help_text = "This is the name people will see when joining your auction"
    slug = AutoSlugField(populate_from="title", unique=True)
    is_online = models.BooleanField(default=True)
    is_online.help_text = "Is this is an online auction with in-person pickup at one or more locations?"
    sealed_bid = models.BooleanField(default=False)
    sealed_bid.help_text = "Users won't be able to see what the current bid is"
    lot_entry_fee = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(10)])
    lot_entry_fee.help_text = "The amount the seller will be charged if a lot sells"
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
    invoiced = models.BooleanField(default=False)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    location = models.CharField(max_length=300, null=True, blank=True)
    location.help_text = "State or region of this auction"
    summernote_description = models.TextField(verbose_name="Rules", default="", blank=True)
    # notes = MarkdownField(
    #     rendered_field="notes_rendered",
    #     validator=VALIDATOR_STANDARD,
    #     blank=True,
    #     null=True,
    #     verbose_name="Rules",
    #     default="",
    # )
    # notes.help_text = "To add a link: [Link text](https://www.google.com)"
    # notes_rendered = RenderedMarkdownField(blank=True, null=True)
    code_to_add_lots = models.CharField(max_length=255, blank=True, null=True)
    code_to_add_lots.help_text = (
        "This is like a password: People in your club will enter this code to put their lots in this auction"
    )
    lot_promotion_cost = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    first_bid_payout = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)])
    first_bid_payout.help_text = "This is a feature to encourage bidding.  Give each bidder this amount, for free.  <a href='/blog/encouraging-participation/' target='_blank'>More information</a>"
    promote_this_auction = models.BooleanField(default=True)
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
    make_stats_public = models.BooleanField(default=True)
    make_stats_public.help_text = "Allow any user who has a link to this auction's stats to see them.  Uncheck to only allow the auction creator to view stats"
    bump_cost = models.PositiveIntegerField(blank=True, default=1, validators=[MinValueValidator(1)])
    bump_cost.help_text = "The amount a user will be charged each time they move a lot to the top of the list"
    use_categories = models.BooleanField(default=True, verbose_name="Use category field")
    use_categories.help_text = "Not shown on the bulk add lots form.  Check to use categories like Cichlids, Livebearers, etc.  This option is required if you want to promote your auction on the main auctions list."
    is_deleted = models.BooleanField(default=False)
    ONLINE_BIDDING_OPTIONS = (
        ("allow", "Allow buy now and bidding"),
        ("buy_now_only", "Allow buy now only"),
        ("disable", "No online bidding"),
    )
    online_bidding = models.CharField(max_length=20, choices=ONLINE_BIDDING_OPTIONS, blank=False, default="allow")
    # allow_bidding_on_lots = models.BooleanField(default=True, verbose_name="Allow online bidding")
    only_approved_sellers = models.BooleanField(default=False)
    only_approved_sellers.help_text = "Require admin approval before users can add lots.  This will not change permissions for users that have already joined."
    only_approved_bidders = models.BooleanField(default=False)
    only_approved_bidders.help_text = "Require admin approval before users can bid.  This only applies to new users: Users that you manually add and users who have a paid invoice in a past auctions will be allowed to bid."
    require_phone_number = models.BooleanField(default=False)
    require_phone_number.help_text = "Require users to have entered a phone number before they can join this auction"
    email_users_when_invoices_ready = models.BooleanField(default=True)
    invoice_payment_instructions = models.CharField(max_length=255, blank=True, null=True, default="")
    invoice_payment_instructions.help_text = "Shown to the user on their invoice.  For example, 'You will receive a seperate PayPal invoice with payment instructions'"
    invoice_rounding = models.BooleanField(default=True)
    invoice_rounding.help_text = (
        "Round invoice totals to whole dollar amounts.  Check if you plan to accept cash payments."
    )
    minimum_bid = models.PositiveIntegerField(default=2, validators=[MinValueValidator(1)])
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
    tax.help_text = "Added to invoices for all won lots"
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
    use_custom_checkbox_field = models.BooleanField(default=False, blank=True)
    use_custom_checkbox_field.help_text = "Optional information such as CARES, native species, difficult to keep, etc."
    CUSTOM_CHOICES = (
        ("disable", "Off"),
        ("allow", "Optional"),
        ("required", "Required for all lots"),
    )
    custom_field_1 = models.CharField(
        max_length=20,
        choices=CUSTOM_CHOICES,
        default="disable",
        verbose_name="Custom field for lots",
    )
    custom_field_1.help_text = (
        "Additional information on the label such as notes, scientific name, collection location..."
    )
    custom_field_1_name = models.CharField(
        max_length=50, default="Notes", blank=True, null=True, verbose_name="Custom field name"
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
        default="qr_code,lot_name,min_bid_label,buy_now_label,quantity_label,seller_name,donation_label,custom_field_1,i_bred_this_fish_label,custom_checkbox_label",
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
        """
        Pre-encoded subject/body for the 'request promoted auction access' mailto link.
        Uses SITE_DOMAIN to build an absolute URL to this auction.
        """
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

    @property
    def paypal_information(self):
        """
        Return the merchant ID for PayPal payments
        Fallback for admin users to use the site-wide api keys
        """

        seller = PayPalSeller.objects.filter(user=self.created_by).first()
        if seller:
            return seller.paypal_merchant_id
        if self.created_by.is_superuser:
            return "admin"
        return None

    @property
    def square_information(self):
        """
        Return the merchant ID for Square payments
        Only returns ID if seller has linked their Square account via OAuth
        """
        from auctions.models import SquareSeller

        seller = SquareSeller.objects.filter(user=self.created_by).first()
        if seller:
            return seller.square_merchant_id
        return None

    @property
    def show_paypal_banner(self):
        """Can we show the link your PayPal account banner?
        One more check is needed on the template:
        this banner should only be shown to the auction creator"""
        if self.dismissed_paypal_banner:
            return False
        if not self.created_by.userdata.paypal_enabled:
            return False
        if self.created_by.is_superuser:
            return False
        if self.created_by.userdata.never_show_paypal_connect:
            return False
        # if self.enable_online_payments:
        #    return False
        if PayPalSeller.objects.filter(user=self.created_by).first():
            return False
        return True

    @property
    def show_square_banner(self):
        """Can we show the link your Square account banner?
        One more check is needed on the template:
        this banner should only be shown to the auction creator"""
        from auctions.models import SquareSeller

        if self.dismissed_square_banner:
            return False
        if not self.created_by.userdata.square_enabled:
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
        """Perform a soft delete by setting is_deleted=True.

        Note: This is a soft delete implementation that marks the auction as deleted
        without removing it from the database. Related objects (lots, bids, etc.) will
        still exist and may appear in queries unless they explicitly filter out deleted
        auctions using: .exclude(auction__is_deleted=True)

        This allows for data retention and potential recovery while hiding the auction
        from normal user operations.
        """
        self.is_deleted = True
        self.save()

    def fix_year(self, date_field, low_cutoff=2000, high_cutoff=2050):
        """If the year is a long time ago or in the future, assume they meant this year"""
        if date_field and (date_field.year < low_cutoff or date_field.year > high_cutoff):
            self.create_history("RULES", f"Changed invalid date {date_field.year} to current year")
            current_year = timezone.now().year
            return date_field.replace(year=current_year)
        return date_field

    def save(self, *args, **kwargs):
        self.date_start = self.fix_year(self.date_start)
        self.lot_submission_start_date = self.fix_year(self.lot_submission_start_date)
        self.lot_submission_end_date = self.fix_year(self.lot_submission_end_date)
        self.date_end = self.fix_year(self.date_end)
        self.date_online_bidding_starts = self.fix_year(self.date_online_bidding_starts)
        self.date_online_bidding_ends = self.fix_year(self.date_online_bidding_ends)
        # if self.date_start.year < 2000:
        #    current_year = timezone.now().year
        #    self.date_start = self.date_start.replace(year=current_year)
        self.summernote_description = remove_html_color_tags(self.summernote_description)
        super().save(*args, **kwargs)

    def find_user(self, name="", email="", exclude_pk=None):
        """Used for duplicate checks and when adding users to an auction
        Returns an AuctionTOS instance or None"""
        qs = AuctionTOS.objects.filter(auction__pk=self.pk)
        if not name and not email:
            return None
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        if email:
            email_search = qs.filter(email=email).first()
            if email_search:
                return email_search
        if name:
            from .filters import AuctionTOSFilter

            name_search = AuctionTOSFilter.generic(self, qs, name, match_names_only=True).first()
            if name_search:
                return name_search
        return None

    @property
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

    @property
    def physical_location_qs(self):
        """Find all non-default locations"""
        # I am not sure why we were excluding the default location, but it doesn't make sense to
        # return self.location_qs.exclude(Q(pickup_by_mail=True)|Q(is_default=True))
        return self.location_qs.exclude(pickup_by_mail=True)

    @property
    def location_with_location_qs(self):
        """Find all locations that have coordinates - useful to see if there's an actual location associated with this auction.  By default, auctions get a location with no coordinates added"""
        return self.physical_location_qs.exclude(latitude=0, longitude=0)

    @property
    def number_of_locations(self):
        """The number of physical locations this auction has"""
        return self.physical_location_qs.count()

    @property
    def all_location_count(self):
        """All locations, even mail"""
        return self.location_qs.count()

    @property
    def allow_mailing_lots(self):
        if self.location_qs.filter(pickup_by_mail=True).first():
            return True
        return False

    @property
    def auction_type(self):
        """Returns whether this is an online, in-person, or hybrid auction for use in tooltips and templates.  See also auction_type_as_str for a friendly display version"""
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

    @property
    def auction_type_as_str(self):
        """Returns friendly string of whether this is an online, in-person, or hybrid auction"""
        auction_type = self.auction_type
        if auction_type == "online_one_location":
            return "online auction with in-person pickup"
        if auction_type == "online_multi_location":
            return "online auction with in-person pickup at multiple locations"
        if auction_type == "online_no_location":
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
        """only for templates, winning_bid_percent_to_club - pre_register_lot_discount_percent"""
        return self.winning_bid_percent_to_club - self.pre_register_lot_discount_percent

    def get_absolute_url(self):
        return self.url

    def get_edit_url(self):
        return f"/auctions/{self.slug}/edit/"

    @property
    def url(self):
        return f"/auctions/{self.slug}/"

    @property
    def label_print_link(self):
        return f"{self.get_absolute_url()}?printredirect={reverse('print_my_labels', kwargs={'slug': self.slug})}"

    @property
    def label_print_unprinted_link(self):
        return f"{self.get_absolute_url()}?printredirect={reverse('print_my_unprinted_labels', kwargs={'slug': self.slug})}"

    @property
    def add_lot_link(self):
        return f"/lots/new/?auction={self.slug}"

    @property
    def view_lot_link(self):
        return f"/lots/?auction={self.slug}&status=all"

    @property
    def user_admin_link(self):
        return reverse("auction_tos_list", kwargs={"slug": self.slug})

    @property
    def set_lot_winners_link(self):
        # return f"{self.get_absolute_url()}lots/set-winners/{self.set_lot_winners_url}"
        return f"{self.get_absolute_url()}lots/set-winners/"

    def permission_check(self, user):
        """See if `user` can make changes to this auction"""
        if self.created_by == user:
            return True
        if user.is_superuser:
            return True
        if not user.is_authenticated:
            return False
        tos = AuctionTOS.objects.filter(is_admin=True, user=user, user__isnull=False, auction=self.pk).first()
        if tos:
            return True
        return False

    @property
    def pickup_locations_before_end(self):
        """If there's a problem with the pickup location times, all of them need to be after the end date of the auction (or after the start date for an in-person auction).
        Returns the edit url for the first pickup location whose end time is before the auction end"""
        locations = self.location_qs
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
        """Check if auction start or end times are not set to logical times (ending in :00:00 or :30:00).
        Returns the edit url if times are illogical, False otherwise."""
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

    @property
    def date_end_as_str(self):
        """Human-reable end date of the auction; this will always be an empty string for in-person auctions"""
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

    @property
    def closed(self):
        """For display on the main auctions list"""
        if self.is_online and self.date_end:
            if timezone.now() > self.dynamic_end:
                return True
        # in-person auctions don't end right now
        return False

    @property
    def in_person_closed(self):
        """Maybe we can combine this with the above `closed`, but I'm not sure where else that is used"""
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

    @property
    def ended_badge(self):
        if self.closed or self.in_person_closed:
            return mark_safe('<span class="badge bg-danger">Ended</span>')
        return ""

    @property
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

    @property
    def club_profit_raw(self):
        """Total amount made by the club in this auction.  This number does not take into account rounding in the invoices, nor any invoice adjustments"""
        return add_price_info(self.lots_qs).aggregate(total_sold=Sum("club_cut"))["total_sold"] or 0

    @property
    def club_profit(self):
        """Total amount made by the club in this auction, including rounding in the customer's favor in invoices"""
        return abs(
            Invoice.objects.filter(auction=self.pk).aggregate(total_sold=Sum("calculated_total"))["total_sold"] or 0
        )

    @property
    def gross(self):
        """Total value of all lots sold"""
        return self.lots_qs.aggregate(Sum("winning_price"))["winning_price__sum"] or 0

    @property
    def total_to_sellers(self):
        """Total amount paid out to all sellers"""
        return self.gross - self.club_profit

    @property
    def percent_to_club(self):
        """Percent of gross that went to the club"""
        if self.gross:
            return self.club_profit / self.gross * 100
        else:
            return 0

    @property
    def total_donations(self):
        return (
            self.lots_qs.filter(winning_price__isnull=False, donation=True).aggregate(total=Sum("winning_price"))[
                "total"
            ]
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
        """invoice status of 'ready' is very confusing for people
        This is tied to several pieces of logic that are also not needed for in person auctions, like paypal integratiaon
        With PayPal integration, it may be needed for all auctions, let's see if it continues to be confusing"""
        return True
        # if self.is_online:
        #    return True
        # if self.online_bidding == "disable":
        #    return False
        # return True

    @property
    def show_paypal_csv_link(self):
        if self.is_online:
            return True
        if self.online_bidding == "disable":
            return False
        return True

    @property
    def tos_qs(self):
        """Return AuctionTOS queryset with has_ever_granted_permission annotation.

        has_ever_granted_permission is True if the user has ever joined any auction
        created by this auction's creator with manually_added=False.
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

    @property
    def number_of_confirmed_tos(self):
        """How many people selected a pickup location in this auction"""
        return self.tos_qs.count()

    @property
    def number_of_sellers(self):
        return AuctionTOS.objects.filter(auctiontos_seller__auction=self.pk).distinct().count()
        # return AuctionTOS.objects.filter(auctiontos_seller__auction=self.pk, auctiontos_winner__isnull=False).distinct().count()
        # users = User.objects.values('lot__user').annotate(Sum('lot')).filter(lot__auction=self.pk, lot__winner__isnull=False)
        # users = User.objects.filter(lot__auction=self.pk, lot__winner__isnull=False).distinct()

    @property
    def number_of_sellers_who_didnt_buy(self):
        return (
            AuctionTOS.objects.filter(auctiontos_seller__auction=self.pk, auctiontos_winner__isnull=False)
            .distinct()
            .count()
        )

    # @property
    # def number_of_unsuccessful_sellers(self):
    # """This is the number of sellers who didn't sell ALL their lots"""
    # 	users = User.objects.values('lot__user').annotate(Sum('lot')).filter(lot__auction=self.pk, lot__winner__isnull=True)
    #   users = User.objects.filter(lot__auction=self.pk, lot__winner__isnull=True).distinct()
    # 	return len(users)

    @property
    def number_of_buyers(self):
        # users = User.objects.values('lot__winner').annotate(Sum('lot')).filter(lot__auction=self.pk)
        return AuctionTOS.objects.filter(auctiontos_winner__auction=self.pk).distinct().count()
        # users = User.objects.filter(winner__auction=self.pk).distinct()

    @property
    def median_lot_price(self):
        lots = self.lots_qs.filter(winning_price__isnull=False)
        if lots:
            return median_value(lots, "winning_price")
        else:
            return 0

    @property
    def lots_qs(self):
        """All lots in this auction"""
        # return Lot.objects.exclude(is_deleted=True).filter(auction=self.pk)
        return Lot.objects.exclude(is_deleted=True).filter(auctiontos_seller__auction__pk=self.pk)

    @property
    def total_sold_lots(self):
        return self.lots_qs.filter(winning_price__isnull=False).exclude(banned=True).count()

    @property
    def total_sold_lots_with_buy_now_percent(self):
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

    @property
    def total_unsold_lots(self):
        return self.lots_qs.filter(winning_price__isnull=True).exclude(banned=True).count()

    @property
    def total_lots(self):
        return self.lots_qs.exclude(banned=True).count()

    @property
    def number_of_lots_with_scanned_qr(self):
        return self.lots_qs.filter(pageview__source__icontains="qr", auction__pk=self.pk).distinct().count()

    @property
    def labels_qs(self):
        lots = self.lots_qs.exclude(banned=True)
        if self.is_online:
            lots = lots.filter(auctiontos_winner__isnull=False, winning_price__isnull=False)
        return lots

    @property
    def unprinted_labels_qs(self):
        return self.labels_qs.exclude(label_printed=True)

    @property
    def percent_unsold_lots(self):
        try:
            return self.total_unsold_lots / self.total_lots * 100
        except:
            return 100

    @property
    def lots_sold_per_minute(self):
        """Calculate the average lots sold per minute for in-person auctions.
        This uses the same logic as the auctioneer speed graph, ignoring the first and last 10% of lots."""
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

    @property
    def total_auction_duration(self):
        """For in-person auctions, this also uses the same logic as the auctioneer speed graph, ignoring the first and last 10% of lots."""
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
        """Not directly used in templates, use template_lot_link_first_column and template_lot_link_separate_column instead"""
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

    @property
    def number_of_participants(self):
        """
        Number of users who bought or sold at least one lot
        """
        buyers = AuctionTOS.objects.filter(auctiontos_winner__auction=self.pk).distinct()
        sellers = (
            AuctionTOS.objects.filter(auctiontos_seller__auction=self.pk, auctiontos_winner__isnull=False)
            .exclude(id__in=buyers)
            .distinct()
        )
        # buyers = User.objects.filter(winner__auction=self.pk).distinct()
        # sellers = User.objects.filter(lot__auction=self.pk, lot__winner__isnull=False).exclude(id__in=buyers).distinct()
        return len(sellers) + len(buyers)

    @property
    def preregistered_users(self):
        return AuctionTOS.objects.filter(auction=self.pk, manually_added=False).count()

    @property
    def campaigns_qs(self):
        return AuctionCampaign.objects.filter(auction=self.pk).order_by("-timestamp")

    @property
    def number_of_reminder_emails(self):
        return self.campaigns_qs.exclude(result="ERR").count()

    @property
    def reminder_email_clicks(self):
        if self.number_of_reminder_emails == 0:
            return 0
        return (
            self.campaigns_qs.exclude(result="ERR").exclude(result="NONE").count()
            / self.number_of_reminder_emails
            * 100
        )

    @property
    def reminder_email_joins(self):
        if self.number_of_reminder_emails == 0:
            return 0
        return self.campaigns_qs.filter(result="JOINED").count() / self.number_of_reminder_emails * 100

    @property
    def all_auctions_reminder_email_clicks(self):
        campaigns = AuctionCampaign.objects.exclude(result="ERR").count()
        if campaigns == 0:
            return 0
        return AuctionCampaign.objects.exclude(result="ERR").exclude(result="NONE").count() / campaigns * 100

    @property
    def all_auctions_reminder_email_joins(self):
        campaigns = AuctionCampaign.objects.exclude(result="ERR").count()
        if campaigns == 0:
            return 0
        return AuctionCampaign.objects.filter(result="JOINED").count() / campaigns * 100

    @property
    def weekly_promo_email_clicks(self):
        return PageView.objects.filter(source="weekly_email", auction=self.pk).count()

    @property
    def weekly_promo_email_click_rate(self):
        if self.weekly_promo_emails_sent == 0:
            return 0
        return (self.weekly_promo_email_clicks / self.weekly_promo_emails_sent) * 100

    @property
    def multi_location(self):
        """
        True if there's more than one location at this auction
        """
        locations = self.physical_location_qs.count()
        if locations > 1:
            return True
        return False

    @property
    def no_location(self):
        """
        True if there's no pickup location at all for this auction -- pickup by mail excluded
        """
        locations = self.location_with_location_qs.count()
        if not locations:
            return True
        return False

    @property
    def can_be_deleted(self):
        if self.total_lots:
            return False
        else:
            return True

    @property
    def paypal_invoices(self):
        # all drafts and ready:
        # return Invoice.objects.filter(auction=self).exclude(status="PAID")
        # only ready:
        return Invoice.objects.filter(auction=self, status="UNPAID")

    @property
    def draft_paypal_invoices(self):
        """Used for a tooltip warning telling people to make invoices ready"""
        return Invoice.objects.filter(auction=self, status="DRAFT", calculated_total__lt=0).count()

    @property
    def paypal_invoice_chunks(self):
        """
        Needed to know how many chunks to split the invoice list to
        chunk size 150 per https://www.paypal.com/invoice/batch
        """
        invoices_count = self.paypal_invoices.filter(calculated_total__lt=0).count()
        chunk_size = 150
        chunks = (invoices_count + chunk_size - 1) // chunk_size
        return list(range(1, chunks + 1))

    @property
    def set_location_link(self):
        """If there's a location without a lat and lng, this link will let you edit the first one found"""
        location = self.location_qs.filter(latitude=0, longitude=0, pickup_by_mail=False).first()
        if self.all_location_count == 1:
            location = self.location_qs.first()
        if location:
            return reverse("edit_pickup", kwargs={"pk": location.pk})
        return None

    @property
    def admin_checklist_mostly_completed(self):
        if (
            self.admin_checklist_location_set
            and self.admin_checklist_rules_updated
            and self.admin_checklist_joined
            and self.admin_checklist_others_joined
        ):
            # if self.is_online or (not self.is_online and self.admin_checklist_lots_added):
            return True
        return False

    @property
    def admin_checklist_completed(self):
        if (
            self.admin_checklist_mostly_completed
            and self.admin_checklist_lots_added
            and self.admin_checklist_winner_set
            and self.admin_checklist_additional_admin
        ):
            return True
        return False

    @property
    def admin_checklist_location_set(self):
        if self.allow_mailing_lots or self.location_with_location_qs.count():
            return True
        return False

    @property
    def admin_checklist_rules_updated(self):
        if "You should remove this line and edit this section to suit your auction." in self.summernote_description:
            return False
        return True

    @property
    def admin_checklist_joined(self):
        if (
            AuctionTOS.objects.filter(auction__pk=self.pk).filter(Q(user=self.created_by) | Q(is_admin=True)).count()
            > 0
        ):
            return True
        return False

    @property
    def admin_checklist_others_joined(self):
        if self.number_of_confirmed_tos > 1:
            return True
        return False

    @property
    def admin_checklist_lots_added(self):
        if self.lots_qs.count() > 0:
            return True
        return False

    @property
    def admin_checklist_winner_set(self):
        if self.is_online:
            return True
        if self.lots_qs.filter(auctiontos_winner__isnull=False).count():
            return True
        return False

    @property
    def admin_checklist_additional_admin(self):
        if self.is_online:
            return True
        if (
            AuctionTOS.objects.filter(auction__pk=self.pk).exclude(user=self.created_by).filter(is_admin=True).count()
            > 0
        ):
            return True
        return False

    @property
    def location_link(self):
        if not self.location_qs.count():
            return reverse("create_auction_pickup_location", kwargs={"slug": self.slug})
        if self.location_qs.count() == 1 and not self.is_online:
            return reverse("edit_pickup", kwargs={"pk": self.location_qs.first().pk})
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

    @property
    def auction_admins_qs(self):
        return AuctionTOS.objects.filter(Q(is_admin=True) | Q(user=self.created_by), auction__pk=self.pk).order_by(
            "name"
        )

    @property
    def auction_admins_pks(self):
        """For use in querysets, pks only"""
        return self.auction_admins_qs.values_list("user__pk", flat=True)

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

        # if date_end is in the future, shift the graph to show the same range, but for the present
        if date_end > timezone.now():
            time_difference = date_end - date_start
            date_end = timezone.now()
            date_start = date_end - time_difference
            dates_messed_with = True

        views = PageView.objects.filter(Q(auction=self) | Q(lot_number__auction=self))
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
            Lot.objects.exclude(Q(date_end__isnull=True) | Q(is_deleted=True))
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
            Lot.objects.exclude(Q(date_end__isnull=True) | Q(is_deleted=True))
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

    def set_stat_lot_sell_prices(self):
        """Calculate and return lot sell prices chart data"""
        sold_lots = self.lots_qs.filter(winning_price__isnull=False)

        # Make bins dynamic based on actual sell prices
        if sold_lots.exists():
            from django.db.models import Max

            max_price = sold_lots.aggregate(max_price=Max("winning_price"))["max_price"] or 40
            # Round up to nearest $10 for cleaner bins
            max_price = int((max_price + 9) // 10 * 10)

            # Create bins with $2 intervals up to max price
            num_bins = min(max_price // 2, 30)  # Cap at 30 bins to avoid too many
            if num_bins < 10:
                num_bins = 10  # Minimum 10 bins

            histogram = bin_data(
                sold_lots,
                "winning_price",
                number_of_bins=num_bins,
                start_bin=1,
                end_bin=max_price - 1,
                add_column_for_high_overflow=True,
            )

            # Generate labels dynamically
            labels = ["Not sold"]
            for i in range(0, max_price - 1, 2):
                labels.append(f"{self.currency_symbol}{i + 1}-{i + 2}")
            labels.append(f"{self.currency_symbol}{max_price}+")

            return {
                "labels": labels,
                "providers": ["Number of lots"],
                "data": [[self.total_unsold_lots] + histogram],
            }
        else:
            # No sold lots, use default bins
            histogram = bin_data(
                sold_lots,
                "winning_price",
                number_of_bins=19,
                start_bin=1,
                end_bin=39,
                add_column_for_high_overflow=True,
            )
            return {
                "labels": [
                    "Not sold",
                    f"{self.currency_symbol}1-2",
                    f"{self.currency_symbol}3-4",
                    f"{self.currency_symbol}5-6",
                    f"{self.currency_symbol}7-8",
                    f"{self.currency_symbol}9-10",
                    f"{self.currency_symbol}11-12",
                    f"{self.currency_symbol}13-14",
                    f"{self.currency_symbol}15-16",
                    f"{self.currency_symbol}17-18",
                    f"{self.currency_symbol}19-20",
                    f"{self.currency_symbol}21-22",
                    f"{self.currency_symbol}23-24",
                    f"{self.currency_symbol}25-26",
                    f"{self.currency_symbol}27-28",
                    f"{self.currency_symbol}29-30",
                    f"{self.currency_symbol}31-32",
                    f"{self.currency_symbol}33-34",
                    f"{self.currency_symbol}35-36",
                    f"{self.currency_symbol}37-38",
                    f"{self.currency_symbol}39-40",
                    f"{self.currency_symbol}40+",
                ],
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
            PageView.objects.filter(Q(auction=self) | Q(lot_number__auction=self))
            .exclude(referrer__isnull=True)
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

        lots = Lot.objects.filter(auction=self, winning_price__isnull=False).annotate(num_images=Count("lotimage"))
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
        for location in self.location_qs:
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
        if auctiontos.count() == 0:
            lot_with_buy_now_percent = 0
            account_percent = 0
        else:
            account_percent = int(auctiontos_with_account.count() / auctiontos.count() * 100)
            lot_with_buy_now_percent = int(lot_with_buy_now / auctiontos.count() * 100)
        invoices = Invoice.objects.filter(auction=self)
        viewed_invoices = invoices.filter(opened=True)
        if invoices.count():
            view_invoice_percent = int(viewed_invoices.count() / invoices.count() * 100)
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

    def get_stat_misc(self):
        """A few one-off stats that are slow to calculate and/or dependent on page views"""
        if self.cached_stats and "misc" in self.cached_stats:
            return self.cached_stats["misc"]
        return {}

    def set_stat_misc(self):
        """A few one-off stats that are slow to calculate and/or dependent on page views"""

        all_views = PageView.objects.filter(Q(auction=self) | Q(lot_number__auction=self))
        anonymous_views = all_views.values("session_id").annotate(c=Count("session_id")).count()
        user_views = all_views.values("user").annotate(c=Count("user")).count()
        total_views = anonymous_views + user_views

        total_bidders = User.objects.filter(bid__lot_number__auction=self).annotate(c=Count("id")).count()
        total_winners = User.objects.filter(winner__auction=self).annotate(c=Count("id")).count()

        # Additional email/reminder stats
        reminder_emails_sent = self.number_of_reminder_emails
        reminder_email_click_rate = self.reminder_email_clicks
        reminder_email_join_rate = self.reminder_email_joins

        # QR code scans
        qr_scans = self.number_of_lots_with_scanned_qr

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
        }

    def recalculate_stats(self):
        """Recalculate and cache all auction statistics.
        This method calls all the setter methods to calculate chart data
        and stores it in the cached_stats JSONField to avoid expensive recalculations.
        """
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

        # Save the stats
        self.cached_stats = stats
        self.last_stats_update = timezone.now()

        # Smart scheduling based on auction age and status
        # Active auctions (start date within a week): recalculate every 4 hours
        # Other auctions: recalculate once per day
        # Auctions > 90 days old: don't recalculate automatically
        now = timezone.now()

        if self.date_start:
            days_until_start = (self.date_start - now).days
            days_since_start = (now - self.date_start).days

            # Auctions > 90 days in the past aren't recalculated at all
            if days_since_start > 90:
                self.next_update_due = None
            # Active auctions (started within 7 days ago or start within 7 days) - every 4 hours
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
        """Applies to can be RULES, USERS, INVOICES, LOTS, LOT_WINNERS, user should be the user making the change or None if it's a system change.
        Action is a string describing the change, form is a form instance that has changed data
        """
        if form:
            action += " "
            for field_name in form.changed_data:
                try:
                    field = form.instance._meta.get_field(field_name)
                    action += field.verbose_name
                except Exception:
                    action += field_name.replace("_", " ").title()
                action += ", "
            action = action[:-2]  # remove the last comma and space
        if len(action) > 800:
            action = action[:797] + "..."
        AuctionHistory.objects.create(
            auction=self,
            user=user,
            action=action[:800],
            applies_to=applies_to,
        )


class PickupLocation(models.Model):
    """
    A pickup location associated with an auction
    A given auction can have multiple pickup locations
    """

    name = models.CharField(max_length=70, default="", blank=True, null=True)
    name.help_text = "Location name shown to users.  e.x. University Mall in VT"
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    auction = models.ForeignKey(Auction, null=True, blank=True, on_delete=models.CASCADE)
    # auction.help_text = "If your auction isn't listed here, it may not exist or has already ended"
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

    @property
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

    @property
    def number_of_incoming_lots(self):
        return self.incoming_lots.count()

    @property
    def number_of_outgoing_lots(self):
        return self.outgoing_lots.count()

    @property
    def email_list(self):
        """String of all email addresses associated with this location, used for bcc'ing all people at a location"""
        email = ""
        for user in self.user_list:
            if user.email:
                email += user.email + ", "
        return email

    @property
    def total_sold(self):
        lots = self.outgoing_lots.aggregate(total_winning_price=Sum("winning_price"))
        return lots["total_winning_price"] or 0

    @property
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


class AuctionTOS(models.Model):
    """Models how a user engages with an auction and is the basis for the user view when running an auction
    Usually this will correspond with a single person which may or may not also be a user"""

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
    # yes we are using a string to store a number
    # this is actually important because some day, someone will ask to make the bidder numbers have characters like "1-234" or people's names
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

    @property
    def phone_as_string(self):
        """Add proper dashes to phone"""
        if not self.phone_number:
            return ""
        # n = re.sub("[^0-9]", "", self.phone_number)
        # return format(int(n[:-1]), ",").replace(",", "-") + n[-1]
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

    @property
    def unbanned_lot_qs(self):
        return self.lots_qs.exclude(banned=True)

    @property
    def unbanned_lot_count(self):
        return self.unbanned_lot_qs.count()

    @property
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

    @property
    def unprinted_label_count(self):
        return self.unprinted_labels_qs.count()

    @property
    def print_labels_link_html(self):
        if self.unbanned_lot_count:
            url = reverse(
                "print_labels_by_bidder_number",
                kwargs={"bidder_number": self.bidder_number, "slug": self.auction.slug},
            )
            return f"<a href='{url}'><i class='bi bi-tags me-1'></i>Print labels</a>"
        return ""

    @property
    def print_unprinted_labels_link_html(self):
        if self.unprinted_label_count and self.unprinted_label_count != self.print_labels_qs.count():
            unprinted_url = reverse(
                "print_unprinted_labels_by_bidder_number",
                kwargs={"bidder_number": self.bidder_number, "slug": self.auction.slug},
            )
            return f"<a href='{unprinted_url}'>Print only {self.unprinted_label_count} unprinted labels</a>"
        return ""

    @property
    def print_labels_html(self):
        """For use in HTMX users table; print lot labels for this user"""
        if self.unbanned_lot_count:
            result = self.print_labels_link_html
            if self.print_unprinted_labels_link_html:
                result += f"""
                <button type="button" class="btn btn-sm btn-secondary dropdown-toggle dropdown-toggle-split" data-bs-toggle="dropdown" aria-haspopup="true" aria-expanded="false">
                </button>
                <div class="dropdown-menu">
                    <span class='dropdown-item'>{self.print_unprinted_labels_link_html}</span>
                </div>"""
            return html.format_html(result)
        return ""

    @property
    def actions_dropdown_html(self):
        show_on_mobile_string = "d-md-none"
        result = f"""<button type='button' class='btn btn-sm btn-secondary dropdown-toggle dropdown-toggle-split' data-bs-toggle='dropdown'
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
        result += f"<span class='dropdown-item'><a href={won_lots_url}><i class='bi bi bi-calendar-check me-1'></i>View {self.bought_lots_qs.count()} lots won</a></span>"
        sold_lots_url = (
            reverse("auction_lot_list", kwargs={"slug": self.auction.slug}) + f"?query=seller%3A{self.bidder_number}"
        )

        result += f"<span class='dropdown-item'><a href={sold_lots_url}><i class='bi bi-calendar me-1'></i>View {self.lots_qs.count()} lots sold</a></span>"
        delete_url = reverse("auctiontosdelete", kwargs={"pk": self.pk})
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
        result += "</div>"
        return html.format_html(result)

    @property
    def invoice(self):
        return Invoice.objects.filter(auctiontos_user=self.pk).order_by("-date").first()

    @property
    def invoice_link_html(self):
        """HTML snippet with a link to the invoice for this auctionTOS, if set.  Otherwise, show create link"""
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
        def check_number_in_auction(number):
            """See if any other auctiontos are currently using a given bidder number"""
            return AuctionTOS.objects.filter(bidder_number=number, auction=self.auction).count()

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
                # self.confirm_email_sent = True
                # self.print_reminder_email_sent = True
                # self.second_confirm_email_sent = True
            if self.email and not self.user:
                self.user = User.objects.filter(is_active=True, email=self.email).first()
        # fill out some fields from user, if set
        # There is a huge security concern here:   <<<< ATTENTION!!!
        # If someone creates an auction and adds every email address that's public
        # We must avoid allowing them to collect addresses/phone numbers/locations from these people
        # Having this code below run only on creation means that the user won't be filled out and prevents collecting data
        # if making changes, remember that there's user_logged_in_callback in signals.py which sets the user field
        # if self.user and not self.pk:
        # moved to AuctionInfo.post()
        # if not self.name:
        # 	self.name = self.user.first_name + " " + self.user.last_name
        # if not self.email:
        # 	self.email = self.user.email
        # userData, created = UserData.objects.get_or_create(
        # 	user = self.user,
        # 	defaults={},
        # 	)
        # if not self.phone_number:
        # self.phone_number = userData.phone_number
        # if not self.address:
        # self.address = userData.address
        # set the bidder number based on the phone, address, last used number, or just at random
        if not self.bidder_number or self.bidder_number == "None":
            # recycle numbers from the last auction if we can
            # Build query to find previous AuctionTOS by same auction creator
            from django.db.models import Q
            
            query = Q()
            if self.user:
                query |= Q(user=self.user)
            if self.email:
                query |= Q(email=self.email)
            
            last_number_used = None
            if query:
                last_number_used = (
                    AuctionTOS.objects.filter(query, auction__created_by=self.auction.created_by)
                    .exclude(pk=self.pk)  # Exclude self if updating
                    .order_by("-auction__date_posted")
                    .first()
                )
            
            if last_number_used and check_number_in_auction(last_number_used.bidder_number) == 0:
                self.bidder_number = last_number_used.bidder_number
            else:
                dont_use_these = ["13", "14", "15", "16", "17", "18", "19"]
                search = None
                if self.phone_number:
                    search = re.search(r"([\d]{3}$)|$", self.phone_number).group()
                if not search or str(search) in dont_use_these:
                    if self.address:
                        search = re.search(r"([\d]{3}$)|$", self.address).group()
                if self.user:
                    userData = self.user.userdata
                    if userData.preferred_bidder_number:
                        search = userData.preferred_bidder_number
                # I guess it's possible that someone could make 999 accounts and have them all join a single auction, which would turn this into an infinite loop
                failsafe = 0
                # bidder numbers shouldn't start with 0
                try:
                    if str(search)[0] == "0":
                        search = search[1:]
                    if str(search)[0] == "0":
                        search = search[1:]
                except:
                    pass
                while failsafe < 6000:
                    search = str(search)
                    if search[:-2] not in dont_use_these and search != "None":
                        if check_number_in_auction(search) == 0:
                            self.bidder_number = search
                            if self.user:
                                if not userData.preferred_bidder_number:
                                    userData.preferred_bidder_number = search
                                    userData.save()
                            break
                    # OK, give up and just randomly generate something
                    search = randint(1, 999)
                    failsafe += 1
        if not self.bidder_number:
            # I don't ever want this to be null
            self.bidder_number = "ERROR"
        if str(self.memo) == "None":
            self.memo = ""
        # update the email address as appropriate
        # if you changed the email of this tos, reset the email status
        if not self.name:
            self.name = "Unknown"
        if self.email and self.pk:
            saved_tos = AuctionTOS.objects.filter(pk=self.pk).first()
            if saved_tos and saved_tos.email and saved_tos.email != self.email:
                self.email_address_status = "UNKNOWN"
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

            # Check if the cleaned bidder_number would create a duplicate
            # Exclude self from the check (if updating existing record)
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

        duplicate_instance = self.auction.find_user(name=self.name, email=self.email, exclude_pk=self.pk)
        if duplicate_instance:
            # using update here avoids recursion because update does not call save()
            AuctionTOS.objects.filter(pk=self.pk).update(possible_duplicate=duplicate_instance.pk)
            AuctionTOS.objects.filter(pk=duplicate_instance.pk).update(possible_duplicate=self.pk)
        else:
            # no duplicate found
            if self.possible_duplicate:
                # remove ourselves from the duplicate if it was previously set
                AuctionTOS.objects.filter(pk=self.possible_duplicate.pk).update(possible_duplicate=None)
                AuctionTOS.objects.filter(pk=self.pk).update(possible_duplicate=None)

        if self.user:
            related_campaign = (
                AuctionCampaign.objects.filter(auction=self.auction, user=self.user).exclude(result="JOINED").first()
            )
            if related_campaign:
                related_campaign.result = "JOINED"
                related_campaign.save()

    @property
    def display_name_for_admins(self):
        """Same as display name, but no anonymous option"""
        if self.auction.is_online:
            if self.user and not self.manually_added:
                return self.user.username
        if self.bidder_number:
            return self.bidder_number
        return "Unknown user"

    @property
    def display_name(self):
        """Use usernames for online auctions, and bidder numbers for in-person auctions"""
        # return f"{self.user} will meet at {self.pickup_location} for {self.auction}"
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

    @property
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
        # single location auction, or user's location not set; anyway, not a problem
        return True

    @property
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

    @property
    def previous_auctions_count(self):
        return AuctionTOS.objects.filter(email=self.email, createdon__lte=self.createdon).exclude(pk=self.pk).count()

    @property
    def closer_location_savings(self):
        if not self.has_selected_closest_location:
            if self.closest_location_for_this_user and self.distance_traveled:
                return int(self.distance_traveled - self.closest_location_for_this_user.distance)
        return 0

    @property
    def closer_location_warning(self):
        current_site = Site.objects.get_current()
        if self.closer_location_savings > 9:
            return f"You've selected {self.pickup_location}, but {self.closest_location_for_this_user} is {int(self.closer_location_savings)} miles closer to you.  You can change your pickup location on the auction rules page: https://{current_site.domain}{self.auction.get_absolute_url()}#join"
        return ""

    @property
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

    @property
    def trying_to_avoid_ban(self):
        """We track IPs in userdata, so we can do a quick check for this"""
        if self.user:
            userData = self.user.userdata
            if userData.last_ip_address:
                other_users = UserData.objects.filter(last_ip_address=userData.last_ip_address).exclude(pk=userData.pk)
                for other_user in other_users:
                    logger.debug("%s is also known as %s", self.user, other_user.user)
                    banned = UserBan.objects.filter(banned_user=other_user.user, user=self.auction.created_by).first()
                    if banned:
                        url = reverse("userpage", kwargs={"slug": other_user.user.username})
                        return f"<a href='{url}'>{other_user.user.username}</a>"
        return False

    @property
    def number_of_userbans(self):
        if self.user:
            other_bans = UserBan.objects.filter(banned_user=self.user)
            return other_bans.count()
        return ""


class Lot(models.Model):
    """A lot is something to bid on"""

    PIC_CATEGORIES = (
        ("ACTUAL", "This picture is of the exact item"),
        (
            "REPRESENTATIVE",
            "This is my picture, but it's not of this exact item.  e.x. This is the parents of these fry",
        ),
        ("RANDOM", "This picture is from the internet"),
    )
    # 3 lot numbers follow, in general use the property lot_number_display which will select the appropriate one
    # all have the verbose name lot number, and to users they are all essentially the same, but they are used differently
    # below is the database pk
    lot_number = models.AutoField(primary_key=True)
    # below is an automatically assigned int for use in auctions
    lot_number_int = models.IntegerField(null=True, blank=True, verbose_name="Lot number", db_index=True)
    # below is an override of the other lot numbers, it was the default for use in auctions until 2025, but now lot_number_int is used instead
    # see https://github.com/iragm/fishauctions/issues/269
    custom_lot_number = models.CharField(max_length=9, blank=True, null=True, verbose_name="Lot number", db_index=True)
    custom_lot_number.help_text = "You can override the default lot number with this"
    lot_name = models.CharField(max_length=40)
    slug = AutoSlugField(populate_from="lot_name", unique=False)
    # lot_name.help_text = "Short description of this lot"
    image = ThumbnailerImageField(upload_to="images/", blank=True)
    image.help_text = "Optional.  Add a picture of the item here."
    image_source = models.CharField(max_length=20, choices=PIC_CATEGORIES, blank=True)
    image_source.help_text = "Where did you get this image?"
    custom_checkbox = models.BooleanField(default=False, verbose_name="Custom checkbox")
    custom_field_1 = models.CharField(max_length=60, default="", blank=True)
    i_bred_this_fish = models.BooleanField(default=False, verbose_name=settings.I_BRED_THIS_FISH_LABEL)
    i_bred_this_fish.help_text = "Check to get breeder points for this lot"
    summernote_description = models.TextField(verbose_name="Description", default="", blank=True)
    # description = MarkdownField(
    #     rendered_field="description_rendered",
    #     validator=VALIDATOR_STANDARD,
    #     blank=True,
    #     null=True,
    # )
    # description.help_text = "To add a link: [Link text](https://www.google.com)"
    # description_rendered = RenderedMarkdownField(blank=True, null=True)
    reference_link = models.URLField(blank=True, null=True)
    reference_link.help_text = (
        "A URL with additional information about this lot.  YouTube videos will be automatically embedded."
    )
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    quantity.help_text = "How many of this item are in this lot?"
    reserve_price = models.PositiveIntegerField(
        default=2,
        validators=[MinValueValidator(1), MaxValueValidator(2000)],
        verbose_name="Minimum bid",
    )
    reserve_price.help_text = "Also called a reserve price. Lot will not be sold unless someone bids at least this much"
    buy_now_price = models.PositiveIntegerField(
        default=None,
        validators=[MinValueValidator(1), MaxValueValidator(1000)],
        blank=True,
        null=True,
    )
    buy_now_price.help_text = (
        "This lot will be sold with no bidding for this price, if someone is willing to pay this much"
    )
    species = models.ForeignKey(Product, null=True, blank=True, on_delete=models.SET_NULL)
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
    winning_price = models.PositiveIntegerField(null=True, blank=True, db_index=True)
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
    # seller and buyer invoice are no longer needed and can safely be removed in a future migration
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
    # promotion weight started out as a way to test how heavily a lot should get promoted, but it's now used as a random number generator
    # to allow some stuff that's not in your favorite cateogy to show up in the recommended list
    promotion_weight = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(20)])
    feedback_rating = models.IntegerField(default=0, validators=[MinValueValidator(-1), MaxValueValidator(1)])
    feedback_text = models.CharField(max_length=100, blank=True, null=True)
    winner_feedback_rating = models.IntegerField(default=0, validators=[MinValueValidator(-1), MaxValueValidator(1)])
    winner_feedback_text = models.CharField(max_length=100, blank=True, null=True)
    date_of_last_user_edit = models.DateTimeField(auto_now_add=True, blank=True)
    is_chat_allowed = models.BooleanField(default=True)
    is_chat_allowed.help_text = (
        "Uncheck to prevent chatting on this lot.  This will not remove any existing chat messages"
    )
    buy_now_used = models.BooleanField(default=False)

    # Location, populated from userdata.  This is needed to prevent users from changing their address after posting a lot
    latitude = models.FloatField(blank=True, null=True, db_index=True)
    longitude = models.FloatField(blank=True, null=True, db_index=True)
    address = models.CharField(max_length=500, blank=True, null=True)

    # Payment and shipping options, populated from last submitted lot
    # Only show these fields if auction is set to none
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

    def save(self, *args, **kwargs):
        from django.db import transaction

        # for old and new auctions, generate a lot number int or custom_lot_number
        # Use database-level locking to prevent race conditions when assigning lot numbers
        needs_lock = self.auction and (
            (self.lot_number_int is None)  # Standard mode needs lot_number_int
            or (
                not self.custom_lot_number and self.auction.use_seller_dash_lot_numbering
            )  # Seller dash mode needs custom_lot_number
        )

        if needs_lock:
            # We need to wrap the entire save in a transaction with locking
            with transaction.atomic():
                # Lock the auction row using SELECT FOR UPDATE
                # This will block other transactions trying to lock the same row until this transaction completes
                Auction.objects.select_for_update().get(pk=self.auction.pk)

                # Assign lot_number_int if needed
                if self.lot_number_int is None:
                    # Now safely get the max lot_number_int while holding the lock
                    minimum_lot_number = 1
                    # This is deliberately not excluding deleted and removed lots -- don't use auction.lots_qs here
                    max_number = Lot.objects.filter(auction=self.auction).aggregate(Max("lot_number_int"))[
                        "lot_number_int__max"
                    ]
                    self.lot_number_int = (max_number or (minimum_lot_number - 1)) + 1

                # Continue with the rest of the save logic
                self._do_save(*args, **kwargs)
        else:
            # No lock needed, proceed normally
            self._do_save(*args, **kwargs)

    def _do_save(self, *args, **kwargs):
        """Internal method to complete the save operation"""
        # custom lot number set for old auctions: bidder_number-lot_number format
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
        if fix_category:
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
        # These lines would make it so you can't set a reserve price (for in person bidding)
        # when an auction is set to be buy now only
        # if self.auction and self.auction.online_bidding == "buy_now_only":
        #    self.reserve_price = self.buy_now_price
        if (
            self.auction
            and self.auction.force_donation_threshold
            and self.winning_price
            and self.winning_price <= self.auction.force_donation_threshold
        ):
            self.donation = True
        self.summernote_description = remove_html_color_tags(self.summernote_description)
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
        # make sure lot_number_display is unique within the auction
        # This handles both lot_number_int and custom_lot_number (seller_dash_lot_numbering)
        # reported in a large auction where two lots had the same number but I have not been able to reproduce it
        # https://github.com/iragm/fishauctions/issues/420
        # Note: Only check after first save (when pk exists). Race conditions during initial save
        # are prevented by the SELECT FOR UPDATE locking in the save() method above.
        if self.auction and self.pk:
            # Check for duplicates based on lot_number_display
            if self.auction.use_seller_dash_lot_numbering and self.custom_lot_number:
                # Check for duplicate custom_lot_number in seller_dash_lot_numbering mode
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

    @property
    def currency(self):
        """Get the currency for this lot based on the auction creator or lot owner"""
        if self.auction and self.auction.created_by:
            return self.auction.created_by.userdata.currency
        elif self.user:
            return self.user.userdata.currency
        return "USD"

    @property
    def currency_symbol(self):
        """Get the currency symbol for this lot"""
        return get_currency_symbol(self.currency)

    def add_winner_message(self, user, tos, winning_price):
        """Create a lot history message when a winner is declared (or changed)
        It's critical that this function is called every time the winner is changed so that invoices get recalculated"""
        message = (
            f"{user.username} has set bidder {tos} as the winner of this lot ({self.currency_symbol}{winning_price})"
        )
        LotHistory.objects.create(
            lot=self,
            user=user,
            message=message,
            notification_sent=True,
            bid_amount=winning_price,
            changed_price=True,
            seen=True,
        )
        # Impossibly this line sometimes errors, there must be a way of making duplicates
        # invoice, created = Invoice.objects.get_or_create(auctiontos_user=tos, auction=self.auction, defaults={})
        invoice = Invoice.objects.filter(auctiontos_user=tos, auction=self.auction).first()
        if not invoice:
            invoice = Invoice.objects.create(auctiontos_user=tos, auction=self.auction)
        invoice.recalculate()
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
        channel_layer = channels.layers.get_channel_layer()
        async_to_sync(channel_layer.group_send)(f"lot_{self.pk}", message)

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
        """Send websocket message and create LotHistory when lot ends with or without a winner"""
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

        # at this point, the lot should have a winner filled out if it's sold.  If it still doesn't:
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
            LotHistory.objects.create(
                lot=self,
                user=bidder,
                message=message,
                changed_price=True,
                current_price=self.high_bid,
            )
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
        """Handle automatic relisting logic for non-auction lots

        Returns:
            tuple: (relist: bool, sendNoRelistWarning: bool)
        """
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
        """Create a duplicate lot for relisting purposes

        Returns:
            Lot: The newly created lot
        """
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
            )
            newImage.image = get_thumbnailer(originalImage.image)
            # if the original lot sold, this picture sure isn't of the actual item
            if originalImage.image_source == "ACTUAL":
                newImage.image_source = "REPRESENTATIVE"
            newImage.save()

        return self

    def refund(self, amount, user, message=None):
        """Call this to add a message when refunding a lot
        If square_refund_possible, automatically processes Square refund"""
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

    @property
    def winner_invoice(self):
        """Get the Invoice for this lot's winner
        Returns Invoice object or None if not found
        """
        from auctions.models import Invoice

        if not (self.auctiontos_winner or self.winner):
            return None

        try:
            query = Q()
            if self.auctiontos_winner:
                query |= Q(auctiontos_user=self.auctiontos_winner)
            if self.winner:
                query |= Q(user=self.winner, auction=self.auction)

            return Invoice.objects.filter(query).first()
        except Exception:
            return None

    @property
    def sellers_invoice(self):
        """Get the Invoice for this lot's seller
        Returns Invoice object or None if not found
        """
        from auctions.models import Invoice

        if not (self.auctiontos_seller or self.user):
            return None

        try:
            query = Q()
            if self.auctiontos_seller:
                query |= Q(auctiontos_user=self.auctiontos_seller)
            if self.user:
                query |= Q(user=self.user, auction=self.auction)

            return Invoice.objects.filter(query).first()
        except Exception:
            return None

    @property
    def square_refund_possible(self):
        """Returns True if there's a Square payment associated with this lot's invoice
        with enough funds to cover the lot's cost and no refund has been issued yet"""
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
        """Create a Square refund for this lot
        Args:
            percent: Percentage of lot winning_price to refund (0-100)
        Returns:
            Error message string or None on success
        """
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

    def image_permission_check(self, user):
        """See if `user` can add/edit images to this lot"""
        if not self.can_add_images:
            return False
        if not user.is_authenticated:
            return False
        if self.user == user:
            return True
        if self.auctiontos_seller and self.auctiontos_seller.user:
            if self.auctiontos_seller.user == user:
                return True
        if user.is_superuser:
            return True
        if self.auction:
            tos = AuctionTOS.objects.filter(is_admin=True, user=user, user__isnull=False, auction=self.auction).first()
            if tos:
                return True
            if self.auction.created_by == user:
                return True
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
            return f"/invoices/{invoice.pk}"
        return ""

    @property
    def winner_invoice_link(self):
        """/invoices/123 for the auction/winner of this lot"""
        invoice = self.winner_invoice
        if invoice:
            return f"/invoices/{invoice.pk}"
        return ""

    @property
    def tos_needed(self):
        if not self.auction:
            return False
        if self.auctiontos_seller:
            return False
        if AuctionTOS.objects.filter(user=self.user, auction=self.auction).exists():
            return False
        return f"/auctions/{self.auction.slug}"

    @property
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

    @property
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

    @property
    def seller_as_str(self):
        """String of the seller name or number, for use on lot pages"""
        if self.auctiontos_seller:
            return str(self.auctiontos_seller)
        if self.user:
            return str(self.user)
        return "Unknown"

    @property
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

    @property
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
        """A div that admins can click on to show the high bidder.  Include only if view is admin
        Returns safe html for inclusion in a template"""
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

    @property
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
    def pre_registered(self):
        """True if this lot will get a discount for being pre-registered"""
        if self.auction:
            if self.auction.pre_register_lot_discount_percent or self.auction.pre_register_lot_entry_fee_discount:
                if self.added_by and self.user:
                    if self.added_by == self.user:
                        return True
        return False

    @property
    def number_of_watchers(self):
        return Watch.objects.filter(lot_number=self.lot_number).count()

    @property
    def hard_end(self):
        """The absolute latest a lot can end, even with dynamic endings"""
        dynamic_end = datetime.timedelta(minutes=60)
        if self.auction:
            return self.auction.dynamic_end
        # there is currently no hard endings on lots not associated with an auction
        # as soon as the lot is saved, date_end will be set to dynamic_end (by consumers.reset_lot_end_time)
        # a new field hard_end could be added to lot to accomplish this, but I do not think it makes sense to have a hard end at this point
        # collect stats from a couple auctions with dynamic endings and re-assess
        return self.date_end + dynamic_end

    @property
    def calculated_end(self):
        """Return datetime object for when this lot will end.

        A good alternative is lot.calculated_end_for_templates which returns either a string or a datetime
        """
        # for in-person auctions only
        if self.is_part_of_in_person_auction:
            return self.auction.date_start + datetime.timedelta(days=364)
        # online auctions update lot.date_end (rolling endings)
        if self.date_end:
            return self.date_end
        # I would hope we never get here...but it it theoretically possible that a bug could cause self.date_end to be blank
        return timezone.now()

    @property
    def calculated_end_for_templates(self):
        """For models, use self.calculated_end which always returns a date
        But for places where a user can see this, we need a friendly reminder that the auction admin needs to manually end lots"""
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
        # sometimes people use buy now, in which case self.ended = True, but the auction itself hasn't ended yet
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
            # if this lot is part of an auction, allow changes right up until lot submission ends
            if timezone.now() > self.auction.lot_submission_end_date:
                return "Lot submission is over for this auction"
        # if we are getting here, there are no bids or this lot is not part of an auction
        # lots that are not part of an auction can always be edited as long as there are no bids
        return False

    @property
    def can_be_edited(self):
        """Check to see if this lot can be edited.
        This is needed to prevent people making lots a donation right before the auction ends
        Actually, by request from many people, there's nothing at all preventing that right at this moment..."""
        if self.cannot_be_edited_reason:
            return False
        return True

    @property
    def cannot_be_deleted_reason(self):
        if self.cannot_change_reason:
            return self.cannot_change_reason
        if self.auction and self.auction.is_online and self.auction.unsold_lot_fee:
            # if this lot is part of an auction, allow changes until 24 hours before the lot submission end
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
        """Check to see if this lot can be deleted.
        This is needed to prevent people deleting lots that don't sell right before the auction ends"""
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
        """Return false if bidding is allowed, or an error message.  Used when trying to bid on lots."""
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
        # but, see https://github.com/iragm/fishauctions/issues/116
        # all we would need for this request is to configure Auction.date_end
        # and a new view to set it to X date (probably 1 minute in the future)
        # the biggest issue I see is lack of an undo on this option
        if self.auction:
            if self.auction.is_online:
                return False
            else:
                return True
        return False

    @property
    def ended(self):
        """Used by the view for display of whether or not the auction has ended
        See also the database field active, which is set (based on this field) by a system job (endauctions.py)"""
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
        """Number of minutes until bidding ends, as an int.  Returns 0 if bidding has ended"""
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
        """
        If a lot is about to end in less than a minute, notification will be pushed to the channel
        """
        if self.minutes_to_end < 1:
            return True
        return False

    @property
    def within_dynamic_end_time(self):
        """
        Return true if a lot will end in the next 15 minutes.  This is used to update the lot end time when last minute bids are placed.
        """
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

    @property
    def max_bid(self):
        """returns the highest bid amount for this lot - this number should not be visible to the public"""
        allBids = (
            Bid.objects.exclude(is_deleted=True)
            .filter(
                lot_number=self.lot_number,
                last_bid_time__lte=self.calculated_end,
                amount__gte=self.reserve_price,
            )
            .order_by("-amount", "last_bid_time")[:2]
        )
        try:
            # $1 more than the second highest bid
            bidPrice = allBids[0].amount
            return bidPrice
        except:
            return self.reserve_price

    @property
    def bids(self):
        """Get all bids for this lot, highest bid first"""
        # bids = Bid.objects.filter(lot_number=self.lot_number, last_bid_time__lte=self.calculated_end, amount__gte=self.reserve_price).order_by('-amount', 'last_bid_time')
        bids = (
            Bid.objects.exclude(is_deleted=True)
            .filter(
                lot_number=self.lot_number,
                last_bid_time__lte=self.calculated_end,
                amount__gte=self.reserve_price,
            )
            .order_by("-amount", "last_bid_time")
        )
        return bids

    @property
    def high_bid(self):
        """returns the high bid amount for this lot"""
        if self.winning_price:
            return self.winning_price
        if self.sealed_bid:
            try:
                bids = self.bids
                return self.bids[0].amount
            except:
                return 0
        else:
            if self.auction and self.auction.online_bidding == "buy_now_only" and not self.bids:
                if self.buy_now_price:
                    return self.buy_now_price
                return ""
            try:
                bids = self.bids
                # highest bid is the winner, but the second highest determines the price
                if bids[0].amount == bids[1].amount:
                    return bids[0].amount
                else:
                    # this is the old method: 1 dollar more than the second highest bidder
                    # this would cause an issue if someone was tied for high bidder, and increased their proxy bid
                    bidPrice = bids[1].amount + 1
                    # instead, we'll just return the second highest bid in the case of a tie
                    # bidPrice = bids[1].amount
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

    @property
    def all_page_views(self):
        """Return a set of all users who have viewed this lot, and how long they looked at it for"""
        return PageView.objects.filter(lot_number=self.lot_number)

    @property
    def anonymous_views(self):
        return len(PageView.objects.filter(lot_number=self.lot_number, user_id__isnull=True))

    @property
    def page_views(self):
        """Total number of page views from all users"""
        pageViews = self.all_page_views
        return len(pageViews)

    @property
    def number_of_bids(self):
        """How many users placed bids on this lot?"""
        bids = Bid.objects.exclude(is_deleted=True).filter(
            lot_number=self.lot_number,
            bid_time__lte=self.calculated_end,
            amount__gte=self.reserve_price,
        )
        return len(bids)

    @property
    def view_to_bid_ratio(self):
        """A low number here represents something interesting but not wanted.  A high number (closer to 1) represents more interest"""
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
        # only allow chat for an hour after an auction ends
        # date_chat_end = self.calculated_end + datetime.timedelta(minutes=60)
        # if timezone.now() > date_chat_end:
        # 	return False
        return True

    @property
    def image_count(self):
        """Count the number of images associated with this lot"""
        return self.images.count()

    @property
    def multimedia_count(self):
        """Count the number of images + reference link if video associated with this lot"""
        count = 0
        if self.video_link:
            count = 1
        return self.image_count + count

    @property
    def images(self):
        """All images associated with this lot"""
        return LotImage.objects.filter(lot_number=self.lot_number).order_by("-is_primary", "createdon")

    @property
    def auto_image(self):
        """Grab an automatically generated image"""
        if not self.auction:
            return None
        if self.user and not self.user.userdata.auto_add_images:
            return None
        if not self.auction.auto_add_images:
            return None
        return find_image(self.lot_name, self.user, self.auction)

    @property
    def thumbnail(self):
        default = LotImage.objects.filter(lot_number=self.lot_number, is_primary=True).first()
        if default:
            return default
        return self.auto_image

    def get_absolute_url(self):
        return f"/lots/{self.lot_number}/{self.slug}/"

    @property
    def lot_number_display(self):
        if self.auction and self.auction.use_seller_dash_lot_numbering and self.custom_lot_number:
            return self.custom_lot_number
        # note that custom lot numbers are effectively disabled here
        if self.auction and self.lot_number_int:
            return self.lot_number_int
        return self.lot_number

    @property
    def lot_link(self):
        """Simplest link to access this lot with"""
        if self.auction:
            return f"/auctions/{self.auction.slug}/lots/{self.lot_number_display}/{self.slug}/"
        return f"/lots/{self.lot_number}/{self.slug}/"

    @property
    def full_lot_link(self):
        """Full domain name URL for this lot"""
        current_site = Site.objects.get_current()
        return f"{current_site.domain}{self.lot_link}"

    @property
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
            result += f" Min bid: ${self.reserve_price}"
        if self.buy_now_price and not self.sold:
            result += f" Buy now: ${self.buy_now_price}"
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

    @property
    def label_line_3(self):
        """Used for printed labels"""
        result = ""
        if self.auction and self.auction.multi_location:
            if self.auctiontos_winner:
                return self.auctiontos_winner.pickup_location
            else:
                # this is not sold -- allow the auctioneer to check the appropriate pickup location
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

    @property
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

    @property
    def create_update_invoices(self):
        """Call whenever ending this lot, or when creating it"""
        if self.auction and self.winner and not self.auctiontos_winner:
            tos = (
                AuctionTOS.objects.filter(Q(user=self.winner) | Q(email=self.winner.email), auction=self.auction)
                .order_by("-createdon")
                .first()
            )
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

    @property
    def category(self):
        """string of a shortened species_category.  This is for labels, usually you want to use `lot.species_category` instead"""
        if self.species_category and self.species_category.name != "Uncategorized":
            return self.species_category.name_on_label or self.species_category
        return ""

    @property
    def donation_label(self):
        if self.donation and self.auction.use_donation_field:
            return "(D)"
        return ""

    @property
    def min_bid_label(self):
        if self.reserve_price > self.auction.minimum_bid and not self.sold:
            return f"Min: ${self.reserve_price}"
        return ""

    @property
    def buy_now_label(self):
        if self.buy_now_price and not self.sold:
            return f"Buy: ${self.buy_now_price}"
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

    # @property
    # def description_cleaned(self):
    #     return re.sub(r'(style="[^"]*?)color:[^;"]*;?([^"]*")', r"\1\2", self.summernote_description)


class Invoice(models.Model):
    """
    The total amount you get paid or owe to the club for an auction
    """

    auction = models.ForeignKey(Auction, blank=True, null=True, on_delete=models.SET_NULL)
    auctiontos_user = models.ForeignKey(AuctionTOS, blank=True, on_delete=models.CASCADE, related_name="auctiontos")
    date = models.DateTimeField(auto_now_add=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=(
            ("DRAFT", "Open"),
            ("UNPAID", "Waiting for payment"),
            ("PAID", "Paid"),
        ),
        default="DRAFT",
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
        default=uuid.uuid4,
        blank=True,
        verbose_name="This link will be emailed to the user, allowing them to view their invoice directly without logging in",
    )
    calculated_total = models.IntegerField(null=True, blank=True)
    calculated_total.help_text = "This field is set automatically, you shouldn't need to manually change it"
    memo = models.CharField(max_length=500, blank=True, null=True, default="")
    memo.help_text = "Only other auction admins can see this"

    @property
    def currency(self):
        """Get the currency for this invoice based on the auction creator"""
        if self.auction and self.auction.created_by:
            return self.auction.created_by.userdata.currency
        return "USD"

    @property
    def currency_symbol(self):
        """Get the currency symbol for this invoice"""
        return get_currency_symbol(self.currency)

    @property
    def show_payment_button(self):
        """True if we can show the PayPal or Square button"""
        # Check PayPal
        paypal_configured = settings.PAYPAL_CLIENT_ID and settings.PAYPAL_SECRET
        # Square now requires OAuth - just check if OAuth is configured
        square_configured = getattr(settings, "SQUARE_APPLICATION_ID", None) and getattr(
            settings, "SQUARE_CLIENT_SECRET", None
        )

        if not (paypal_configured or square_configured):
            return False
        if self.status == "PAID":
            return False
        if self.net_after_payments >= 0:
            return False
        if not self.auction:
            # change this if we ever allow direct person to person payments
            return False

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

    @property
    def show_paypal_button(self):
        """True if we can show specifically the PayPal button"""
        if not (settings.PAYPAL_CLIENT_ID and settings.PAYPAL_SECRET):
            return False
        if self.auction and not self.auction.enable_online_payments:
            return False
        if self.auction and not self.auction.created_by.userdata.is_trusted:
            return False
        if self.status == "PAID":
            return False
        if self.net_after_payments >= 0:
            return False
        if (
            self.auction
            and not self.auction.created_by.is_superuser
            and not self.auction.created_by.userdata.paypal_enabled
            and not self.auction.paypal_information
        ):
            return False
        if not self.auction:
            return False
        return True

    @property
    def show_square_button(self):
        """True if we can show specifically the Square button
        Square requires OAuth - seller must have linked their account"""
        # Check OAuth is configured
        if not (getattr(settings, "SQUARE_APPLICATION_ID", None) and getattr(settings, "SQUARE_CLIENT_SECRET", None)):
            return False
        if self.auction and not self.auction.enable_square_payments:
            return False
        if self.auction and not self.auction.created_by.userdata.is_trusted:
            return False
        if self.status == "PAID":
            return False
        if self.net_after_payments >= 0:
            return False
        # Square requires OAuth - check if seller has linked their account
        if self.auction and not self.auction.created_by.userdata.square_enabled:
            return False
        if self.auction and not self.auction.square_information:
            return False
        if not self.auction:
            return False
        return True
        if self.auction and not self.auction.created_by.userdata.is_trusted:
            return False
        if self.status == "PAID":
            return False
        if self.net_after_payments >= 0:
            return False
        if (
            self.auction
            and not self.auction.created_by.is_superuser
            and not self.auction.created_by.userdata.square_enabled
            and not self.auction.square_information
        ):
            return False
        if not self.auction:
            return False
        return True

    @property
    def reason_for_payment_not_available(self):
        """Always use this after invoice.show_payment_button
        This assumes that the button will show up, but be grayed out
         We will return a string reason to the user"""

        if self.auction.is_online and not self.auction.closed and self.status == "DRAFT":
            timedelta = self.dynamic_end - timezone.now()
            seconds = timedelta.total_seconds()
            if seconds > 0:
                minutes = seconds // 60
                return f"This auction hasn't ended yet.  You'll be able to pay in {minutes} minutes."
        if not self.auction.is_online:
            # in person auctions, users will see a pay button on any invoice
            # even ones with online bidding enabled.  Not sure if this is a good idea or not,
            # we can change it later
            pass

    @property
    def soft_descriptor(self):
        """Used for PayPal payments -- short string describing the merchant
        https://developer.paypal.com/docs/multiparty/embedded-integration/reference/#soft-descriptors
        """
        if self.auction and self.auction.paypal_information == "admin":
            return settings.NAVBAR_BRAND
        # could add some logic here to return the club short name or the auction name
        return None

    def sum_adjusments(self, adjustment_type):
        total = self.adjustments.filter(adjustment_type=adjustment_type).aggregate(total=Sum("amount"))["total"]
        if not total:
            return 0
        return total

    @property
    def adjustments(self):
        return InvoiceAdjustment.objects.filter(invoice=self).order_by("-createdon")

    @property
    def flat_value_adjustments(self):
        return self.sum_adjusments("DISCOUNT") - self.sum_adjusments("ADD")

    @property
    def percent_value_adjustments(self):
        return self.sum_adjusments("ADD_PERCENT") - self.sum_adjusments("DISCOUNT_PERCENT")

    @property
    def changed_adjustments(self):
        return self.adjustments.exclude(amount=0)

    def recalculate(self):
        """Store the current net in the calculated_total field.

        Call this method every time you add or remove a lot from this invoice.
        This method should be called explicitly, not accessed as a property,
        as it has side effects (modifies and saves database records).
        """
        self.calculated_total = self.rounded_net
        self.save()

    @property
    def total_adjustment_amount(self):
        """There's a difference between the subtotal and the rounded net -- rounding, manual adjustments, fist bid payouts, etc"""
        return Decimal(self.subtotal) - Decimal(self.rounded_net)

    @property
    def subtotal(self):
        """don't call this directly, use self.net or another property instead"""
        return Decimal(self.total_sold) - Decimal(self.total_bought)

    @property
    def first_bid_payout(self):
        try:
            if self.auction.first_bid_payout:
                if self.lots_bought:
                    return self.auction.first_bid_payout
        except:
            pass
        return 0

    @property
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
        rate = Decimal(self.auction.tax or 0) / Decimal(100)
        tax_amount = total_final * rate
        return tax_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def net(self):
        """Factor in:
        Total bought
        Total sold
        Any auction-wide payout promotions
        Any manual adjustments made to this invoice
        """
        subtotal = Decimal(self.subtotal)
        # if this auction is using the first bid payout system to encourage people to bid
        subtotal += Decimal(self.first_bid_payout)
        subtotal += Decimal(self.flat_value_adjustments)
        subtotal += Decimal(subtotal * self.percent_value_adjustments / 100)
        subtotal -= Decimal(self.tax)
        if not subtotal:
            subtotal = 0
        return Decimal(subtotal)

    @property
    def net_after_payments(self):
        """negative number means they owe the club payment"""
        return self.net + self.total_payments

    @property
    def user_should_be_paid(self):
        """Return true if the user owes the club money.  Most invoices will be negative unless the user is a vendor"""
        if self.net > 0:
            return True
        else:
            return False

    @property
    def rounded_net(self):
        """Always round in the customer's favor (against the club) to make sure that the club doesn't need to deal with change, only whole dollar amounts"""
        if not self.auction.invoice_rounding:
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

    @property
    def absolute_amount(self):
        """Give the absolute value of the invoice's net amount"""
        return abs(self.rounded_net)

    @property
    def sold_lots_queryset(self):
        """Simple qs containing all lots SOLD by this user in this auction"""
        return add_price_info(
            Lot.objects.filter(
                auctiontos_seller=self.auctiontos_user,
                auction=self.auction,
                is_deleted=False,
            ).order_by("pk")
        )

    @property
    def bought_lots_queryset(self):
        """Simple qs containing all lots BOUGHT by this user in this auction"""
        return (
            Lot.objects.filter(
                winning_price__isnull=False,
                auctiontos_winner=self.auctiontos_user,
                is_deleted=False,
            )
            .order_by("pk")
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
            )
            .annotate(
                tax=ExpressionWrapper(
                    Cast(F("final_price"), DecimalField(max_digits=12, decimal_places=2))
                    * Cast(F("auction__tax"), DecimalField(max_digits=5, decimal_places=2))
                    / Value(Decimal("100.00")),
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                )
            )
        )

    @property
    def bought_lots_queryset_old(self):
        """Simple qs containing all lots BOUGHT by this user in this auction"""
        return (
            Lot.objects.filter(
                winning_price__isnull=False,
                auctiontos_winner=self.auctiontos_user,
                is_deleted=False,
            )
            .order_by("pk")
            .annotate(final_price=F("winning_price") * (100 - F("partial_refund_percent")) / 100)
            .annotate(tax=F("final_price") * F("auction__tax") / 100)
        )

    @property
    def sold_lots_queryset_sorted(self):
        try:
            return sorted(self.sold_lots_queryset, key=lambda t: str(t.winner_location))
        except:
            return self.sold_lots_queryset

    @property
    def lots_sold(self):
        """Return number of lots the user attempted to sell in this invoice (unsold lots included)"""
        return len(self.sold_lots_queryset)

    @property
    def lots_sold_successfully(self):
        """Queryset of lots the user sold in this invoice (unsold lots not included)"""
        return self.sold_lots_queryset.filter(auctiontos_winner__isnull=False)

    @property
    def lots_sold_successfully_count(self):
        """Return number of lots the user sold in this invoice (unsold lots not included)"""
        return self.lots_sold_successfully.count()

    @property
    def lot_labels(self):
        """For online auctions, only sold lots will have printed labels.  For in-person auctions, all submitted lots get printed"""
        if self.is_online:
            return self.lots_sold_successfully
        else:
            return self.sold_lots_queryset

    @property
    def unsold_lots(self):
        """Return number of lots the user did not sell. This may be simply lots whose winner has not been set yet."""
        return len(self.sold_lots_queryset.exclude(auctiontos_winner__isnull=False))

    @property
    def unsold_non_donation_lots(self):
        """For non-online auctions only.  Return number of lots the user did not sell. This may be simply lots whose winner has not been set yet."""
        if self.is_online:
            return 0
        # leave active = True here, this is used for the warning on the invoice page.  If you mark a lot unsold, it'll be set not active
        return self.sold_lots_queryset.filter(
            active=True, auctiontos_winner__isnull=True, donation=False, banned=False
        ).count()

    @property
    def total_sold_gross(self):
        """Total winning price of all lots sold"""
        return self.sold_lots_queryset.aggregate(total=Sum("winning_price"))["total"] or 0

    @property
    def total_sold(self):
        """Seller's cut of all lots sold"""
        return self.sold_lots_queryset.aggregate(total_sold=Sum("your_cut"))["total_sold"] or 0

    @property
    def total_sold_club_cut(self):
        """Club's cut of all lots sold"""
        return self.sold_lots_queryset.aggregate(total=Sum("club_cut"))["total"] or 0

    @property
    def lots_bought(self):
        """Return number of lots the user bought in this invoice"""
        return len(self.bought_lots_queryset)

    @property
    def total_bought(self):
        return self.bought_lots_queryset.aggregate(total_bought=Sum("final_price"))["total_bought"] or 0

    @property
    def total_donations(self):
        """Total value of all donated lots"""
        return (
            self.sold_lots_queryset.filter(winning_price__isnull=False, donation=True).aggregate(
                total=Sum("winning_price")
            )["total"]
            or 0
        )

    @property
    def location(self):
        """Pickup location selected by the user"""
        return self.auctiontos_user.pickup_location

    @property
    def contact_email(self):
        if self.location:
            if self.location.pickup_location_contact_email:
                return self.location.pickup_location_contact_email
        return self.auction.created_by.email

    @property
    def has_refunds(self):
        """Check if this invoice has any refunds (negative payment amounts)"""
        return self.payments.filter(amount__lt=0).exists()

    @property
    def rounded_net_after_payments(self):
        """
        Calculate net_after_payments with rounding for cash payments.
        When invoice_rounding is enabled and there are refunds with fractional amounts,
        round to whole dollars for display purposes.
        """
        if not self.auction or not self.auction.invoice_rounding:
            return self.net_after_payments

        # If there are refunds and the absolute amount is less than $1, round to $0
        if self.has_refunds and abs(self.net_after_payments) < 1:
            return Decimal("0.00")

        # Otherwise apply standard rounding in customer's favor
        # Note: Python's round() uses banker's rounding (round half to even)
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
                # net_after_payments is between rounded and rounded+1 (e.g., -1.5 between -2 and -1)
                return Decimal(rounded + 1)

    @property
    def rounding_adjustment(self):
        """
        Calculate the rounding adjustment to display as a line item.
        Returns None if no adjustment needed, otherwise the adjustment amount.
        """
        if not self.auction or not self.auction.invoice_rounding:
            return None

        # Only show adjustment when we have refunds and fractional amounts less than $1
        if self.has_refunds and abs(self.net_after_payments) < 1 and self.net_after_payments != 0:
            # The adjustment is the difference between exact and rounded
            return self.net_after_payments - self.rounded_net_after_payments

        return None

    @property
    def invoice_summary_short(self):
        result = ""
        # Use rounded value for display when invoice_rounding is enabled
        display_amount = (
            self.rounded_net_after_payments
            if (self.auction and self.auction.invoice_rounding)
            else self.net_after_payments
        )

        if display_amount > 0:
            result += "needs to be paid"
        else:
            result += "owes the club"
        return result + " $" + f"{abs(display_amount):.2f}"

    @property
    def invoice_summary(self):
        return f"{self.auctiontos_user.name} {self.invoice_summary_short}"

    @property
    def label(self):
        return self.auction

    def __str__(self):
        return f"{self.auctiontos_user.name}'s invoice for {self.auctiontos_user.auction}"

    def get_absolute_url(self):
        return f"/invoices/{self.pk}/"

    @property
    def is_online(self):
        """Based on the auction associated with this invoice"""
        if self.auctiontos_user:
            return self.auctiontos_user.auction.is_online
        if self.auction:
            return self.auction.is_online
        return False

    @property
    def unsold_lot_warning(self):
        if self.unsold_non_donation_lots:
            return f"{self.unsold_non_donation_lots} unsold lot(s), sell these before setting this paid"
        return ""

    @property
    def pre_register_used(self):
        return self.sold_lots_queryset.filter(pre_register_discount__gt=0).exists()

    @property
    def total_payments(self):
        """Sum of payments recorded against this invoice (Decimal)."""
        total = self.payments.aggregate(total=Coalesce(Sum("amount"), Value(Decimal("0.00"))))["total"]
        if total is None:
            return Decimal("0.00")
        # Ensure Decimal return type
        return Decimal(total)

    def save(self, *args, **kwargs):
        if not self.auction:
            self.auction = self.auctiontos_user.auction
        super().save(*args, **kwargs)


class InvoiceAdjustment(models.Model):
    """Alteration to a specific invoice"""

    invoice = models.ForeignKey("Invoice", null=True, blank=True, on_delete=models.CASCADE)
    user = models.ForeignKey(User, blank=True, null=True, on_delete=models.SET_NULL)
    user.help_text = "The auction admin who created this adjustment"
    createdon = models.DateTimeField(auto_now_add=True, blank=True)
    adjustment_type = models.CharField(
        max_length=20,
        choices=(
            ("ADD", "Charge extra"),
            ("DISCOUNT", "Discount"),
            # so confusing and not used by most users
            # ("ADD_PERCENT", "Charge extra percent"),
            # ("DISCOUNT_PERCENT", "Discount percent"),
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
        if self.adjustment_type in ["DISCOUNT", "DISCOUNT_PERCENT"]:
            result += "-"
        if self.adjustment_type in ["ADD", "DISCOUNT"]:
            # Get currency symbol from the invoice
            currency_symbol = self.invoice.currency_symbol if self.invoice else "$"
            result += f"{currency_symbol}{self.formatted_float_value}"
        else:
            result += f"{self.amount}%"
        return result


class InvoicePayment(models.Model):
    """
    Record of a payment applied to an Invoice (supports partial payments).
    Payments are kept separate from InvoiceAdjustments.
    """

    PAYMENT_STATUS = (
        ("PENDING", "Pending"),
        ("COMPLETED", "Completed"),
        ("FAILED", "Failed"),
        ("REFUNDED", "Refunded"),
    )

    invoice = models.ForeignKey("Invoice", related_name="payments", on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    amount_available_to_refund = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(max_length=10, default="USD")
    # status = models.CharField(max_length=20, choices=PAYMENT_STATUS, default="COMPLETED", db_index=True)
    external_id = models.CharField(max_length=255, blank=True, null=True, help_text="Provider transaction id")
    receipt_number = models.CharField(
        max_length=10, blank=True, null=True, help_text="Short receipt number (4 chars for Square)", db_index=True
    )
    payer_name = models.CharField(max_length=200, blank=True, null=True)
    payer_email = models.CharField(max_length=200, blank=True, null=True)
    payer_address = models.CharField(max_length=500, blank=True, null=True)
    memo = models.CharField(max_length=500, blank=True, null=True)
    # metadata = models.JSONField(blank=True, null=True)  # store provider payload if needed
    payment_method = models.CharField(
        max_length=50, blank=True, null=True, default="PayPal"
    )  # e.g. 'paypal', 'stripe', 'cash'
    createdon = models.DateTimeField(auto_now_add=True)

    # def __str__(self):
    #     return f"Payment {self.pk} for Invoice {self.invoice_id}: {self.amount} {self.currency} ({self.status})"


class Bid(models.Model):
    """Bids apply to lots"""

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    lot_number = models.ForeignKey(Lot, on_delete=models.CASCADE)
    bid_time = models.DateTimeField(auto_now_add=True, blank=True)
    last_bid_time = models.DateTimeField(auto_now_add=True, blank=True)
    amount = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    was_high_bid = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)
    # note: there is not AuctionTOS field here - this means that bids can only be placed by Users
    # AuctionTOSs CAN be declared the winners of lots without placing a single bid
    # time will tell if this is a mistake or not

    def __str__(self):
        return str(self.user) + " bid " + str(self.amount) + " on lot " + str(self.lot_number)

    def delete(self, *args, **kwargs):
        self.is_deleted = True
        self.save()


class Watch(models.Model):
    """
    Users can watch lots.
    This adds them to a list on the users page, and sends an email 2 hours before the auction ends
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    lot_number = models.ForeignKey(Lot, on_delete=models.CASCADE)
    # not doing anything with createdon field right now
    # but might be interesting to track at what point in the auction users watch lots
    createdon = models.DateTimeField(auto_now_add=True, blank=True)

    def __str__(self):
        return str(self.user) + " watching " + str(self.lot_number)

    class Meta:
        verbose_name_plural = "Users watching"


class UserBan(models.Model):
    """
    Users can ban other users from bidding on their lots
    This will prevent the banned_user from bidding on any lots or in auction auctions created by the owned user
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    banned_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="banned_user")
    createdon = models.DateTimeField(auto_now_add=True, blank=True)

    def __str__(self):
        return str(self.user) + " has banned " + str(self.banned_user)


class UserIgnoreCategory(models.Model):
    """
    Users can choose to hide all lots from all views
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    category = models.ForeignKey(Category, on_delete=models.CASCADE)
    createdon = models.DateTimeField(auto_now_add=True, blank=True)

    def __str__(self):
        return str(self.user) + " hates " + str(self.category)


class PageView(models.Model):
    """Track what lots a user views"""

    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    auction = models.ForeignKey(Auction, null=True, blank=True, on_delete=models.CASCADE)
    auction.help_text = "Only filled out when a user views an auction's rules page"
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

    @property
    def duplicates(self):
        """Some duplciates have appeared and I can't figure out how it's possible"""
        return PageView.objects.filter(
            user=self.user,
            lot_number=self.lot_number,
            url=self.url,
            auction=self.auction,
            session_id=self.session_id,
        ).exclude(pk=self.pk)

    @property
    def duplicate_count(self):
        return self.duplicates.count()

    def merge_and_delete_duplicate(self):
        """Merge duplicate PageView records and delete the duplicate.

        This method should be called explicitly, not accessed as a property,
        as it has side effects (modifies and deletes database records).
        """
        if self.duplicate_count:
            dup = self.duplicates.first()
            if self.date_start > dup.date_start:
                self.date_start = dup.date_start
            if self.date_end and dup.date_end:
                if self.date_end < dup.date_end:
                    self.date_end = dup.date_end
            self.total_time = self.total_time + dup.total_time
            if not self.source:
                self.source = dup.source
            self.counter = self.counter + dup.counter
            if dup.notification_sent:
                self.notification_sent = True
            if not self.title:
                self.title = dup.title
            if not self.referrer:
                self.referrer = dup.referrer
            self.save()
            dup.delete()

    def save(self, *args, **kwargs):
        if not self.latitude and self.ip_address:
            other_view_with_same_ip = (
                PageView.objects.exclude(latitude=0, longitude=0)
                .filter(ip_address=self.ip_address)
                .order_by("-date_start")
                .first()
            )
            if other_view_with_same_ip:
                self.latitude = other_view_with_same_ip.latitude
                self.longitude = other_view_with_same_ip.longitude
            elif self.user:
                if self.user.userdata.latitude:
                    self.latitude = self.user.userdata.latitude
                    self.longitude = self.user.userdata.longitude
        super().save(*args, **kwargs)


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


def get_default_can_create_auctions():
    return settings.ALLOW_USERS_TO_CREATE_AUCTIONS
    # return getattr(settings, "ALLOW_USERS_TO_CREATE_AUCTIONS", True)


def get_default_can_submit_lots():
    return settings.ALLOW_USERS_TO_CREATE_LOTS
    # return getattr(settings, "ALLOW_USERS_TO_CREATE_LOTS", True)


def get_default_paypal_enabled():
    return settings.PAYPAL_ENABLED_FOR_USERS


def get_default_square_enabled():
    return getattr(settings, "SQUARE_ENABLED_FOR_USERS", False)


def get_default_is_trusted():
    return settings.USERS_ARE_TRUSTED_BY_DEFAULT


class UserData(models.Model):
    """
    Extension of user model to store additional info
    """

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
        null=True, blank=True, default=100, verbose_name="New online auction distance"
    )
    email_me_about_new_auctions_distance.help_text = "miles, from your address"
    email_me_about_new_in_person_auctions = models.BooleanField(default=True, blank=True)
    email_me_about_new_in_person_auctions.help_text = (
        "When new in-person auctions are created near my location, notify me"
    )
    email_me_about_new_in_person_auctions_distance = models.PositiveIntegerField(
        null=True,
        blank=True,
        default=100,
        verbose_name="New in-person auction distance",
    )
    email_me_about_new_in_person_auctions_distance.help_text = "miles, from your address"
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
    paypal_email_address = models.CharField(max_length=200, blank=True, null=True, verbose_name="PayPal Address")
    paypal_email_address.help_text = "If different from your email address"
    unsubscribe_link = models.CharField(max_length=255, default=uuid.uuid4, blank=True)
    has_unsubscribed = models.BooleanField(default=False, blank=True)
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
    username_is_email_warning_sent = models.BooleanField(default=False, blank=True)
    username_is_email_warning_sent.help_text = (
        "Warning email has been sent because this user made their username an email"
    )
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

    @property
    def last_auction_created(self):
        return Auction.objects.filter(created_by=self.user).order_by("-date_posted").first()

    @property
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

    def send_websocket_message(self, message):
        channel_layer = channels.layers.get_channel_layer()
        async_to_sync(channel_layer.group_send)(f"user_{self.user.pk}", message)

    @property
    def my_lots_qs(self):
        """All lots this user submitted, whether in an auction, or independently"""
        return Lot.objects.filter(Q(user=self.user) | Q(auctiontos_seller__user=self.user)).exclude(is_deleted=True)

    @property
    def lots_submitted(self):
        """All lots this user has submitted, including unsold"""
        return self.my_lots_qs.count()

    @property
    def lots_sold(self):
        """All lots this user has sold"""
        return self.my_lots_qs.filter(winner__isnull=False).count()

    @property
    def total_sold(self):
        """Total amount this user has sold on this site"""
        total = 0
        for lot in self.my_lots_qs.filter(winning_price__isnull=False):
            total += lot.winning_price
        return total

    @property
    def species_sold(self):
        """Total different species that this user has bred and sold in auctions"""
        logger.warning(
            "species_sold is is no longer used, there's no way for users to enter species information anymore"
        )
        allLots = (
            self.my_lots_qs.filter(i_bred_this_fish=True, winner__isnull=False).values("species").distinct().count()
        )
        return allLots

    @property
    def my_won_lots_qs(self):
        """All lots won by this user, in an auction or independently"""
        return Lot.objects.filter(
            Q(winner=self.user) | Q(auctiontos_winner__user=self.user),
            winning_price__isnull=False,
        ).exclude(is_deleted=True)

    @property
    def lots_bought(self):
        """Total number of lots this user has purchased"""
        return self.my_won_lots_qs.count()

    @property
    def lots_bought_online(self):
        """Total number of lots this user has purchased only in online auctions"""
        return self.my_won_lots_qs.filter(auction__is_online=True).count()

    @property
    def total_spent(self):
        """Total amount this user has spent on this site"""
        total = 0
        for lot in self.my_won_lots_qs:
            total += lot.winning_price
        return total

    @property
    def calc_total_volume(self):
        """Bought + sold"""
        return self.total_spent + self.total_sold

    @property
    def total_bids(self):
        """Total number of successful bids this user has placed (max one per lot)"""
        # return len(Bid.objects.filter(user=self.user, was_high_bid=True))
        return len(Bid.objects.exclude(is_deleted=True).filter(user=self.user))

    @property
    def lots_viewed(self):
        """Total lots viewed by this user"""
        return len(PageView.objects.filter(user=self.user.pk))

    @property
    def bought_to_sold(self):
        """Ratio of lots bought to lots sold"""
        if self.lots_sold:
            return self.lots_bought / self.lots_sold
        else:
            return 0

    @property
    def bid_to_view(self):
        """Ratio of lots viewed to lots bought.  Lower number is indicative of tire kicking, higher number means business"""
        if self.lots_viewed:
            return self.total_bids / self.lots_viewed
        else:
            return 0

    @property
    def viewed_to_sold(self):
        """Ratio of lots viewed to lots sold"""
        if self.lots_viewed:
            return self.lots_sold / self.lots_viewed
        else:
            return 0

    @property
    def dedication(self):
        """Ratio of bids to won lots, only for online auctions"""
        if self.lots_bought_online and self.total_bids:
            return self.lots_bought_online / self.total_bids
        else:
            return 0

    @property
    def percent_success(self):
        """Ratio of bids to won lots, formatted"""
        return self.dedication * 100

    @property
    def positive_feedback_as_seller(self):
        return self.my_lots_qs.filter(feedback_rating=1).count()

    @property
    def negative_feedback_as_seller(self):
        return self.my_lots_qs.filter(feedback_rating=-1).count()

    @property
    def percent_positive_feedback_as_seller(self):
        positive = self.positive_feedback_as_seller
        negative = self.negative_feedback_as_seller
        if not negative:
            return 100
        return int((positive / (positive + negative)) * 100)

    @property
    def positive_feedback_as_winner(self):
        return self.my_won_lots_qs.filter(winner_feedback_rating=1).count()

    @property
    def negative_feedback_as_winner(self):
        return self.my_won_lots_qs.filter(winner_feedback_rating=-1).count()

    @property
    def percent_positive_feedback_as_winner(self):
        positive = self.positive_feedback_as_winner
        negative = self.negative_feedback_as_winner
        if not negative:
            return 100
        return int((positive / (positive + negative)) * 100)

    @property
    def auctions_created(self):
        return Auction.objects.filter(created_by__pk=self.user.pk).count()

    @property
    def auctions_admined(self):
        return Auction.objects.filter(auctiontos__email=self.user.email, auctiontos__is_admin=True).count()

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
        """For templates: A list of all subscriptions annotated with a `new_message_count` property"""
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
            subscriptions = ChatSubscription.objects.exclude(lot__user=self.user).filter(
                user=self.user, unsubscribed=False
            )
            for subscription in subscriptions:
                subscription.unsubscribed = True
                subscription.save()
        super().save(*args, **kwargs)

    @property
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

    @property
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


class PayPalSeller(models.Model):
    """Extension of user model to store PayPal info for sellers
    Initially it seemed like there would be a lot of data here so I created a model for it
    but the reality is there's basically just one field
    Still, this is at least easy to delete in a callback if the user disconnects their PayPal account
    """

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
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
        return result

    def delete(self):
        auctions = Auction.objects.filter(created_by=self.user, enable_online_payments=True)
        for auction in auctions:
            auction.create_history(
                applies_to="INVOICES",
                action=f"PayPal partner consent from {self.payer_email} has been revoked.  Relink your PayPal account to re-enable payments.",
                user=None,
            )
            auction.enable_online_payments = False
            auction.save()
        return super().delete()


class SquareSeller(models.Model):
    """Extension of user model to store Square info for sellers
    Similar to PayPalSeller, stores Square merchant information and OAuth tokens
    OAuth tokens are encrypted at rest for security using django-encrypted-model-fields
    """

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    square_merchant_id = models.CharField(max_length=64, blank=True, null=True)
    access_token = EncryptedCharField(max_length=500, blank=True, null=True)
    refresh_token = EncryptedCharField(max_length=500, blank=True, null=True)
    token_expires_at = models.DateTimeField(blank=True, null=True)
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
        return result

    def is_token_expired(self):
        """Check if the access token is expired or will expire soon (within 1 hour)"""
        if not self.token_expires_at:
            return False
        from datetime import timedelta

        buffer_time = timedelta(hours=1)
        return timezone.now() + buffer_time >= self.token_expires_at

    def refresh_access_token(self):
        """Refresh the Square access token using the refresh token
        Returns True if successful, False otherwise
        """
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
            # Square returns the same refresh_token in code flow, new one in PKCE flow
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
        """Get a valid access token, refreshing if necessary
        Returns the access token or None if unable to get a valid token
        """
        if self.is_token_expired():
            logger.info("Square token expired for user %s, attempting refresh", self.user.pk)
            if not self.refresh_access_token():
                logger.error("Failed to refresh Square token for user %s", self.user.pk)
                return None
        return self.access_token

    def get_square_client(self):
        """Initialize and return Square SDK client using this seller's OAuth token
        Returns Square client or None if token is invalid
        """
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
        """Get the first active location ID for this merchant
        Returns location_id string or None if no active location found
        """
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

    def create_payment_link(self, invoice, request):
        """Create a Square payment link for the given invoice
        Args:
            invoice: Invoice object to create payment for
            request: HttpRequest object for building redirect URL
        Returns:
            tuple: (payment_url, error_message) - payment_url is None if error occurs
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

            amount_decimal = Decimal("0.00") - Decimal(invoice.net_after_payments)
            amount_cents = int(max(amount_decimal, Decimal("0.00")) * 100)
        except Exception:
            logger.exception("Failed to compute payment amount for invoice %s", invoice.pk)
            return None, "Failed to calculate payment amount"

        if amount_cents <= 0:
            logger.error("Computed amount invalid for invoice %s: %s cents", invoice.pk, amount_cents)
            return None, "Invalid payment amount"

        try:
            import uuid

            from django.urls import reverse

            payment_note = f"Bidder {invoice.auctiontos_user.bidder_number} in {invoice.auction.title}"[:500]

            # Get and validate buyer email
            buyer_email = getattr(getattr(invoice, "auctiontos_user", None), "email", None)

            # Validate email domain - Square blocks certain domains like example.com
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

            # Pre-populate buyer info from auctiontos
            # Note: These are hints for the Square checkout form and users can edit them.
            # String truncation is used to meet Square API field length limits.
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
                # Add phone number if available (20 char limit per Square API)
                if invoice.auctiontos_user.phone_number:
                    pre_populated_data["buyer_phone_number"] = invoice.auctiontos_user.phone_number[:20]
                # Add address if available (500 char limit per Square API)
                if invoice.auctiontos_user.address:
                    pre_populated_data["buyer_address"] = {
                        "address_line_1": invoice.auctiontos_user.address[:500],
                    }

            link_resp = client.checkout.payment_links.create(
                idempotency_key=str(uuid.uuid4()),
                checkout_options={
                    "redirect_url": request.build_absolute_uri(
                        reverse("square_payment_success", kwargs={"uuid": invoice.no_login_link})
                    ),
                    "ask_for_shipping_address": ask_for_shipping_address,
                },
                pre_populated_data=pre_populated_data if pre_populated_data else {},
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
                    elif error_detail:
                        error_msg = f"Square error: {error_detail}"
            return None, error_msg

    def process_refund(self, payment, refund_amount, reason):
        """Process a Square refund
        Args:
            payment: InvoicePayment object with external_id for the payment
            refund_amount: Decimal amount to refund
            reason: String reason for the refund
        Returns:
            Error message string or None on success
        """
        client = self.get_square_client()
        if not client:
            return "Failed to initialize Square client"

        try:
            import uuid
            from decimal import Decimal

            # Convert amount to cents
            refund_amount_cents = int(Decimal(str(refund_amount)) * 100)

            client.refunds.refund_payment(
                payment_id=payment.external_id,
                idempotency_key=str(uuid.uuid4()),
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

    def delete(self):
        auctions = Auction.objects.filter(created_by=self.user, enable_square_payments=True)
        for auction in auctions:
            auction.create_history(
                applies_to="INVOICES",
                action=f"Square account from {self.payer_email or self.user} has been disconnected. Relink your Square account to re-enable payments.",
                user=None,
            )
            auction.enable_square_payments = False
            auction.save()
        return super().delete()


class UserInterestCategory(models.Model):
    """
    How interested is a user in a given category
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    category = models.ForeignKey(Category, on_delete=models.CASCADE)
    interest = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)])
    as_percent = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])

    def __str__(self):
        return f"{self.user} interest level in {self.category} is {self.as_percent}"

    def save(self, *args, **kwargs):
        """
        Normalize the user's interest in a category relative to all of this user's interests
        """
        try:
            maxInterest = UserInterestCategory.objects.filter(user=self.user).order_by("-interest")[0].interest
            self.as_percent = int(((self.interest + 1) / maxInterest) * 100)  # + 1 for the times maxInterest is 0
            if self.as_percent > 100:
                self.as_percent = 100
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
    current_price = models.PositiveIntegerField(null=True, blank=True)
    current_price.help_text = "Price of the lot immediately AFTER this message"
    changed_price = models.BooleanField(default=False)
    changed_price.help_text = (
        "Was this a bid that changed the price?  If False, this lot will show up in the admin chat system"
    )
    notification_sent = models.BooleanField(default=False)
    notification_sent.help_text = "Set to true automatically when the notification email is sent"
    bid_amount = models.PositiveIntegerField(null=True, blank=True)
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
            ("LOT_WINNERS", "Set lot winners"),
        ),
    )

    def __str__(self):
        if self.user:
            return f"{self.user.first_name} {self.user.last_name} {self.action}"
        else:
            return f"System {self.action}"


class AdCampaignGroup(models.Model):
    title = models.CharField(max_length=100, default="Untitled campaign")
    contact_user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    paid = models.BooleanField(default=False)
    total_cost = models.FloatField(default=0)

    def __str__(self):
        return f"{self.title}"

    @property
    def number_of_clicks(self):
        """..."""
        return AdCampaignResponse.objects.filter(campaign__campaign_group=self.pk, clicked=True).count()

    @property
    def number_of_impressions(self):
        """How many times ads in this campaign group have been viewed"""
        return AdCampaignResponse.objects.filter(campaign__campaign_group=self.pk).count()

    @property
    def click_rate(self):
        """What percent of views result in a click"""
        return (self.number_of_clicks / (self.number_of_impressions + 1)) * 100

    @property
    def number_of_campaigns(self):
        """How many campaigns are there in this group"""
        return AdCampaign.objects.filter(campaign_group=self.pk).count()


class AdCampaign(models.Model):
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

    @property
    def number_of_clicks(self):
        """..."""
        return AdCampaignResponse.objects.filter(campaign=self.pk, clicked=True).count()

    @property
    def number_of_impressions(self):
        """How many times this ad has been viewed"""
        return AdCampaignResponse.objects.filter(campaign=self.pk).count()

    @property
    def click_rate(self):
        """What percent of views result in a click"""
        return (self.number_of_clicks / (self.number_of_impressions + 1)) * 100


class AdCampaignResponse(models.Model):
    campaign = models.ForeignKey(AdCampaign, on_delete=models.CASCADE)
    responseid = models.CharField(max_length=255, default=uuid.uuid4, blank=True)
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


class AuctionCampaign(models.Model):
    auction = models.ForeignKey(Auction, null=True, blank=True, on_delete=models.SET_NULL)
    uuid = models.CharField(max_length=255, default=uuid.uuid4, blank=True)
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

    @property
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


class LotImage(models.Model):
    """An image that belongs to a lot.  Each lot can have multiple images"""

    PIC_CATEGORIES = (
        ("ACTUAL", "This picture is of the exact item"),
        (
            "REPRESENTATIVE",
            "This is my picture, but it's not of this exact item.  e.x. This is the parents of these fry",
        ),
        ("RANDOM", "This picture is from the internet"),
    )
    lot_number = models.ForeignKey(Lot, on_delete=models.CASCADE)
    caption = models.CharField(max_length=60, blank=True, null=True)
    caption.help_text = "Optional"
    image = ThumbnailerImageField(
        upload_to="images/",
        blank=False,
        null=False,
        resize_source={"size": (600, 600), "quality": 85},
    )
    image.help_text = "Select an image to upload"
    image_source = models.CharField(max_length=20, choices=PIC_CATEGORIES, blank=True)
    is_primary = models.BooleanField(default=False, blank=True)
    createdon = models.DateTimeField(auto_now_add=True)


class FAQ(models.Model):
    """Questions...constantly questions.  Maintained in the admin site, and used only on the FAQ page"""

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


class SearchHistory(models.Model):
    """To keep track of what people are searching for"""

    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    search = models.CharField(max_length=600)
    createdon = models.DateTimeField(auto_now_add=True)
    auction = models.ForeignKey(Auction, null=True, blank=True, on_delete=models.SET_NULL)


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
