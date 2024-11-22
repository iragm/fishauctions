import datetime
import logging
import re
import uuid
from datetime import time
from random import randint

import channels.layers
from asgiref.sync import async_to_sync
from autoslug import AutoSlugField
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.signals import user_logged_in
from django.contrib.sites.models import Site
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import (
    Case,
    Count,
    ExpressionWrapper,
    F,
    FloatField,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.expressions import RawSQL
from django.db.models.functions import Coalesce
from django.db.models.query import QuerySet
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.urls import reverse
from django.utils import html, timezone
from django.utils.safestring import mark_safe
from django_ses.signals import bounce_received, complaint_received
from easy_thumbnails.fields import ThumbnailerImageField
from location_field.models.plain import PlainLocationField
from markdownfield.models import MarkdownField, RenderedMarkdownField
from markdownfield.validators import VALIDATOR_STANDARD
from pytz import timezone as pytz_timezone

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
    elif user and not include_already_joined:
        locations = locations.exclude(auction__auctiontos__user=user)
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
        lots_bid=Case(When(Q(manually_added=True), then=Value(0)), default=F("lots_bid_actual")),
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
        lots_viewed=Case(When(Q(manually_added=True), then=Value(0)), default=F("lots_viewed_actual")),
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
                Q(manually_added=True),
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
            When(Q(manually_added=True), then=Value(0)),
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
    return (
        qs.select_related("user__userdata")
        .select_related("pickup_location")
        .annotate(
            new_distance_traveled=Case(
                When(Q(manually_added=True), then=Value(-1)),
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
        .exclude(species_category__pk=21)
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

    name = models.CharField(max_length=255)
    abbreviation = models.CharField(max_length=255, blank=True, null=True)
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
    """A species or item in the auction"""

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
    promote_this_auction.help_text = "Show this to everyone in the list of auctions. <span class='text-warning'>Uncheck if this is a test or private auction</span>."
    is_chat_allowed = models.BooleanField(default=True)
    max_lots_per_user = models.PositiveIntegerField(null=True, blank=True, validators=[MaxValueValidator(100)])
    max_lots_per_user.help_text = "A user won't be able to add more than this many lots to this auction"
    allow_additional_lots_as_donation = models.BooleanField(default=True)
    allow_additional_lots_as_donation.help_text = "If you don't set max lots per user, this has no effect"
    email_first_sent = models.BooleanField(default=False)
    email_second_sent = models.BooleanField(default=False)
    email_third_sent = models.BooleanField(default=False)
    email_fourth_sent = models.BooleanField(default=False)
    email_fifth_sent = models.BooleanField(default=False)
    reprint_reminder_sent = models.BooleanField(default=False)
    make_stats_public = models.BooleanField(default=True)
    make_stats_public.help_text = "Allow any user who has a link to this auction's stats to see them.  Uncheck to only allow the auction creator to view stats"
    bump_cost = models.PositiveIntegerField(blank=True, default=1, validators=[MinValueValidator(1)])
    bump_cost.help_text = "The amount a user will be charged each time they move a lot to the top of the list"
    use_categories = models.BooleanField(default=True, verbose_name="This is a fish auction")
    use_categories.help_text = "Check to use categories like Cichlids, Livebearers, etc."
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
    lot_entry_fee_for_club_members.help_text = (
        "Used instead of the standard entry fee, when you designate someone as a club member"
    )
    winning_bid_percent_to_club_for_club_members = models.PositiveIntegerField(
        default=0, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    winning_bid_percent_to_club_for_club_members.help_text = (
        "Used instead of the standard split, when you designate someone as a club member"
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
    extra_promo_text = models.CharField(max_length=50, default="", blank=True, null=True)
    extra_promo_link = models.URLField(blank=True, null=True)
    allow_deleting_bids = models.BooleanField(default=False, blank=True)
    allow_deleting_bids.help_text = "Allow users to delete their own bids until the auction ends"
    auto_add_images = models.BooleanField("Automatically add images to lots", default=True, blank=True)
    auto_add_images.help_text = (
        "Images taken from older lots with the same name in any auctions created by you or other admins"
    )
    message_users_when_lots_sell = models.BooleanField(default=True, blank=True)
    message_users_when_lots_sell.help_text = (
        "When you enter a lot number on the set lot winners screen, send a notification to any users watching that lot"
    )
    label_print_fields = models.CharField(
        max_length=1000,
        blank=True,
        null=True,
        default="qr_code,lot_name,min_bid_label,buy_now_label,quantity_label,seller_name,donation_label",
    )

    def __str__(self):
        result = self.title
        if "auction" not in self.title.lower():
            result += " auction"
        if not self.title.lower().startswith("the "):
            result = "The " + result
        return result

    def delete(self, *args, **kwargs):
        self.is_deleted = True
        self.save()

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
            invoice.recalculate
            invoice.save()

    @property
    def number_of_confirmed_tos(self):
        """How many people selected a pickup location in this auction"""
        return AuctionTOS.objects.filter(auction=self.pk).count()

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
    def template_lot_link(self):
        """Not directly used in templates, use template_lot_link_first_column and template_lot_link_separate_column instead"""
        if timezone.now() > self.lot_submission_start_date:
            result = f"<a href='{ self.view_lot_link }'>View lots</a>"
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
    def number_of_tos(self):
        """This will return users, ignoring any auctiontos without a user set"""
        return AuctionTOS.objects.filter(auction=self.pk).count()

    @property
    def preregistered_users(self):
        return AuctionTOS.objects.filter(auction=self.pk, manually_added=False).count()

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
        if self.number_of_tos > 1:
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
    )
    # yes we are using a string to store a number
    # this is actually important because some day, someone will ask to make the bidder numbers have characters like "1-234" or people's names
    bidder_number = models.CharField(max_length=20, default="", blank=True)
    bidder_number.help_text = "Must be unique, blank to automatically generate"
    bidding_allowed = models.BooleanField(default=True, blank=True)
    selling_allowed = models.BooleanField(default=True, blank=True)
    name = models.CharField(max_length=181, null=True, blank=True)
    email = models.EmailField(null=True, blank=True)
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

    @property
    def phone_as_string(self):
        """Add proper dashes to phone"""
        if not self.phone_number:
            return ""
        n = re.sub("[^0-9]", "", self.phone_number)
        return format(int(n[:-1]), ",").replace(",", "-") + n[-1]

    @property
    def bulk_add_link_html(self):
        """Link to add multiple lots at once for this user"""
        url = reverse(
            "bulk_add_lots",
            kwargs={"bidder_number": self.bidder_number, "slug": self.auction.slug},
        )
        if not self.selling_allowed:
            icon = '<i class="text-danger me-1 bi bi-cash-coin" title="Selling not allowed"></i>'
        else:
            icon = "<i class='bi bi-calendar-plus me-1'></i>"
        return html.format_html(f"<a href='{url}' hx-noget>{icon} Add lots</a>")

    @property
    def bought_lots_qs(self):
        lots = Lot.objects.exclude(is_deleted=True).filter(auctiontos_winner=self.pk)
        return lots

    @property
    def lots_qs(self):
        lots = Lot.objects.exclude(is_deleted=True).filter(auctiontos_seller=self.pk)
        return lots

    @property
    def unbanned_lot_qs(self):
        return self.lots_qs.exclude(banned=True)

    @property
    def unbanned_lot_count(self):
        return self.unbanned_lot_qs.count()

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
        result = f"""<button type='button' class='btn btn-sm btn-secondary dropdown-toggle dropdown-toggle-split' data-bs-toggle='dropdown'
        aria-haspopup='true' aria-expanded='false'>Actions </button>
        <div class = "dropdown-menu" id='actions_dropdown'>
        <span class='dropdown-item'>{self.bulk_add_link_html}</span>"""
        if self.invoice_link_html:
            result += f"<span class='dropdown-item'>{self.invoice_link_html}</span>"
        if self.print_labels_link_html:
            result += f"<span class='dropdown-item'>{self.print_labels_link_html}</span>"
        if self.print_unprinted_labels_link_html:
            result += f"<span class='dropdown-item'>{self.print_unprinted_labels_link_html}</span>"
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
        result += f"<span class='dropdown-item'><a href={bulk_add_images_url}><i class='bi bi-file-image me-1'></i>Quick add images</a></span>"
        result += "</div>"
        return html.format_html(result)

    @property
    def invoice(self):
        return Invoice.objects.filter(auctiontos_user=self.pk).first()

    @property
    def invoice_link_html(self):
        """HTML snippet with a link to the invoice for this auctionTOS, if set.  Otherwise, empty"""
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
            return ""

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
        # fill out some fields from user, if set
        # There is a huge security concern here:   <<<< ATTENTION!!!
        # If someone creates an auction and adds every email address that's public
        # We must avoid allowing them to collect addresses/phone numbers/locations from these people
        # Having this code below run only on creation means that the user won't be filled out and prevents collecting data
        # if making changes, remember that there's user_logged_in_callback below which sets the user field
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
            last_number_used = (
                AuctionTOS.objects.filter(auction__created_by=self.auction.created_by, email=self.email)
                .order_by("-auction__date_posted")
                .first()
            )
            if self.email and last_number_used and check_number_in_auction(last_number_used.bidder_number) == 0:
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
                    userData, created = UserData.objects.get_or_create(
                        user=self.user,
                        defaults={},
                    )
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
        super().save(*args, **kwargs)

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
                userData, created = UserData.objects.get_or_create(
                    user=self.user,
                    defaults={},
                )
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
            userData, created = UserData.objects.get_or_create(
                user=self.user,
                defaults={},
            )
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
            userData, created = UserData.objects.get_or_create(
                user=self.user,
                defaults={},
            )
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
            userData, created = UserData.objects.get_or_create(
                user=self.user,
                defaults={},
            )
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
    lot_number = models.AutoField(primary_key=True)
    custom_lot_number = models.CharField(max_length=9, blank=True, null=True, verbose_name="Lot number")
    custom_lot_number.help_text = "You can override the default lot number with this"
    lot_name = models.CharField(max_length=40)
    slug = AutoSlugField(populate_from="lot_name", unique=False)
    lot_name.help_text = "Short description of this lot"
    image = ThumbnailerImageField(upload_to="images/", blank=True)
    image.help_text = "Optional.  Add a picture of the item here."
    image_source = models.CharField(max_length=20, choices=PIC_CATEGORIES, blank=True)
    image_source.help_text = "Where did you get this image?"
    i_bred_this_fish = models.BooleanField(default=False, verbose_name="I bred this fish/propagated this plant")
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
    )
    active = models.BooleanField(default=True)
    winning_price = models.PositiveIntegerField(null=True, blank=True)
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
    latitude = models.FloatField(blank=True, null=True)
    longitude = models.FloatField(blank=True, null=True)
    address = models.CharField(max_length=500, blank=True, null=True)

    # Payment and shipping options, populated from last submitted lot
    # Only show these fields if auction is set to none
    payment_paypal = models.BooleanField(default=False, verbose_name="Paypal accepted")
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
    max_bid_revealed_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="max_bid_revealed_by"
    )

    def save(self, *args, **kwargs):
        """
        For in-person auctions, we'll generate a bidder_number-lot_number format
        """
        if not self.custom_lot_number and self.auction:
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
        if self.species_category:
            if self.species_category.pk == 21:
                fix_category = True
        if not self.species_category:
            fix_category = True
        if self.category_checked:
            fix_category = False
        if fix_category:
            self.category_checked = True
            if self.auction:
                if self.auction.use_categories:
                    result = guess_category(self.lot_name)
                    if result:
                        self.species_category = result
                        self.category_automatically_added = True
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

    def __str__(self):
        return "" + str(self.lot_number_display) + " - " + self.lot_name

    def add_winner_message(self, user, tos, winning_price):
        """Create a lot history message when a winner is declared (or changed)
        It's critical that this function is called every time the winner is changed so that invoices get recalculated"""
        message = f"{user.username} has set bidder {tos} as the winner of this lot (${winning_price})"
        LotHistory.objects.create(
            lot=self,
            user=user,
            message=message,
            notification_sent=True,
            bid_amount=winning_price,
            changed_price=True,
            seen=True,
        )
        invoice, created = Invoice.objects.get_or_create(auctiontos_user=tos, auction=self.auction, defaults={})
        invoice.recalculate
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

    def refund(self, amount, user, message=None):
        """Call this to add a message when refunding a lot"""
        if amount and amount != self.partial_refund_percent:
            if not message:
                message = f"{user} has issued a {amount}% refund on this lot."
            LotHistory.objects.create(lot=self, user=user, message=message, changed_price=True)
        self.partial_refund_percent = amount
        self.save()

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
        try:
            if self.auctiontos_seller:
                invoice = Invoice.objects.get(auctiontos_user=self.auctiontos_seller)
                return f"/invoices/{invoice.pk}"
        except:
            pass
        try:
            if self.user:
                invoice = Invoice.objects.get(user=self.user, auction=self.auction)
                return f"/invoices/{invoice.pk}"
        except:
            pass
        return ""

    @property
    def winner_invoice_link(self):
        """/invoices/123 for the auction/winner of this lot"""
        try:
            if self.auctiontos_winner:
                invoice = Invoice.objects.get(auctiontos_user=self.auctiontos_winner)
                return f"/invoices/{invoice.pk}"
        except:
            pass
        try:
            if self.winner:
                invoice = Invoice.objects.get(user=self.winner, auction=self.auction)
                return f"/invoices/{invoice.pk}"
        except:
            pass
        return ""

    @property
    def tos_needed(self):
        if not self.auction:
            return False
        if self.auctiontos_seller:
            return False
        try:
            AuctionTOS.objects.get(user=self.user, auction=self.auction)
            return False
        except:
            return f"/auctions/{self.auction.slug}"

    @property
    def winner_location(self):
        """String of location of the winner for this lot"""
        try:
            return str(self.auctiontos_winner.pickup_location)
        except:
            pass
        try:
            return str(AuctionTOS.objects.get(user=self.winner, auction=self.auction).pickup_location)
        except:
            pass
        return ""

    @property
    def location_as_object(self):
        """Pickup location of the seller"""
        try:
            return self.auctiontos_seller.pickup_location
        except:
            pass
        try:
            return AuctionTOS.objects.get(user=self.user, auction=self.auction).pickup_location
        except:
            pass
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
            userData, userdataCreated = UserData.objects.get_or_create(
                user=self.high_bidder,
                defaults={},
            )
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
                hx-get="{reverse('auction_show_high_bidder', kwargs={'pk':self.pk})}"
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
            userData, created = UserData.objects.get_or_create(
                user=self.winner,
                defaults={},
            )
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
            return f"{self.high_bidder_for_admins} is now the winner of lot {self.custom_lot_number} for ${self.winning_price}"
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
            if not self.auction.is_online and timezone.now() > self.auction.date_online_bidding_ends:
                return "Online bidding has ended for this auction"
            if not self.auction.is_online and timezone.now() < self.auction.date_online_bidding_starts:
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
        return self.custom_lot_number or self.lot_number

    @property
    def lot_link(self):
        """Simplest link to access this lot with"""
        if self.custom_lot_number and self.auction:
            return f"/auctions/{self.auction.slug}/lots/{self.custom_lot_number}/{self.slug}/"
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
            tos = AuctionTOS.objects.filter(auction=self.auction, user=self.winner).first()
            self.auctiontos_winner = tos
            self.save()
        if self.auction and self.auctiontos_winner:
            invoice, created = Invoice.objects.get_or_create(
                auctiontos_user=self.auctiontos_winner,
                auction=self.auction,
                defaults={},
            )
            invoice.recalculate
        if self.auction and self.auctiontos_seller:
            invoice, created = Invoice.objects.get_or_create(
                auctiontos_user=self.auctiontos_seller,
                auction=self.auction,
                defaults={},
            )
            invoice.recalculate

    @property
    def category(self):
        """string of a shortened species_category.  For labels, usually you want to use `lot.species_category` instead"""
        if self.species_category and self.species_category.pk != 21:
            return self.species_category.name_on_label or self.species_category
        return ""

    @property
    def donation_label(self):
        return "(D)" if self.donation else ""

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
        if self.auction.advanced_lot_adding or self.quantity > 1:
            return f"QTY: {self.quantity}"
        return ""

    @property
    def auction_date(self):
        return self.auction.date_start.strftime("%b %Y")

    @property
    def description_label(self):
        """Strip all html except <br> from summernote description"""
        return re.sub(r"(?!<br\s*/?>)<.*?>", "", self.summernote_description)

    @property
    def description_cleaned(self):
        return re.sub(r'(style="[^"]*?)color:[^;"]*;?([^"]*")', r"\1\2", self.summernote_description)


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

    @property
    def recalculate(self):
        """Store the current net in the calculated_total field.  Call this every time you add or remove a lot from this invoice"""
        self.calculated_total = self.rounded_net
        self.save()

    @property
    def total_adjustment_amount(self):
        """There's a difference between the subtotal and the rounded net -- rounding, manual adjustments, fist bid payouts, etc"""
        return self.subtotal - self.rounded_net

    @property
    def subtotal(self):
        """don't call this directly, use self.net or another property instead"""
        return self.total_sold - self.total_bought

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
        if self.auctiontos_user.auction and self.auctiontos_user.auction.tax:
            return self.total_bought * self.auctiontos_user.auction.tax / 100
        return 0

    @property
    def net(self):
        """Factor in:
        Total bought
        Total sold
        Any auction-wide payout promotions
        Any manual adjustments made to this invoice
        """
        subtotal = self.subtotal
        # if this auction is using the first bid payout system to encourage people to bid
        subtotal += self.first_bid_payout
        subtotal += self.flat_value_adjustments
        subtotal += subtotal * self.percent_value_adjustments / 100
        subtotal -= self.tax
        if not subtotal:
            subtotal = 0
        return subtotal

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
                return rounded + 1
            else:
                return rounded
        else:
            if self.net <= rounded:
                return rounded
            else:
                return rounded + 1

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
            .annotate(final_price=F("winning_price") * (100 - F("partial_refund_percent")) / 100)
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
    def invoice_summary_short(self):
        result = ""
        if self.user_should_be_paid:
            result += "needs to be paid"
        else:
            result += "owes the club"
        return result + " $" + f"{self.absolute_amount:.2f}"

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
            ("ADD_PERCENT", "Charge extra percent"),
            ("DISCOUNT_PERCENT", "Discount percent"),
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
            result += f"${self.formatted_float_value}"
        else:
            result += f"{self.amount}%"
        return result


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
    date_start = models.DateTimeField(auto_now_add=True)
    date_end = models.DateTimeField(null=True, blank=True, default=timezone.now)
    total_time = models.PositiveIntegerField(default=0)
    total_time.help_text = "The total time in seconds the user has spent on the lot page"
    source = models.CharField(max_length=200, blank=True, null=True, default="")
    counter = models.PositiveIntegerField(default=0)
    url = models.CharField(max_length=600, blank=True, null=True)
    title = models.CharField(max_length=600, blank=True, null=True)
    referrer = models.CharField(max_length=600, blank=True, null=True)
    session_id = models.CharField(max_length=600, blank=True, null=True)
    notification_sent = models.BooleanField(default=False)
    duplicate_check_completed = models.BooleanField(default=False)
    latitude = models.FloatField(default=0)
    longitude = models.FloatField(default=0)
    ip_address = models.CharField(max_length=100, blank=True, null=True)
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

    @property
    def merge_and_delete_duplicate(self):
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
    paypal_email_address = models.CharField(max_length=200, blank=True, null=True, verbose_name="Paypal Address")
    paypal_email_address.help_text = "If different from your email address"
    unsubscribe_link = models.CharField(max_length=255, default=uuid.uuid4, blank=True)
    has_unsubscribed = models.BooleanField(default=False, blank=True)
    banned_from_chat_until = models.DateTimeField(null=True, blank=True)
    banned_from_chat_until.help_text = (
        "After this date, the user can post chats again.  Being banned from chatting does not block bidding"
    )
    can_submit_standalone_lots = models.BooleanField(default=True)
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
                ),
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
                ),
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


@receiver(pre_save, sender=Auction)
def on_save_auction(sender, instance, **kwargs):
    """This is run when an auction is saved"""
    if instance.date_end and instance.date_start:
        # if the user entered an end time that's after the start time
        if instance.date_end < instance.date_start:
            new_start = instance.date_end
            instance.date_end = instance.date_start
            instance.date_start = new_start
    if not instance.date_end:
        if instance.is_online:
            instance.date_end = instance.date_start + datetime.timedelta(days=7)
    if not instance.lot_submission_end_date:
        if instance.is_online:
            instance.lot_submission_end_date = instance.date_end
        else:
            instance.lot_submission_end_date = instance.date_start
    if not instance.lot_submission_start_date:
        if instance.is_online:
            instance.lot_submission_start_date = instance.date_start
        else:
            instance.lot_submission_start_date = instance.date_start - datetime.timedelta(days=7)
    # if the lot submission end date is badly set, fix it
    if instance.is_online:
        if instance.lot_submission_end_date > instance.date_end:
            instance.lot_submission_end_date = instance.date_end
    # else:
    # if instance.lot_submission_end_date > instance.date_start:
    # instance.lot_submission_end_date = instance.date_start
    if instance.lot_submission_start_date > instance.date_start:
        instance.lot_submission_start_date = instance.date_start
    # I don't see a problem submitting lots after the auction has started,
    # or any need to restrict when people add lots to an in-person auction
    # So I am not putting any new validation checks here
    # OK, the above comment was not correct, this caused confusion.  A couple checks have been added.
    # Admins can always override those, and they seem to be adding most of the lots for in person stuff anyway.
    # OK, third time's the charm, leave the lines above commented out

    # Some validation for online bidding with in-person auctions for #189
    if not instance.is_online and instance.online_bidding != "disable":
        if not instance.date_online_bidding_ends:
            instance.date_online_bidding_ends = instance.date_start
        if not instance.date_online_bidding_starts:
            instance.date_online_bidding_starts = instance.date_start - datetime.timedelta(days=7)
        if instance.date_online_bidding_ends < instance.date_online_bidding_starts:
            new_start = instance.date_online_bidding_ends
            instance.date_online_bidding_ends = instance.date_online_bidding_starts
            instance.date_online_bidding_starts = new_start

    # if this is an existing auction
    if instance.pk:
        logger.info("updating date end on lots because this is an existing auction")
        # update the date end for all lots associated with this auction
        # note that we do NOT update the end time if there's a winner!
        # This means you cannot reopen an auction simply by changing the date end
        if instance.date_end:
            if instance.date_end + datetime.timedelta(minutes=60) < timezone.now():
                # if we are at least 60 minutes before the auction end
                lots = Lot.objects.exclude(is_deleted=True).filter(
                    auction=instance.pk,
                    winner__isnull=True,
                    auctiontos_winner__isnull=True,
                    active=True,
                )
                for lot in lots:
                    lot.date_end = instance.date_end
                    lot.save()
        if not instance.is_online and instance.number_of_locations == 1:
            # don't make the users set the pickup time seperately for simple auctions
            location = instance.location_qs.first()
            location.pickup_time = instance.date_start
            location.save()

    else:
        # logic for new auctions goes here
        pass
    if not instance.is_online:
        # for in-person auctions, we need to add a single pickup location
        # and create it if the user was dumb enough to delete it
        try:
            in_person_location, created = PickupLocation.objects.get_or_create(
                auction=instance,
                is_default=True,
                defaults={
                    "name": str(instance)[:50],
                    "pickup_time": instance.date_start,
                },
            )
        except:
            logger.warning("Somehow there's two pickup locations for this auction -- how is this possible?")


@receiver(pre_save, sender=UserData)
@receiver(pre_save, sender=PickupLocation)
@receiver(pre_save, sender=Club)
def update_user_location(sender, instance, **kwargs):
    """
    GeoDjango does not appear to support MySQL and Point objects well at the moment (2020)
    To get around this, I'm storing the coordinates in a raw latitude and longitude column

    The custom function distance_to is used to annotate queries

    It is bad practice to use a signal in models.py,
    however with just a couple signals it makes more sense to have them here than to add a whole separate file for it
    """
    try:
        # if not instance.latitude and not instance.longitude:
        # some things to change here:
        # if sender has coords and they do not equal the instance coords, update instance lat/lng from sender
        # if sender has lat/lng and they do not equal the instance lat/lng, update instance coords
        cutLocation = instance.location_coordinates.split(",")
        instance.latitude = float(cutLocation[0])
        instance.longitude = float(cutLocation[1])
    except:
        pass


@receiver(pre_save, sender=Lot)
def update_lot_info(sender, instance, **kwargs):
    """
    Fill out the location and address from the user
    Fill out end date from the auction
    """
    if not instance.pk:
        # new lot?  set the default end date to the auction end
        if instance.auction:
            instance.date_end = instance.auction.date_end
    if instance.user:
        userData, created = UserData.objects.get_or_create(
            user=instance.user,
            defaults={},
        )
        instance.latitude = userData.latitude
        instance.longitude = userData.longitude
        instance.address = userData.address

    # create an invoice for this seller/winner
    if instance.auction and instance.auctiontos_seller:
        invoice, created = Invoice.objects.get_or_create(
            auctiontos_user=instance.auctiontos_seller,
            auction=instance.auction,
            defaults={},
        )
    if instance.auction and instance.auctiontos_winner:
        invoice, created = Invoice.objects.get_or_create(
            auctiontos_user=instance.auctiontos_winner,
            auction=instance.auction,
            defaults={},
        )
    if instance.auction and (not instance.reserve_price or instance.reserve_price < instance.auction.minimum_bid):
        instance.reserve_price = instance.auction.minimum_bid


@receiver(user_logged_in)
def user_logged_in_callback(sender, user, request, **kwargs):
    """When a user signs in, check for any AuctionTOS that have this users email but no user, and attach them to the user
    This allows people to view invoices, leave feedback, get contact information for sellers, etc.
    Important to have this be any user, not just new ones so that existing users can be signed up for in-person auctions"""
    auctiontoss = AuctionTOS.objects.filter(user__isnull=True, email=user.email)
    for auctiontos in auctiontoss:
        auctiontos.user = user
        auctiontos.save()


@receiver(post_save, sender=User)
def create_user_userdata(sender, instance, created, **kwargs):
    if created:
        UserData.objects.create(user=instance)


@receiver(bounce_received)
def bounce_handler(sender, mail_obj, bounce_obj, raw_message, *args, **kwargs):
    # you can then use the message ID and/or recipient_list(email address) to identify any problematic email messages that you have sent
    # message_id = mail_obj['messageId']
    recipient_list = mail_obj["destination"]
    email = recipient_list[0]
    auctiontos = AuctionTOS.objects.filter(email=email)
    for tos in auctiontos:
        tos.email_address_status = "BAD"
        tos.save()


@receiver(complaint_received)
def complaint_handler(sender, mail_obj, complaint_obj, raw_message, *args, **kwargs):
    recipient_list = mail_obj["destination"]
    email = recipient_list[0]
    user = User.objects.filter(email=email).first()
    if user:
        user.userdata.unsubscribe_from_all
