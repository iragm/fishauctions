"""DRF serializers for the club API: the shape of what a key gets back.

Two rules run through the whole file. **Everything that names somebody lives in a ``private``
object** which :class:`PrivateBlockMixin` removes entirely -- absent, not null -- for a key without
the privacy flag. And **money is always a string**: :class:`DecimalField` renders one, because a raw
``Decimal`` in a hand-built dict comes out of DRF's encoder as a float.

:class:`SparseFieldsMixin` implements ``?fields=`` by dropping fields in ``__init__``, so an omitted
field costs no queries. It cannot conjure ``private`` -- the mixin pops that afterwards.
"""

import re
from datetime import timezone as date_tz
from urllib.parse import quote_plus

from django.conf import settings
from rest_framework import serializers

from .models import (
    Auction,
    AuctionDropdown,
    AuctionTOS,
    BapAward,
    ClubMember,
    Lot,
    LotImage,
    Species,
    SpeciesCommonName,
    normalize_species_name,
)

CLUB_MEMBER_API_KEY_EXCLUDED_FIELDS = frozenset(
    {
        "id",
        "user",
        "club",
        "uuid",
        "createdon",
        "added_by",
        "is_deleted",
        "possible_duplicate",
        "last_discord_role_assigned",
        "discord_role_override",
        "membership_number",
        "source",  # set server-side from the API key name; not caller-writable
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
        "bap_points",
        "hap_points",
        "culture_points",
        "bap_points_ytd",
        "hap_points_ytd",
        "culture_points_ytd",
    }
)
CLUB_MEMBER_API_KEY_WRITE_FIELDS = tuple(
    field.name for field in ClubMember._meta.fields if field.name not in CLUB_MEMBER_API_KEY_EXCLUDED_FIELDS
)
CLUB_MEMBER_API_KEY_MAPPING_FIELDS = (*CLUB_MEMBER_API_KEY_WRITE_FIELDS, "first_name", "last_name")


class ClubMemberSerializer(serializers.ModelSerializer):
    wallet_link = serializers.ReadOnlyField()
    simple_membership_link = serializers.ReadOnlyField()
    # lat/lng are intentionally excluded from the API to protect member location privacy.
    # Only the rounded distance to the club is exposed. Do not add lat/lng here.
    distance_to = serializers.SerializerMethodField()

    def get_distance_to(self, obj):
        """Return distance from club to member in miles, rounded to 10 miles, or null."""
        val = getattr(obj, "distance_to", None)
        return int(val) if val is not None else None

    class Meta:
        model = ClubMember
        fields = [
            "id",
            "club",
            "name",
            "email",
            "email_address_status",
            "phone_number",
            "address",
            "discord_id",
            "bap_points",
            "hap_points",
            "membership_last_paid",
            "membership_expiration_date",
            "membership_expiration_reminder_due",
            "createdon",
            "source",
            "is_deleted",
            "memo",
            "membership_number",
            "wallet_link",
            "simple_membership_link",
            "distance_to",
        ]
        # membership_expiration_date is reported but not writable here: it is set by renewals
        # (the renew endpoint / Renew button) so the ledger and club history always agree with it.
        read_only_fields = [
            "id",
            "createdon",
            "club",
            "is_deleted",
            "membership_number",
            "membership_expiration_date",
        ]


