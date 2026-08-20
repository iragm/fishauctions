import re
from datetime import timezone as date_tz

from rest_framework import serializers

from .models import BapAward, ClubMember, Lot, Species, SpeciesCommonName, normalize_species_name

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