class ClubMemberIngestSerializer(serializers.Serializer):
    """Flexible ingest serializer for API key-authenticated external services.

    Accepts either a single ``name`` field, or ``first_name``/``last_name``
    (which are combined into ``name``). At least one of those, or ``email``,
    must be provided.
    """

    name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    first_name = serializers.CharField(max_length=100, required=False, allow_blank=True, write_only=True)
    last_name = serializers.CharField(max_length=100, required=False, allow_blank=True, write_only=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone_number = serializers.CharField(max_length=20, required=False, allow_blank=True)
    address = serializers.CharField(max_length=500, required=False, allow_blank=True)
    memo = serializers.CharField(required=False, allow_blank=True)

    def validate(self, data):
        first = (data.pop("first_name", "") or "").strip()
        last = (data.pop("last_name", "") or "").strip()
        name = (data.get("name", "") or "").strip()
        if not name and (first or last):
            name = f"{first} {last}".strip()
        if name:
            data["name"] = name
        else:
            data.pop("name", None)
        if not data.get("email") and not data.get("name"):
            msg = "Provide at least an email address or a name."
            raise serializers.ValidationError(msg)
        if data.get("email"):
            data["email"] = data["email"].lower().strip()
        for field in ("address", "memo", "phone_number"):
            if data.get(field):
                data[field] = data[field].strip()
        return data


class ClubMemberAPIKeySerializer(serializers.ModelSerializer):
    """Writable serializer for ClubMember records created or updated via API keys."""

    id = serializers.IntegerField(read_only=True)
    # source is set server-side from the API key name and must not be overridden by callers
    source = serializers.CharField(read_only=True)
    first_name = serializers.CharField(max_length=100, required=False, allow_blank=True, write_only=True)
    last_name = serializers.CharField(max_length=100, required=False, allow_blank=True, write_only=True)

    class Meta:
        model = ClubMember
        fields = ["id", "source", *CLUB_MEMBER_API_KEY_WRITE_FIELDS, "first_name", "last_name"]

    def validate(self, data):
        first = (data.pop("first_name", "") or "").strip()
        last = (data.pop("last_name", "") or "").strip()
        name = (data.get("name", "") or "").strip()
        if not name and (first or last):
            name = f"{first} {last}".strip()
        if name:
            data["name"] = name
        elif "name" in data:
            data["name"] = ""

        if data.get("email"):
            data["email"] = data["email"].lower().strip()
        for field in (
            "address",
            "memo",
            "phone_number",
            "discord_id",
            "discord_username",
        ):
            if data.get(field):
                data[field] = data[field].strip()
        if not self.instance and not data.get("email") and not data.get("name"):
            msg = "Provide at least an email address or a name."
            raise serializers.ValidationError(msg)
        return data


class BapAwardSummarySerializer(serializers.ModelSerializer):
    """The points already recorded against a lot, nested inside ClubBapLotSerializer."""

    auto_awarded = serializers.SerializerMethodField()

    def get_auto_awarded(self, obj):
        """True when the site awarded these points itself; false when a person did."""
        return obj.awarded_by_id is None

    class Meta:
        model = BapAward
        fields = ["id", "date", "points", "hap_points", "cap_points", "notes", "auto_awarded"]


class ClubBapLotSerializer(serializers.ModelSerializer):
    """Read-only view of one lot from a club's auction, for external BAP systems.

    Everything here is already visible to club admins on the BAP pages; this is the same data in a
    shape an outside breeder-award program can match on (seller/winner email are the join keys),
    plus everything the site knows about whether the lot earns points.
    """

    lot_id = serializers.IntegerField(source="pk", read_only=True)
    # Always a string: Lot.lot_number_display is an int for plain numbering and a string like
    # "101-1" under seller-dash numbering, and a caller shouldn't have to handle both types.
    lot_number_display = serializers.CharField(read_only=True)
    seller_name = serializers.SerializerMethodField()
    seller_email = serializers.SerializerMethodField()
    winner_name = serializers.ReadOnlyField()
    winner_email = serializers.ReadOnlyField()
    # UTC, so every timestamp in the response reads the same regardless of the club's timezone
    timestamp = serializers.DateTimeField(source="date_end", read_only=True, default_timezone=date_tz.utc)
    sold = serializers.ReadOnlyField()
    category = serializers.SerializerMethodField()
    program = serializers.CharField(source="bap_placeholder", read_only=True)
    custom_checkbox_name = serializers.SerializerMethodField()
    bap_eligible = serializers.SerializerMethodField()
    bap_ineligible_reason = serializers.SerializerMethodField()
    bap_ineligible_reason_display = serializers.SerializerMethodField()
    bap_award = serializers.SerializerMethodField()

    # Lot.seller_name / seller_email return the literal "Unknown" when a lot has neither an
    # AuctionTOS nor a user; blank is friendlier for a caller matching on these.
    def get_seller_name(self, obj):
        return "" if obj.seller_name == "Unknown" else obj.seller_name

    def get_seller_email(self, obj):
        return "" if obj.seller_email == "Unknown" else obj.seller_email

    def get_category(self, obj):
        return obj.species_category.name if obj.species_category else ""

    def get_custom_checkbox_name(self, obj):
        """The auction's label for custom_checkbox, so the flag means something to the caller."""
        if obj.auction and obj.auction.use_custom_checkbox_field:
            return obj.auction.custom_checkbox_name or ""
        return ""

    @staticmethod
    def _no_bap_reason(obj):
        """Lot.sold_lot_no_bap_reason hits the database, so evaluate it once per lot."""
        if not hasattr(obj, "_cached_no_bap_reason"):
            obj._cached_no_bap_reason = obj.sold_lot_no_bap_reason
        return obj._cached_no_bap_reason

    def get_bap_eligible(self, obj):
        return self._no_bap_reason(obj) is None

    def get_bap_ineligible_reason(self, obj):
        return self._no_bap_reason(obj) or ""

    def get_bap_ineligible_reason_display(self, obj):
        reason = self._no_bap_reason(obj)
        return dict(Lot.BAP_REASON_CHOICES).get(reason, reason or "")

    def get_bap_award(self, obj):
        try:
            award = obj.bap_award
        except BapAward.DoesNotExist:
            return None
        return BapAwardSummarySerializer(award).data

    class Meta:
        model = Lot
        fields = [
            "lot_id",
            "lot_number_display",
            "lot_name",
            "quantity",
            "seller_name",
            "seller_email",
            "winner_name",
            "winner_email",
            "timestamp",
            "sold",
            "category",
            "program",
            "i_bred_this_fish",
            "donation",
            "custom_checkbox",
            "custom_checkbox_name",
            "bap_eligible",
            "bap_ineligible_reason",
            "bap_ineligible_reason_display",
            "bap_auto_reason",
            "bap_points_awarded",
            "manually_approved",
            "bap_award",
        ]


class SpeciesMatchSerializer(serializers.ModelSerializer):
    """One species, as the species lookup API returns it.

    Read-only, and deliberately fat: a caller matching free text to a species is usually filing it
    into their own taxonomy, so everything this site knows about the row travels with it and they
    never need a second call.  ``full_scientific_name`` is the one to display -- it is the only
    field that carries a cultivar ("Neocaridina davidi 'Blue Dream'"), where ``scientific_name`` is
    the parent species and looks identical for all thirteen colour strains.
    """

    #: Cultivar rows carry their parent's genus and epithet, so a caller that only wants real
    #: taxonomy can follow this instead of guessing from the variety field.
    parent = serializers.SerializerMethodField()
    full_scientific_name = serializers.ReadOnlyField()
    #: "Genus species 'Strain'" -- what a person picks from on the lot form.  The common name is
    #: deliberately not repeated into it; it is its own field, and both are here anyway.
    label = serializers.ReadOnlyField()
    category = serializers.SerializerMethodField()
    common_names = serializers.SerializerMethodField()
    # Named "species" on the model, which is confusing inside a species record, so it goes out
    # under the name the rest of the world uses for it.
    species_epithet = serializers.CharField(source="species", read_only=True)

    def get_parent(self, obj):
        if not obj.parent_id:
            return None
        return {"id": obj.parent_id, "scientific_name": obj.parent.scientific_name}

    def get_category(self, obj):
        """The category a lot of this species gets on this site, or null where none is mapped."""
        if not obj.category_id:
            return None
        return {"id": obj.category_id, "name": obj.category.name}

    def get_common_names(self, obj):
        """Every name this species answers to, so a caller can see the one it just added.

        Capped, because FishBase gives *Poecilia reticulata* forty-odd names in a dozen languages
        and a match response is not the place to ship all of them.
        """
        names = obj.common_names.all()[:20]
        return [{"id": name.pk, "name": name.name, "source": name.source} for name in names]

    class Meta:
        model = Species
        fields = [
            "id",
            "scientific_name",
            "full_scientific_name",
            "common_name",
            "common_names",
            "label",
            "genus",
            "species_epithet",
            "variety",
            "parent",
            # True where "variety" is a cross rather than a strain, and the only case where
            # scientific_name, genus and species_epithet are all empty: a hybrid has no binomial
            # to carry.  full_scientific_name reads "Hybrid 'Tibee'" for these.
            "is_hybrid",
            "family",
            "order",
            "category",
            "trade_rank",
            "source",
            # A species this club added and nobody has approved yet comes back to *this* club and
            # to nobody else, so the club's own software has to be able to tell the two apart.
            "approved",
        ]


class _NameListField(serializers.Field):
    """A list of names, or one string of them separated by commas or newlines.

    Both, because the two callers are different: a script sending JSON has a list already, and a
    person pasting the names off a bag label has one line with commas in it.  Rejecting either
    would be a rule with no purpose behind it.
    """

    def to_internal_value(self, data):
        if isinstance(data, str):
            data = re.split(r"[,\n]+", data)
        if not isinstance(data, list | tuple):
            msg = "Send a list of names, or one string with them separated by commas."
            raise serializers.ValidationError(msg)
        return [str(item).strip()[:255] for item in data if str(item).strip()]

    def to_representation(self, value):
        return list(value or [])


class SpeciesCreateSerializer(serializers.Serializer):
    """Add a species this site's list has never heard of, through the club API.

    The same shape as :class:`~auctions.forms.SpeciesAdminForm`, and deliberately so: the two share
    :func:`~auctions.species_matching.split_scientific_name` and
    :func:`~auctions.species_matching.species_already_named`, so a name typed at a check-in table
    and the same name sent by a club's website are filed identically.

    Create only.  Nothing here can reach an existing row: a name that already exists is answered
    with *that* row rather than a second copy of it, and the caller is told where to find it.
    """

    scientific_name = serializers.CharField(required=False, allow_blank=True, max_length=250)
    common_name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    other_names = _NameListField(required=False)
    variety = serializers.CharField(required=False, allow_blank=True, max_length=100)
    parent = serializers.IntegerField(required=False, allow_null=True)
    is_hybrid = serializers.BooleanField(required=False, default=False)
    freshwater = serializers.BooleanField(required=False, default=True)
    brackish = serializers.BooleanField(required=False, default=False)
    saltwater = serializers.BooleanField(required=False, default=False)
    breeder_points = serializers.BooleanField(required=False, default=True)

    def __init__(self, *args, club=None, **kwargs):
        super().__init__(*args, **kwargs)
        #: Which unapproved species this caller may point a strain at.  A club may build on its own
        #: rows and on everybody's, and cannot see anyone else's unapproved ones.
        self.club = club

    def validate_parent(self, value):
        from .species_matching import visible_species

        if value is None:
            return None
        parent = visible_species(None, self.club).filter(pk=value).first()
        if not parent:
            msg = "No species with that id on this site's list."
            raise serializers.ValidationError(msg)
        if parent.parent_id or parent.variety:
            msg = "A strain has to be a strain of a plain species, not of another strain or a hybrid."
            raise serializers.ValidationError(msg)
        return parent

    def validate(self, data):
        from .species_matching import species_carrying_common_name, split_scientific_name

        variety = (data.get("variety") or "").strip()
        parent = data.get("parent")
        is_hybrid = bool(data.get("is_hybrid"))
        if is_hybrid:
            # A cross is a name and nothing else -- no binomial, no parent species.  Sending
            # either alongside is a contradiction, not extra detail, so it is refused rather than
            # dropped on the floor.  See Species.is_hybrid.
            if parent:
                msg = {"parent": "A hybrid is not a strain of one species — that is what makes it a hybrid."}
                raise serializers.ValidationError(msg)
            if (data.get("scientific_name") or "").strip():
                msg = {"scientific_name": "Leave this out for a hybrid: a cross has no scientific name."}
                raise serializers.ValidationError(msg)
            if not variety:
                msg = {"variety": "Give the hybrid the name the trade uses for it, e.g. Tibee."}
                raise serializers.ValidationError(msg)
            genus, epithet = "", ""
        elif variety and not parent:
            msg = {"parent": "A strain has to say which species it is a strain of, unless is_hybrid is true."}
            raise serializers.ValidationError(msg)
        elif parent and not variety:
            msg = {"variety": "Give the strain a name, e.g. Blue Dream."}
            raise serializers.ValidationError(msg)
        elif parent:
            # A strain takes its parent's name; there is nothing to send and nothing to disagree
            # about.  This is what keeps "Blue Dream" out of the genus column.
            genus, epithet = parent.genus, parent.species
        else:
            genus, epithet = split_scientific_name(data.get("scientific_name"))
            if not genus:
                msg = {"scientific_name": "Required, unless you are adding a strain of a species that is already here."}
                raise serializers.ValidationError(msg)
        # A name that already names something else would make both lookups ambiguous, so it is
        # refused here rather than written and left to confuse the matcher later.
        sent = (("common_name", [data.get("common_name")]), ("other_names", data.get("other_names", [])))
        for field, names in sent:
            for name in names:
                taken = species_carrying_common_name(name, club=self.club)
                if taken:
                    msg = {field: f"“{name}” is already the name for {taken.label}."}
                    raise serializers.ValidationError(msg)
        data["variety"] = variety
        data["genus"] = genus
        data["epithet"] = epithet
        return data

    def create(self, validated_data):
        """Write the species and its names.  ``club``, ``added_by`` and ``category`` come from save()."""
        species = Species.objects.create(
            genus=validated_data["genus"],
            species=validated_data["epithet"],
            variety=validated_data["variety"],
            parent=validated_data.get("parent"),
            is_hybrid=validated_data.get("is_hybrid", False),
            common_name=(validated_data.get("common_name") or "").strip(),
            category=validated_data.get("category"),
            freshwater=validated_data.get("freshwater", True),
            brackish=validated_data.get("brackish", False),
            saltwater=validated_data.get("saltwater", False),
            breeder_points=validated_data.get("breeder_points", True),
            # Added on the site on purpose, which is not the same as the "manual" rows left over
            # from the old Product table -- import_fishbase folds those into the imported list.
            source="admin",
            # Somebody is adding this because a club is selling one, which is better evidence than
            # FishBase's column.  See Species.in_aquarium_trade.
            in_trade_override=True,
            # A key is a script, not a superuser: what it adds is this club's until somebody
            # approves it for everybody.  See species_matching.visible_species.
            approved=False,
            club=validated_data.get("club"),
            added_by=validated_data.get("added_by"),
        )
        names = [species.common_name, *validated_data.get("other_names", [])]
        seen = set()
        for index, name in enumerate(names):
            key = (name or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            SpeciesCommonName.objects.create(
                species=species,
                name=name.strip()[:255],
                language="English",
                is_preferred=(index == 0),
                source="admin",
                # Stamped like the species: they arrived with it and become everybody's when it
                # does.  See SpeciesApproveView.
                approved=species.approved,
                added_by=species.added_by,
                club=species.club,
            )
        Species.recompute_trade_ranks(genus=species.genus)
        species.refresh_from_db()
        return species


class SpeciesCommonNameCreateSerializer(serializers.Serializer):
    """Attach one more common name to a species that is already on the list."""

    name = serializers.CharField(max_length=255)

    def validate_name(self, value):
        if not normalize_species_name(value):
            msg = "A name has to have some letters or numbers in it."
            raise serializers.ValidationError(msg)
        return value.strip()


class BapAwardAPIKeyCreateSerializer(serializers.Serializer):
    """Simple serializer for adding BAP points to a club member."""

    points = serializers.IntegerField(min_value=1)
    date = serializers.DateField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500)

    def validate(self, data):
        if data.get("notes"):
            data["notes"] = data["notes"].strip()
        return data


def _absolute(request, url):
    """Make a site-relative URL absolute, leaving one that is already absolute alone.

    Everything this API returns is meant to be pasted into somebody else's web page, and a media
    path or a lot link that starts with ``/`` resolves against *their* domain there.  Cloudflare
    already hands back absolute image URLs, so the two cases have to live side by side.
    """
    if not url:
        return None
    if url.startswith(("http://", "https://", "//")):
        return url
    return request.build_absolute_uri(url) if request else url


def _auction_status(auction):
    """Where an auction is in its life, as four flags rather than one word.

    Four, because they are not a sequence: an in-person auction is ``started`` and never
    ``closed``, and ``over`` waits a day past the last pickup where ``closed`` fires the moment
    bidding ends.
    """
    return {
        "started": auction.started,
        "closed": auction.closed,
        "over": auction.pretty_much_over,
        "lot_submission_open": auction.can_submit_lots,
    }


def _named(obj, name_attr="name"):
    """``{"id": …, "name": …}`` for a foreign key, or ``None``.

    Every reference in this API carries both halves: the id is what a caller stores and matches on,
    the name is what it prints.  A caller that only got one of them ends up doing a second lookup
    or displaying a number.
    """
    if obj is None:
        return None
    return {"id": obj.pk, "name": getattr(obj, name_attr)}


class PrivateBlockMixin:
    """Drops the ``private`` key entirely unless the caller is allowed to read it.

    Absent rather than ``null``: a page built against a public key never has the shape of a
    response that could have carried a name, so there is nothing to accidentally render.
    """

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not self.context.get("private"):
            data.pop("private", None)
        return data


class SparseFieldsMixin:
    """``context["fields"]`` narrows the response to the keys a caller actually wants.

    A club's lot grid needs a number, a name, a link and a picture; sending it the description and
    every image of four hundred lots is most of the payload and none of the page.  Dropped here
    rather than after rendering so the work is never done: a field left out costs no queries.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        wanted = self.context.get("fields")
        if wanted:
            for name in set(self.fields) - set(wanted):
                self.fields.pop(name)


class ClubApiLotImageSerializer(serializers.ModelSerializer):
    """One picture of a lot: the full-size URL and the same small crop the lot list uses."""

    url = serializers.SerializerMethodField()
    thumbnail = serializers.SerializerMethodField()
    caption = serializers.SerializerMethodField()
    image_source_display = serializers.SerializerMethodField()

    def get_url(self, obj):
        return _absolute(self.context.get("request"), obj.display_url)

    def get_thumbnail(self, obj):
        return _absolute(self.context.get("request"), obj.thumbnail_url)

    def get_caption(self, obj):
        return obj.caption or ""

    def get_image_source_display(self, obj):
        return dict(obj.PIC_CATEGORIES).get(obj.image_source, "")

    class Meta:
        model = LotImage
        fields = ["id", "url", "thumbnail", "caption", "is_primary", "image_source", "image_source_display"]


class ClubApiLotSerializer(SparseFieldsMixin, PrivateBlockMixin, serializers.ModelSerializer):
    """One lot, as a club's own software reads it.

    Two audiences in one shape.  Everything at the top level is what a club would put on its own
    public "here are the lots in our next auction" page, and nothing there names a person.  The
    ``private`` object holds the buyer and the seller, and is simply missing unless the key was
    ticked for it -- see :attr:`~auctions.models.ClubAPIKey.can_read_private_lots`.

    ``lot_id`` is this site's permanent id and is never reused, so overlapping pulls can key on it.
    ``lot_number`` is the number people read off the label and is unique only within the auction.
    """

    lot_id = serializers.IntegerField(source="pk", read_only=True)
    # Always a string: lot_number_display is an int under plain numbering and something like
    # "101-1" under seller-dash numbering, and a caller shouldn't have to handle both types.
    lot_number = serializers.SerializerMethodField()
    url = serializers.SerializerMethodField()
    description = serializers.CharField(source="summernote_description", read_only=True)
    category = serializers.SerializerMethodField()
    species = serializers.SerializerMethodField()
    common_name = serializers.SerializerMethodField()
    min_bid = serializers.DecimalField(source="reserve_price", max_digits=10, decimal_places=2, read_only=True)
    sold = serializers.ReadOnlyField()
    thumbnail = serializers.SerializerMethodField()
    images = serializers.SerializerMethodField()
    date_posted = serializers.DateTimeField(read_only=True, default_timezone=date_tz.utc)
    date_end = serializers.DateTimeField(read_only=True, default_timezone=date_tz.utc)
    private = serializers.SerializerMethodField()

    def get_lot_number(self, obj):
        return str(obj.lot_number_display)

    def get_url(self, obj):
        """The lot's page here, tagged with the name of the key that pulled it.

        ``?src=`` is the same parameter the site's own page-view tracking reads, so a club that
        publishes this feed can see in its auction stats how much traffic its own website sent.
        """
        link = obj.lot_link
        source = self.context.get("src")
        if source:
            link = f"{link}?src={quote_plus(source)}"
        return _absolute(self.context.get("request"), link)

    def get_category(self, obj):
        return _named(obj.species_category)

    def get_species(self, obj):
        """The species, or ``None`` where there isn't one to show.

        ``Lot.scientific_name`` is the one rule for that: it is blank for a lot with no species --
        hardware, mixed bags -- and blank for every lot in an auction whose admins turned the
        scientific name field off, which is that club saying it doesn't want them.
        """
        name = obj.scientific_name
        if not name:
            return None
        return {"id": obj.species_id, "scientific_name": name, "common_name": obj.species.common_name or ""}

    def get_common_name(self, obj):
        """The hobby name to print under the lot name, when the lot name is the scientific one."""
        return obj.common_name_line

    def _lot_images(self, obj):
        """The lot's images, from the map the view prefetched, falling back to a query.

        The map is keyed on whichever lot actually owns the pictures, which is ``use_images_from``
        when one lot is borrowing another's.
        """
        images = self.context.get("images_by_lot")
        if images is None:
            return list(obj.images)
        return images.get(obj.use_images_from_id or obj.pk, [])

    def get_images(self, obj):
        return ClubApiLotImageSerializer(self._lot_images(obj), many=True, context=self.context).data

    def get_thumbnail(self, obj):
        """The small crop for a lot tile, ready to drop straight into an ``<img>``.

        The lot's own primary picture, or the one this site would auto-add to it (a picture from an
        older lot of the same name in one of this club's auctions) -- the same image the lot list
        on this site shows, so a club's own page doesn't come out full of blanks.
        """
        for image in self._lot_images(obj):
            if image.is_primary:
                return _absolute(self.context.get("request"), image.thumbnail_url)
        auto = (self.context.get("auto_images") or {}).get(obj.lot_name)
        if auto:
            return _absolute(self.context.get("request"), auto.thumbnail_url)
        return None

    def get_private(self, obj):
        """Everything that names somebody, plus the admin-side state of the lot."""
        seller, winner = obj.auctiontos_seller, obj.auctiontos_winner
        return {
            "seller_name": obj.seller_name if obj.seller_name != "Unknown" else "",
            "seller_email": obj.seller_email if obj.seller_email != "Unknown" else "",
            "seller_number": seller.bidder_number if seller else "",
            "winner_name": obj.winner_name,
            "winner_email": obj.winner_email,
            "winner_number": winner.bidder_number if winner else "",
            "removed": obj.banned,
            "ban_reason": obj.ban_reason or "",
            "refunded": obj.refunded,
            "partial_refund_percent": obj.partial_refund_percent,
            "label_printed": obj.label_printed,
            "seller_feedback": obj.feedback_text or "",
            "winner_feedback": obj.winner_feedback_text or "",
        }

    class Meta:
        model = Lot
        fields = [
            "lot_id",
            "lot_number",
            "lot_name",
            "url",
            "quantity",
            "description",
            "category",
            "species",
            "common_name",
            "custom_checkbox",
            "custom_field_1",
            "custom_dropdown",
            "i_bred_this_fish",
            "donation",
            "reference_link",
            "min_bid",
            "buy_now_price",
            "active",
            "sold",
            "winning_price",
            "date_posted",
            "date_end",
            "thumbnail",
            "images",
            "private",
        ]


class ClubApiAuctionSerializer(PrivateBlockMixin, serializers.ModelSerializer):
    """One auction: when it runs, what it charges, what its rules say and how lots are described.

    ``rules`` is the HTML an admin wrote on the auction's rules page.  ``lot_fields`` says which of
    the optional per-lot fields this auction uses and what it calls them, so a caller rendering a
    lot knows whether ``custom_dropdown`` means anything and what heading to put over it.
    """

    url = serializers.SerializerMethodField()
    club = serializers.SerializerMethodField()
    rules = serializers.CharField(source="summernote_description", read_only=True)
    status = serializers.SerializerMethodField()
    date_posted = serializers.DateTimeField(read_only=True, default_timezone=date_tz.utc)
    date_start = serializers.DateTimeField(read_only=True, default_timezone=date_tz.utc)
    date_end = serializers.DateTimeField(read_only=True, default_timezone=date_tz.utc)
    lot_submission_start_date = serializers.DateTimeField(read_only=True, default_timezone=date_tz.utc)
    lot_submission_end_date = serializers.DateTimeField(read_only=True, default_timezone=date_tz.utc)
    date_online_bidding_starts = serializers.DateTimeField(read_only=True, default_timezone=date_tz.utc)
    date_online_bidding_ends = serializers.DateTimeField(read_only=True, default_timezone=date_tz.utc)
    lot_count = serializers.SerializerMethodField()
    pickup_locations = serializers.SerializerMethodField()
    fees = serializers.SerializerMethodField()
    lot_fields = serializers.SerializerMethodField()
    private = serializers.SerializerMethodField()

    def get_url(self, obj):
        return _absolute(self.context.get("request"), obj.get_absolute_url())

    def get_club(self, obj):
        return _named(obj.club)

    def get_status(self, obj):
        return _auction_status(obj)

    def get_lot_count(self, obj):
        return obj.lots_qs.exclude(banned=True).count()

    def get_pickup_locations(self, obj):
        return [
            {
                "id": location.pk,
                "name": location.name or "",
                "description": location.description or "",
                "address": location.address or "",
                "pickup_time": location.pickup_time,
                "second_pickup_time": location.second_pickup_time,
                "pickup_by_mail": location.pickup_by_mail,
                "users_must_coordinate_pickup": location.users_must_coordinate_pickup,
            }
            for location in obj.location_qs
        ]

    def get_fees(self, obj):
        return {
            "currency": obj.currency,
            # A string, like every other price in this API: JSON has no decimal type, and rounding
            # money through a float is the kind of bug that shows up on one invoice in a thousand.
            "minimum_bid": f"{obj.minimum_bid:.2f}",
            "lot_entry_fee": obj.lot_entry_fee,
            "unsold_lot_fee": obj.unsold_lot_fee,
            "registration_fee": obj.registration_fee,
            "winning_bid_percent_to_club": obj.winning_bid_percent_to_club,
            "tax": obj.tax,
            "only_whole_dollar_bids": obj.only_whole_dollar_bids,
        }

    def get_lot_fields(self, obj):
        """Which optional lot fields this auction uses, and what it calls the custom ones.

        The three custom fields are the reason this is here: ``custom_checkbox``,
        ``custom_field_1`` and ``custom_dropdown`` arrive on every lot with no hint of what the
        club meant by them, and the heading lives here.
        """
        return {
            "use_quantity_field": obj.use_quantity_field,
            "use_description": obj.use_description,
            "use_reference_link": obj.use_reference_link,
            "use_categories": obj.use_categories,
            "use_scientific_name": obj.use_scientific_name,
            "use_donation_field": obj.use_donation_field,
            "use_i_bred_this_fish_field": obj.use_i_bred_this_fish_field,
            "i_bred_this_fish_label": settings.I_BRED_THIS_FISH_LABEL,
            "use_custom_checkbox_field": obj.use_custom_checkbox_field,
            "custom_checkbox_name": obj.custom_checkbox_name or "",
            "custom_field_1": obj.custom_field_1,
            "custom_field_1_name": obj.custom_field_1_name or "",
            "use_custom_dropdown_field": obj.use_custom_dropdown_field,
            "custom_dropdown_name": obj.custom_dropdown_name or "",
            "custom_dropdown_options": list(
                AuctionDropdown.objects.filter(auction=obj).order_by("createdon").values_list("value", flat=True)
            ),
        }

    def get_private(self, obj):
        """The auction's own numbers that are nobody else's business.

        Deliberately not here: ``google_drive_link``.  That sheet is shared "anyone with the link
        can view", so the link *is* the credential -- handing it out through an API would let
        everyone who can read an auction read the club's spreadsheet, and no checkbox on a key
        should be able to do that.
        """
        return {
            "created_by": obj.created_by.username if obj.created_by else "",
            "invoiced": obj.invoiced,
            "participant_count": AuctionTOS.objects.filter(auction=obj).count(),
            "removed_lot_count": obj.lots_qs.filter(banned=True).count(),
        }

    class Meta:
        model = Auction
        fields = [
            "id",
            "slug",
            "title",
            "url",
            "club",
            "status",
            "is_online",
            "sealed_bid",
            "online_bidding",
            "buy_now",
            "reserve_price",
            "promote_this_auction",
            "location",
            "rules",
            "date_posted",
            "date_start",
            "date_end",
            "lot_submission_start_date",
            "lot_submission_end_date",
            "date_online_bidding_starts",
            "date_online_bidding_ends",
            "max_lots_per_user",
            "lot_count",
            "fees",
            "lot_fields",
            "pickup_locations",
            "private",
        ]


class ClubApiAuctionSummarySerializer(serializers.ModelSerializer):
    """One row of the auction list: enough to pick which auction you want, and nothing else.

    Deliberately thin.  The list is a picker -- the fees, the rules and the pickup locations are
    one more request away, and putting them on every row would make a club with sixty auctions
    behind it an expensive thing to ask a simple question of.
    """

    url = serializers.SerializerMethodField()
    date_posted = serializers.DateTimeField(read_only=True, default_timezone=date_tz.utc)
    date_start = serializers.DateTimeField(read_only=True, default_timezone=date_tz.utc)
    date_end = serializers.DateTimeField(read_only=True, default_timezone=date_tz.utc)
    status = serializers.SerializerMethodField()

    def get_url(self, obj):
        return _absolute(self.context.get("request"), obj.get_absolute_url())

    def get_status(self, obj):
        return _auction_status(obj)

    class Meta:
        model = Auction
        fields = [
            "id",
            "slug",
            "title",
            "url",
            "is_online",
            "promote_this_auction",
            "date_posted",
            "date_start",
            "date_end",
            "status",
        ]
