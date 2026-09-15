"""Operations that are the same whoever asks: web page, API, app or assistant.

Views, the club API, mobile endpoints and ``palette_actions`` all call these, so a rule can't live
in only one of them. Permission checks are the caller's job.
"""

import logging

from django.utils import timezone

from .models import Auction, AuctionTOS, ClubHistory, ClubMember

logger = logging.getLogger(__name__)

# ClubMember fields accepted by API ingest. ``first_name``/``last_name`` are combined into ``name``.
INGEST_ALLOWED_FIELDS = frozenset({"name", "email", "phone_number", "address", "memo"})


def map_fields(data: dict, api_key) -> dict:
    """Rename incoming keys by this key's ClubAPIKeyFieldMap; ``first_name``/``last_name`` become ``name``
    unless ``name`` is set.
    """
    mapping = {m.external_field: m.internal_field for m in api_key.field_mappings.all()}
    result = {mapping.get(k, k): v for k, v in data.items()}

    first = (result.pop("first_name", "") or "").strip()
    last = (result.pop("last_name", "") or "").strip()
    if not result.get("name") and (first or last):
        result["name"] = f"{first} {last}".strip()

    return result


def create_club_member_from_api(validated_data: dict, club, api_key):
    """Create a ClubMember from API data, skipping duplicate emails. Logs history, touches
    ``api_key.last_used_at``. Returns (member, created).
    """
    email = validated_data.get("email", "")
    member = None

    if email:
        member = ClubMember.objects.filter(club=club, email=email, is_deleted=False).first()

    created = member is None
    if created:
        member = ClubMember(club=club, source=api_key.name, added_by=None)
        for field, value in validated_data.items():
            if field in INGEST_ALLOWED_FIELDS:
                setattr(member, field, value)
        member.save()

    label = "Added" if created else "Duplicate skipped for"
    ClubHistory.objects.create(
        club=club,
        user=None,
        action=f"{label} member via API [{api_key.prefix}] ({api_key.name}): {member}",
        applies_to="MEMBERS",
    )

    api_key.last_used_at = timezone.now()
    api_key.save(update_fields=["last_used_at"])

    return member, created


# ---------------------------------------------------------------------------
# Participants in a club-managed auction
# ---------------------------------------------------------------------------
#
# The ClubMember owns the bidder number and permissions; every AuctionTOS is its shadow. Every path
# that adds someone must find or create the member and copy those down. Creating a ClubMember also
# creates its shadow TOS (signals.propagate_clubmember_to_shadow_tos), so a caller with no TOS yet
# ensures the member and adopts the shadow (existing_tos_for_club_member); one with a TOS lets
# AuctionTOS.save() merge the shadow away.


def ensure_club_member(
    auction, *, user=None, name="", email="", phone_number="", address="", bidder_number="", admin_edited=True
):
    """Find or create the ClubMember for a participant in *auction*; return (member, created).

    Matches by user, then email, and links *user* to an email-only member. A new member gets
    *bidder_number* if free in the club, else a generated one, and a history line. ``(None, False)``
    for a plain auction. ``admin_edited=False`` marks a self-signup row as the member's own, so account
    deletion removes it; existing rows are untouched.
    """
    if not auction.is_club_managed:
        return None, False
    club = auction.club
    member = None
    if user is not None:
        member = ClubMember.objects.filter(club=club, user=user, is_deleted=False).first()
    if member is None and email:
        member = ClubMember.objects.filter(club=club, email__iexact=email, is_deleted=False).first()
    created = False
    if member is None:
        if not name or name == "Unknown":
            # Don't carry AuctionTOS's "Unknown" placeholder name into the club.
            name = (user.get_full_name() or user.username) if user else name
        member = ClubMember(
            club=club,
            user=user,
            name=name or "",
            email=email or (user.email if user else "") or "",
            phone_number=phone_number or "",
            address=address or "",
            source=str(auction.title)[:200],
            added_by=user,
            admin_edited=admin_edited,
        )
        # A vetted auction must not grant selling through the back door.
        if auction.only_approved_sellers:
            member.selling_allowed = False
        if auction.only_approved_bidders:
            member.bidding_allowed = False
        if bidder_number and not ClubMember.objects.filter(club=club, bidder_number=bidder_number).exists():
            # Club-unique, so only when free.
            member.bidder_number = bidder_number
        member.save()
        created = True
    elif member.user_id is None and user is not None:
        member.user = user
        member.save(update_fields=["user"])
    if not member.bidder_number:
        member.generate_bidder_number(save=True)
    if created:
        ClubHistory.objects.create(
            club=club,
            user=member.user,
            applies_to="MEMBERS",
            action=f"{member.name} joined via auction '{auction.title}'",
        )
    return member, created


def join_auction(user, auction, pickup_location, *, time_spent_reading_rules=0):
    """Sign ``user`` up for ``auction``, or update their existing record.

    Shared by the Join button and the assistant. Returns ``(tos, created, problem)``; ``problem`` is
    ``""``, ``"phone_number"`` or ``"address"``: a missing detail the caller words its own way.
    """
    from django.utils import timezone as django_timezone

    userdata = user.userdata
    if auction.require_phone_number and not userdata.phone_number:
        return None, False, "phone_number"
    if pickup_location is not None and pickup_location.pickup_by_mail and not userdata.address:
        return None, False, "address"

    find_by_email = AuctionTOS.objects.filter(email=user.email, auction=auction).first()
    is_new_join = False
    if find_by_email:
        # Added by email before signing in and also joined by user id: keep the oldest, fold the other.
        existing_by_user = AuctionTOS.objects.filter(user=user, auction=auction).exclude(pk=find_by_email.pk).first()
        if existing_by_user:
            if (
                find_by_email.createdon
                and existing_by_user.createdon
                and find_by_email.createdon < existing_by_user.createdon
            ):
                canonical, duplicate = find_by_email, existing_by_user
            else:
                canonical, duplicate = existing_by_user, find_by_email
            canonical.merge_duplicate(duplicate, reason="duplicate detected on join")
            obj = canonical
        else:
            obj = find_by_email
            obj.user = user
    else:
        obj, is_new_join = AuctionTOS.objects.get_or_create(
            user=user,
            auction=auction,
            defaults={
                "pickup_location": pickup_location,
                # Seed the email so save()'s email-change guard doesn't unlink the user. ``None``,
                # not "", keeps the email__isnull admin filter working.
                "email": user.email or None,
            },
        )
    if pickup_location is not None:
        obj.pickup_location = pickup_location
    if obj.pickup_location and obj.pickup_location.pickup_by_mail and not userdata.address:
        return None, False, "address"
    obj.time_spent_reading_rules = max(obj.time_spent_reading_rules or 0, time_spent_reading_rules or 0)
    # Joining means not manually added, whoever created the row.
    obj.manually_added = False
    if obj.email_address_status == "UNKNOWN":
        obj.email_address_status = "VALID"
    if not obj.name or obj.name == "Unknown":
        obj.name = f"{user.first_name} {user.last_name}".strip()
    if not obj.email:
        obj.email = user.email
    if not obj.phone_number:
        obj.phone_number = userdata.phone_number
    if not obj.address:
        obj.address = userdata.address
    if auction.is_club_managed:
        # The club owns the number and permissions; shared with the app's proximity join.
        club_member, _created = ensure_club_member(
            auction,
            user=user,
            name=obj.name,
            email=obj.email,
            phone_number=obj.phone_number or "",
            address=obj.address or "",
            admin_edited=False,
        )
        apply_club_member_to_tos(auction, obj, club_member)
    obj.save()
    userdata.last_auction_used = auction
    userdata.last_activity = django_timezone.now()
    userdata.save()
    if auction.is_club_managed and obj.clubmember_id:
        obj.clubmember.update_last_club_activity()
    if is_new_join:
        auction.create_history(
            applies_to="USERS",
            action=f"{obj.name} has joined this auction",
            user=user,
        )
    return obj, is_new_join, ""


def existing_tos_for_club_member(auction, member):
    """The participant row already linked to *member* in *auction*, or None.

    Creating a member creates its shadow, so adopt it: a second row means two invoices. None when
    there is nothing to adopt (e.g. the auction is already invoiced).
    """
    if member is None or not auction.is_club_managed:
        return None
    return AuctionTOS.objects.filter(auction=auction, clubmember=member).order_by("createdon").first()


CLUB_MANAGED_MODES = ("all", "checkin")


def club_managed_auctions_for(club):
    """Every club-managed auction of *club*, finished ones included: where a new member gets a shadow row,
    so where their bidder number must be free. One definition for the form's warning and the save.
    """
    return Auction.objects.filter(
        club=club,
        is_deleted=False,
        manage_users_through_club__in=CLUB_MANAGED_MODES,
    )


def club_managed_shadows_for(member):
    """Every ``AuctionTOS`` that is *member*, across club-managed auctions, finished ones included.

    Member and shadows share one name, email, address and bidder number (:func:`sync_member_to_shadows`).
    Only ``checked_in``, the invoice and reminder flags are per auction.
    """
    return AuctionTOS.objects.filter(
        clubmember=member,
        auction__manage_users_through_club__in=CLUB_MANAGED_MODES,
    ).select_related("auction", "clubmember")


def _member_auction_ids(member):
    return list(club_managed_shadows_for(member).values_list("auction_id", flat=True))


def free_bidder_number_for(member, *, avoid=()):
    """A bidder number free in *member*'s club and every auction they're in. *avoid* counts as taken."""
    from .models import _generate_unique_bidder_number

    avoid = {str(value).strip() for value in avoid if str(value).strip()}
    auction_ids = _member_auction_ids(member)

    def is_taken(candidate):
        if candidate in avoid:
            return True
        if (
            ClubMember.objects.filter(club_id=member.club_id, bidder_number=candidate)
            .exclude(pk=member.pk or 0)
            .exists()
        ):
            return True
        return (
            AuctionTOS.objects.filter(auction_id__in=auction_ids, bidder_number=candidate)
            .exclude(clubmember_id=member.pk)
            .exists()
        )

    return _generate_unique_bidder_number(
        is_taken=is_taken,
        # Keep their existing number when still free: displace people as little as possible.
        preferred=(member.bidder_number or "").strip() or None,
        phone=member.phone_number,
        address=member.address,
    )


def clear_bidder_number_in(auction, number, *, keep_tos=None, acting_user=None, _seen=None):
    """Move everyone except *keep_tos* off bidder *number* in *auction*.

    A displaced club member is renumbered everywhere; a row with no member only here. Each move is
    recorded in the auction history.
    """
    from .models import _generate_unique_bidder_number

    number = (number or "").strip()
    if not number:
        return
    _seen = set() if _seen is None else _seen
    rows = AuctionTOS.objects.filter(auction=auction, bidder_number=number).select_related("clubmember")
    if keep_tos is not None and keep_tos.pk:
        rows = rows.exclude(pk=keep_tos.pk)
    for row in rows:
        if row.clubmember_id and row.clubmember_id not in _seen:
            replacement = free_bidder_number_for(row.clubmember, avoid=[number])
            set_member_bidder_number(row.clubmember, replacement, acting_user=acting_user, _seen=_seen)
        else:
            replacement = _generate_unique_bidder_number(
                is_taken=lambda candidate, row=row: (
                    candidate == number
                    or AuctionTOS.objects.filter(auction=auction, bidder_number=candidate).exclude(pk=row.pk).exists()
                ),
                phone=row.phone_number,
                address=row.address,
            )
            AuctionTOS.objects.filter(pk=row.pk).update(bidder_number=replacement)
        auction.create_history(
            applies_to="USERS",
            action=f"Bidder number {number} was given to somebody else, so {row.name} is now {replacement}",
            user=acting_user,
        )


def set_member_bidder_number(member, number, *, acting_user=None, _seen=None):
    """Give *member* bidder number *number* in the club and every auction they're in.

    The one writer of bidder numbers in club-managed mode; the current holder is moved off first.
    ``update()`` rather than ``save()`` to avoid re-entering the post_save signal; ``_seen`` stops a
    displacement chain moving anyone twice.
    """
    number = (number or "").strip()
    if not number or member is None:
        return
    _seen = set() if _seen is None else _seen
    if member.pk in _seen:
        return
    _seen.add(member.pk)
    # Club scope first: (club, bidder_number) is a database unique constraint.
    for other in ClubMember.objects.filter(club_id=member.club_id, bidder_number=number).exclude(pk=member.pk):
        set_member_bidder_number(
            other, free_bidder_number_for(other, avoid=[number]), acting_user=acting_user, _seen=_seen
        )
    ClubMember.objects.filter(pk=member.pk).update(bidder_number=number)
    member.bidder_number = number
    for shadow in club_managed_shadows_for(member):
        clear_bidder_number_in(shadow.auction, number, keep_tos=shadow, acting_user=acting_user, _seen=_seen)
        AuctionTOS.objects.filter(pk=shadow.pk).update(bidder_number=number)


def bidder_number_holder_in(auction, number, *, exclude_tos=None):
    """Who holds bidder *number* in *auction*, or None. For warnings only; the number always goes where sent."""
    number = (number or "").strip()
    if not number:
        return None
    others = AuctionTOS.objects.filter(auction=auction, bidder_number=number).select_related("clubmember")
    if exclude_tos is not None and exclude_tos.pk:
        others = others.exclude(pk=exclude_tos.pk)
    return others.first()


#: The fields a club member and their participant rows share; everything a person types.
SHARED_MEMBER_FIELDS = ("name", "email", "phone_number", "address")


def shared_member_values(member):
    """*member*'s shared details, truncated to ``AuctionTOS`` widths (name 181 vs 200). ``update()``
    isn't validated, so an over-long name would raise DataError 1406 on every later save.
    """
    values = {}
    for field in SHARED_MEMBER_FIELDS:
        limit = AuctionTOS._meta.get_field(field).max_length
        value = getattr(member, field, None) or ""
        values[field] = value[:limit] if limit else value
    return values


def sync_member_to_shadows(member, *, acting_user=None):
    """Push *member*'s shared details onto every participant row.

    ``update()``, not ``save()``, which would merge same-email rows. Email status is cleared too, so an
    old bounce doesn't suppress mail to a corrected address.
    """
    values = shared_member_values(member)
    for shadow in club_managed_shadows_for(member):
        update = {field: value for field, value in values.items() if (getattr(shadow, field, None) or "") != value}
        if not update:
            continue
        if "email" in update:
            update["email_address_status"] = "UNKNOWN"
            clash = (
                AuctionTOS.objects.filter(auction_id=shadow.auction_id, email__iexact=update["email"])
                .exclude(pk=shadow.pk)
                .exists()
            )
            if clash:
                logger.warning(
                    "AuctionTOS pk=%s now shares email '%s' with another row in auction pk=%s",
                    shadow.pk,
                    update["email"],
                    shadow.auction_id,
                )
        AuctionTOS.objects.filter(pk=shadow.pk).update(**update)


def apply_club_member_to_tos(auction, tos, member):
    """Copy *member*'s bidder number and permissions onto *tos* without saving. No-op if not club-managed."""
    if member is None or not auction.is_club_managed:
        return tos
    tos.clubmember = member
    number = (member.bidder_number or "").strip()
    if number:
        clear_bidder_number_in(auction, number, keep_tos=tos)
        tos.bidder_number = number
    # Not contact details: the caller set those from what the admin typed. They flow up to the member
    # through signals.sync_auctiontos_up_to_clubmember on save.
    if auction.use_check_in_mode and not tos.checked_in:
        # Check-in mode: bidding comes from checking in, as in propagate_clubmember_to_shadow_tos.
        tos.bidding_allowed = False
    else:
        tos.bidding_allowed = member.bidding_allowed
    tos.selling_allowed = member.selling_allowed
    return tos


def check_in_auctiontos(tos, *, acting_user, bidder_number="", note=""):
    """Check a participant in: stamp ``checked_in``, allow bidding, optionally set a bidder number.

    Shared by the check-in modal and the palette. Idempotent apart from the history line. ``note`` is
    appended to it so assistant check-ins show in ``recent_changes``. Returns the ``AuctionTOS``.
    """
    bidder_number = (bidder_number or "").strip()
    update_fields = []
    if not tos.checked_in:
        tos.checked_in = timezone.now()
        update_fields.append("checked_in")
    if not tos.bidding_allowed:
        tos.bidding_allowed = True
        update_fields.append("bidding_allowed")
    if update_fields:
        tos.save(update_fields=update_fields)
    if bidder_number and bidder_number != tos.bidder_number:
        tos.force_set_bidder_number(bidder_number, acting_user=acting_user)
    tos.auction.create_history(
        applies_to="USERS",
        action=f"Checked in {tos.name}{f' {note}' if note else ''}",
        user=acting_user,
    )
    return tos


def undo_check_in_auctiontos(tos, *, acting_user, note=""):
    """Undo a check-in: clear ``checked_in`` and record it. Leaves ``bidding_allowed`` alone, since other
    things grant it too.
    """
    if tos.checked_in:
        tos.checked_in = None
        tos.save(update_fields=["checked_in"])
    tos.auction.create_history(
        applies_to="USERS",
        action=f"Undid the check-in for {tos.name}{f' {note}' if note else ''}",
        user=acting_user,
    )
    return tos


def draw_door_prize(auction, *, acting_user):
    """Pick a random checked-in participant without a door prize, or None.

    Shared by the door-prize page and the palette. ``secrets.choice`` so "was it rigged?" has an answer.
    """
    import secrets

    candidate_ids = list(
        AuctionTOS.objects.filter(
            auction=auction,
            checked_in__isnull=False,
            door_prize_called__isnull=True,
        ).values_list("pk", flat=True)
    )
    if not candidate_ids:
        return None
    winner = AuctionTOS.objects.get(pk=secrets.choice(candidate_ids))
    winner.door_prize_called = timezone.now()
    winner.save(update_fields=["door_prize_called"])
    auction.create_history(
        applies_to="USERS",
        action=f"Picked door prize winner {winner.name}",
        user=acting_user,
    )
    return winner


# Why lots can't be added; each caller picks its own wording.
LOT_ADD_BLOCK_NO_TOS = "no_tos"
LOT_ADD_BLOCK_SELLING_NOT_ALLOWED = "selling_not_allowed"
LOT_ADD_BLOCK_SUBMISSION_ENDED = "submission_ended"
LOT_ADD_BLOCK_BULK_DISABLED = "bulk_disabled"


def lot_add_block(auction, tos, is_admin, *, bulk=True):
    """``(code, message)`` for why lots can't be added here, or ``None``.

    Shared by bulk add and the palette's ``add_lot``. Admins skip all but the join check.
    ``bulk=False`` skips only ``allow_bulk_adding_lots``, which is about the page, not selling.
    """
    if not tos:
        return LOT_ADD_BLOCK_NO_TOS, "You can't add lots until you join this auction"
    if not tos.selling_allowed and not is_admin:
        return LOT_ADD_BLOCK_SELLING_NOT_ALLOWED, "You don't have permission to add lots to this auction"
    if not is_admin and not auction.can_submit_lots:
        return LOT_ADD_BLOCK_SUBMISSION_ENDED, f"Lot submission has ended for {auction}"
    if bulk and not is_admin and not auction.allow_bulk_adding_lots:
        return (
            LOT_ADD_BLOCK_BULK_DISABLED,
            "Bulk adding lots has been disabled in this auction, add your lots one at a time using this form",
        )
    return None


def save_new_lot(lot, *, auction, tos, added_by):
    """Attach a new lot to seller and auction and save it. The caller recalculates the seller's invoice
    (bulk add does it once per batch).
    """
    lot.auctiontos_seller = tos
    lot.auction = auction
    # lot_owner, not tos.user: an unlinked TOS would leave lot.user null and lock the seller out.
    owner = tos.lot_owner(added_by)
    if owner:
        lot.user = owner
    lot.added_by = added_by
    lot.save()
    return lot


def recalculate_seller_invoice(auction, tos):
    """Ensure the seller has an invoice for this auction and recalculate it."""
    from .models import Invoice

    invoice = Invoice.objects.filter(auctiontos_user=tos, auction=auction).first()
    if not invoice:
        invoice = Invoice.objects.create(auctiontos_user=tos, auction=auction)
    invoice.recalculate()
    return invoice


# ---------------------------------------------------------------------------
# Copying a lot ("Copy to new lot")
# ---------------------------------------------------------------------------
#
# The web button pre-fills a form; the palette's add_lot creates the lot outright. Both use these.


#: The lot fields "Copy to new lot" carries over.
CLONE_LOT_FIELDS = (
    "lot_name",
    "quantity",
    "species",
    "species_category",
    "summernote_description",
    "i_bred_this_fish",
    "reserve_price",
    "buy_now_price",
    "reference_link",
    "donation",
    "custom_checkbox",
    "custom_field_1",
    "custom_dropdown",
)


def user_can_clone_lot(user, lot) -> bool:
    """Whether *user* may copy *lot*: their own lots only (superusers: any)."""
    if not (user and lot):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return bool(lot.user_id and lot.user_id == user.pk)


def clone_lot_values(lot) -> dict:
    """The values a copy of *lot* starts with. Foreign keys come back as instances (form ``initial``); a
    caller building form data swaps in pks. The scientific name is kept unless the target auction has
    the field off.
    """
    return {field: getattr(lot, field) for field in CLONE_LOT_FIELDS}


def copy_lot_images(original_lot, new_lot):
    """Copy every image from *original_lot* onto the saved *new_lot*. Returns the new rows.

    Rows share the file and Cloudflare image. A picture of a sold lot becomes "representative".
    """
    from easy_thumbnails.files import get_thumbnailer

    from .models import LotImage

    copies = []
    for original_image in LotImage.objects.filter(lot_number=original_lot.lot_number):
        new_image = LotImage.objects.create(
            createdon=original_image.createdon,
            lot_number=new_lot,
            image_source=original_image.image_source,
            is_primary=original_image.is_primary,
            url=original_image.url,
        )
        if original_image.image:
            new_image.image = get_thumbnailer(original_image.image)
            new_image.cloudflare_image_id = original_image.cloudflare_image_id
        if original_lot.winner and original_image.image_source == "ACTUAL":
            new_image.image_source = "REPRESENTATIVE"
        new_image.save()
        copies.append(new_image)
    return copies


def promoting_makes_it_the_clubs_current_auction(auction, was_promoted) -> bool:
    """Turning promotion on makes an auction its club's current one. True if it did.

    Only on the transition, so saving an unrelated setting doesn't steal the club's current auction.
    """
    if was_promoted or not auction.promote_this_auction or not auction.club_id:
        return False
    club = auction.club
    if club.current_auction_id == auction.pk:
        return False
    club.current_auction = auction
    club.save(update_fields=["current_auction"])
    return True


#: Auction settings a copy inherits. A field missing here is reset to the default on copy.
#: ``tests.AuctionCloneCustomFieldsTests`` fails if the custom fields form outgrows it.
AUCTION_FIELDS_TO_CLONE = [
    "is_online",
    "summernote_description",
    "lot_entry_fee",
    "unsold_lot_fee",
    "winning_bid_percent_to_club",
    "first_bid_payout",
    "club_member_discount",
    "sealed_bid",
    "max_lots_per_user",
    "allow_additional_lots_as_donation",
    "make_stats_public",
    "use_categories",
    "bump_cost",
    "is_chat_allowed",
    "lot_promotion_cost",
    "online_bidding",
    "pre_register_lot_discount_percent",
    "only_approved_sellers",
    "only_approved_bidders",
    "email_users_when_invoices_ready",
    "invoice_payment_instructions",
    "minimum_bid",
    "winning_bid_percent_to_club_for_club_members",
    "lot_entry_fee_for_club_members",
    "registration_fee",
    "registration_fee_for_club_members",
    "set_lot_winners_url",
    "require_phone_number",
    "buy_now",
    "reserve_price",
    "tax",
    "advanced_lot_adding",
    "date_online_bidding_starts",
    "date_online_bidding_ends",
    "allow_deleting_bids",
    "auto_add_images",
    "message_users_when_lots_sell",
    "label_print_fields",
    "use_scientific_name",
    "force_donation_threshold",
    "use_quantity_field",
    "use_custom_checkbox_field",
    "custom_checkbox_name",
    "custom_field_1",
    "custom_field_1_name",
    "use_reference_link",
    "use_description",
    "use_custom_dropdown_field",
    "custom_dropdown_name",
    "allow_bulk_adding_lots",
    "copy_users_when_copying_this_auction",
    "use_donation_field",
    "use_i_bred_this_fish_field",
    "use_seller_dash_lot_numbering",
    "enable_online_payments",
    "enable_square_payments",
    "add_membership_fee_to_invoices_for_expired_members",
    "alternate_split_mode",
    "alternative_split_label",
    "google_drive_link",
    "only_whole_dollar_bids",
    "club",
    "manage_users_through_club",
    "allow_self_checkin",
    "exact_location_set",
]

#: Participant columns about one auction rather than the person, blanked when people are copied.
#: ``checked_in`` would open check-in mode with everyone through the door; ``possible_duplicate``
#: points at the old auction. Name, contact, bidder number, memo and permissions are carried.
PER_RUN_TOS_STATE = {
    "checked_in": None,
    "door_prize_called": None,
    "confirm_email_sent": False,
    "second_confirm_email_sent": False,
    "print_reminder_email_sent": False,
    "time_spent_reading_rules": 0,
    "possible_duplicate": None,
}

DEFAULT_AUCTION_DESCRIPTION = """
            <h4>General information</h4>
            You should remove this line and edit this section to suit your auction.
            Use the formatting here as an example.<br><br>
            <h4>Rules</h4>
            <ul><li>You cannot sell anything banned by state law.</li>
            <li>All lots must be properly bagged.  No leaking bags!</li>
            <li>You do not need to be a club member to buy or sell lots.</li></ul>"""


def auction_to_copy(user):
    """The auction "copy my last auction" means, or ``None``. By ``-date_start``: in-person auctions have
    no ``date_end``.
    """
    from .models import Auction

    for auction in Auction.objects.exclude(is_deleted=True).filter(created_by=user).order_by("-date_start")[:20]:
        if auction.permission_check(user):
            return auction
    return None


def clone_auction(source, *, title, date_start, created_by, note=""):
    """Create a new auction from ``source``, minus its dates and bids.

    Shared by the create page's copy button and ``palette_actions.create_auction``. Copies
    :data:`AUCTION_FIELDS_TO_CLONE`, pickup locations (times shifted), dropdown options, and people
    (minus :data:`PER_RUN_TOS_STATE`) when the source says so and the copy isn't club-managed. Dates
    keep the source's offsets.
    """
    from .models import Auction, AuctionDropdown, PickupLocation

    auction = Auction(title=title, created_by=created_by, date_start=date_start)
    # Never inherited: promotion is a decision made each time.
    auction.promote_this_auction = False
    for field in AUCTION_FIELDS_TO_CLONE:
        setattr(auction, field, getattr(source, field))
    run_duration = timezone.timedelta(days=7)
    online_bidding_start_diff = timezone.timedelta(days=7)
    online_bidding_end_diff = timezone.timedelta(minutes=0)
    lot_submission_end_date_diff = timezone.timedelta(minutes=0)
    if source.date_end:
        run_duration = source.date_end - source.date_start
    if source.date_online_bidding_starts:
        online_bidding_start_diff = source.date_start - source.date_online_bidding_starts
    if source.date_online_bidding_ends:
        online_bidding_end_diff = source.date_start - source.date_online_bidding_ends
    if source.lot_submission_end_date:
        lot_submission_end_date_diff = source.date_start - source.lot_submission_end_date
    # There is no cloned_from column; finish_new_auction writes the source into the history.
    if not auction.summernote_description:
        auction.summernote_description = DEFAULT_AUCTION_DESCRIPTION
    if auction.is_online:
        auction.date_end = auction.date_start + run_duration
        if not auction.lot_submission_end_date:
            auction.lot_submission_end_date = auction.date_end
        if not auction.lot_submission_start_date:
            auction.lot_submission_start_date = auction.date_start
    else:
        auction.date_end = None
        if not auction.lot_submission_end_date:
            auction.lot_submission_end_date = auction.date_start - lot_submission_end_date_diff
        if not auction.lot_submission_start_date:
            auction.lot_submission_start_date = auction.date_start - run_duration
        if not auction.date_online_bidding_starts:
            auction.date_online_bidding_starts = auction.date_start - online_bidding_start_diff
        if not auction.date_online_bidding_ends:
            auction.date_online_bidding_ends = auction.date_start - online_bidding_end_diff
    auction.save()

    for location in PickupLocation.objects.filter(auction=source):
        location.pk = None  # duplicate all fields
        if location.name == str(source):
            location.name = str(auction)
        location.auction = auction
        auction_time = source.date_end or source.date_start
        if location.pickup_time:
            first_time_diff = location.pickup_time - auction_time
            location.pickup_time = (auction.date_end or auction.date_start) + first_time_diff
        if location.second_pickup_time:
            second_time_diff = location.second_pickup_time - auction_time
            location.second_pickup_time = (auction.date_end or auction.date_start) + second_time_diff
        location.save()

    # Club-managed auctions never copy people: participants are the club's members, and check-in mode
    # exists to create rows at the door.
    if source.copy_users_when_copying_this_auction and not auction.is_club_managed:
        for tos in AuctionTOS.objects.filter(auction=source):
            # save() resets bid permissions on a new row; restore them after.
            original_bid_permission = tos.bidding_allowed
            tos.pk = None
            tos.createdon = None
            tos.auction = auction
            tos.manually_added = True
            for field, blank in PER_RUN_TOS_STATE.items():
                setattr(tos, field, blank)
            if tos.pickup_location.name == str(source):
                new_location_name = str(auction)
            else:
                new_location_name = tos.pickup_location.name
            new_location = PickupLocation.objects.filter(auction=auction, name=new_location_name).first()
            if new_location:
                tos.pickup_location = new_location
                tos.save()
                tos.bidding_allowed = original_bid_permission
                tos.save()  # see comment above

    for dropdown_option in AuctionDropdown.objects.filter(auction=source):
        AuctionDropdown.objects.create(auction=auction, user=dropdown_option.user, value=dropdown_option.value)

    finish_new_auction(auction, created_by, copied_from=source, note=note)
    return auction


def finish_new_auction(auction, created_by, *, copied_from=None, note=""):
    """The bookkeeping every new auction gets: a history line, the creator's club if permitted,
    ``last_auction_used``, and the club's admins as auction admins. ``note`` is ``palette_actions.via``.
    """
    from .views import check_club_permission
    from .views.auction_pages import _add_club_admins_as_auction_tos

    action = "Created auction"
    if copied_from:
        action += f" by copying {copied_from}"
    if note:
        action += f" {note}"
    auction.create_history(applies_to="RULES", action=action, user=created_by)
    # The creator's club, if they have admin or manage_auctions there.
    if not auction.club:
        creator_club = created_by.userdata.club
        if creator_club and (
            check_club_permission(created_by, creator_club, "permission_admin")
            or check_club_permission(created_by, creator_club, "permission_manage_auctions")
        ):
            auction.club = creator_club
            auction.save(update_fields=["club"])
            auction.create_history(
                applies_to="RULES",
                action=f"Automatically associated with club '{creator_club}' based on auction creator's preferences.",
                user=None,
            )
    created_by.userdata.last_auction_used = auction
    created_by.userdata.save(update_fields=["last_auction_used"])
    # Also called from PickupLocationsCreate, once a location exists.
    _add_club_admins_as_auction_tos(auction, created_by)


def link_auction_to_club(auction, club, *, note, actor=None, grant_admin=True):
    """Attach an auction to a club after the fact, and optionally make its creator a club admin.

    For organizers whose auctions predate their club. Shared by ``assign_auction_to_club`` and
    ``LinkAuctionToClub``. ``save()``, not ``update()``: attaching a club books settled invoices to the
    club ledger. Returns True when an admin was granted.
    """
    auction.club = club
    auction.save(update_fields=["club"])
    auction.create_history(applies_to="RULES", action=f"Assigned to club '{club}' {note}.", user=actor)
    if grant_admin and auction.created_by:
        return ensure_club_admin(club, auction.created_by, note=note, actor=actor)
    return False


def ensure_club_admin(club, user, *, note, actor=None):
    """Make ``user`` an admin of ``club``, reusing their membership row. True only when newly granted.
    Contact fields are filled from the account without overwriting the club's.
    """
    member = ClubMember.objects.filter(club=club, user=user, is_deleted=False).first()
    if member and member.permission_admin:
        return False
    if not member:
        member = ClubMember(club=club, user=user, source="manually_added")
    userdata = getattr(user, "userdata", None)
    member.name = member.name or user.get_full_name() or user.username
    if not member.email:
        member.email = user.email or None
    if not member.phone_number:
        member.phone_number = getattr(userdata, "phone_number", None) or None
    if not member.address:
        member.address = getattr(userdata, "address", None) or ""
    member.permission_admin = True
    member.save()
    ClubHistory.objects.create(
        club=club,
        user=actor,
        action=f"Granted admin permissions to {member.name} {note}.",
        applies_to="MEMBERS",
    )
    return True


# --- breeder award points ----------------------------------------------------


def bap_review_lots(club):
    """Every lot in this club's auctions its points desk could decide on; the Pending BAP page's base
    queryset. Status filtering is ``filters.ClubBapLotFilter``. ``Exists(matching_member)`` keeps
    strangers' lots at shared auctions out.
    """
    from django.db.models import Exists, OuterRef, Q

    from .models import ClubMember, Lot

    matching_member = ClubMember.objects.filter(
        club=club,
        is_deleted=False,
    ).filter(
        Q(user=OuterRef("auctiontos_seller__user"))
        | Q(user=OuterRef("user"))
        | Q(email__iexact=OuterRef("auctiontos_seller__email"))
    )
    lots = Lot.objects.filter(auction__club=club, is_deleted=False, active=False)
    if club.only_sold_lots:
        lots = lots.filter(auctiontos_winner__isnull=False, winning_price__isnull=False)
    return (
        lots.filter(Exists(matching_member))
        .select_related("auctiontos_seller__user", "auction__club", "species_category", "species")
        .prefetch_related("bap_award")
        .order_by("-date_end")
    )


#: The three decisions on a lot's points. ``undo`` returns it to pending.
BAP_DECISIONS = ("approve", "deny", "undo")


def review_lot_points(lot, club, *, acting_user, decision, bap=0, hap=0, cap=0):
    """Approve, deny or undo one lot's breeder award points. Returns the ``BapAward`` or ``None``.

    The caller checks ``permission_manage_bap``. Every decision writes a history line, including undo.
    ``deny`` leaves ``bap_auto_reason`` alone: the site's own verdict stays visible.
    """
    from .models import BapAward

    if decision not in BAP_DECISIONS:
        message = f"{decision!r} is not one of {BAP_DECISIONS}"
        raise ValueError(message)
    seller = _bap_seller_name(lot)

    if decision == "undo":
        existing = BapAward.objects.filter(lot=lot).first()
        if not existing and not lot.manually_approved:
            # Nothing decided: a quiet no-op, since review_points is idempotent and hosts retry.
            return None
        if existing:
            existing.delete()
        lot.bap_points_awarded = 0
        lot.manually_approved = False
        lot.bap_auto_reason = lot.sold_lot_no_bap_reason or ""
        lot.save(update_fields=["bap_points_awarded", "manually_approved", "bap_auto_reason"])
        ClubHistory.objects.create(
            club=club,
            user=acting_user,
            action=f"Undid the points decision for {seller}: {lot.lot_name}",
            applies_to="BAP",
        )
        return None

    if decision == "deny":
        existing = BapAward.objects.filter(lot=lot).first()
        if existing:
            existing.delete()
        lot.bap_points_awarded = 0
        lot.manually_approved = True
        lot.save(update_fields=["bap_points_awarded", "manually_approved"])
        ClubHistory.objects.create(
            club=club,
            user=acting_user,
            action=f"Rejected BAP points for {seller}: {lot.lot_name}",
            applies_to="BAP",
        )
        return None

    bap, hap, cap = (max(0, int(value or 0)) for value in (bap, hap, cap))
    if not (bap or hap or cap):
        return None
    member = bap_member_for_lot(lot, club)
    if not member:
        return None
    award, _created = BapAward.objects.update_or_create(
        lot=lot,
        defaults={
            "club_member": member,
            "date": lot.date_end.date() if lot.date_end else timezone.now().date(),
            "points": bap,
            "hap_points": hap,
            "cap_points": cap,
            "awarded_by": acting_user,
        },
    )
    lot.bap_points_awarded = bap + hap + cap
    lot.manually_approved = True
    lot.bap_auto_reason = ""
    lot.save(update_fields=["bap_points_awarded", "manually_approved", "bap_auto_reason"])
    ClubHistory.objects.create(
        club=club,
        user=acting_user,
        action=f"Awarded {lot.bap_points_awarded} BAP point(s) to {seller} for {lot.lot_name}",
        applies_to="BAP",
    )
    return award


def bap_member_for_lot(lot, club):
    """The club member credited for this lot: its seller, by account then email (for members with no account)."""
    seller_user = lot.user or (lot.auctiontos_seller.user if lot.auctiontos_seller else None)
    seller_email = (lot.auctiontos_seller.email if lot.auctiontos_seller else None) or (
        seller_user.email if seller_user else None
    )
    member = None
    if seller_user:
        member = ClubMember.objects.filter(club=club, user=seller_user, is_deleted=False).first()
    if not member and seller_email:
        member = ClubMember.objects.filter(club=club, email__iexact=seller_email, is_deleted=False).first()
    return member


def _bap_seller_name(lot):
    """Whoever brought the lot, for a history line. ``LotBapPointsView._seller_name``, verbatim."""
    if lot.auctiontos_seller:
        return lot.auctiontos_seller.name
    if lot.user:
        return f"{lot.user.first_name} {lot.user.last_name}".strip() or lot.user.username or f"user #{lot.user.pk}"
    return f"lot #{lot.pk}"


#: How recent a participant row must be to receive a contact-info change. Older rows are a record
#: of who stood at a desk that day and are left alone.
CONTACT_INFO_RECENT_DAYS = 30


def recent_auctiontos_for(user):
    """Participant rows a contact-info change follows into. ``manually_added`` rows are skipped: the
    admin's version wins.
    """
    from datetime import timedelta

    cutoff = timezone.now() - timedelta(days=CONTACT_INFO_RECENT_DAYS)
    return AuctionTOS.objects.filter(
        user=user,
        manually_added=False,
        createdon__gte=cutoff,
    ).select_related("auction")


#: Session flag (not a querystring, which could be stripped into a redirect loop) for a gate that
#: needs a phone number.
CONTACT_GATE_NEEDS_PHONE = "contact_gate_needs_phone"


def missing_contact_info(user, *, require_phone=False):
    """Contact fields this person left blank, in page order. Empty when nothing is missing.

    Adding a lot needs name and address; creating an auction also needs a phone. The auction gate is
    also what puts the club picker in front of an organizer, without which their auction has no club.
    """
    userdata = getattr(user, "userdata", None)
    asked_for = {
        "first name": user.first_name,
        "last name": user.last_name,
        "address": getattr(userdata, "address", ""),
    }
    if require_phone:
        asked_for["phone number"] = getattr(userdata, "phone_number", "")
    return [label for label, value in asked_for.items() if not (value or "").strip()]


def readable_list(items):
    """``["a", "b", "c"]`` -> ``"a, b and c"``.  For putting a list inside a sentence."""
    items = list(items)
    if len(items) < 2:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def propagate_contact_info(user, userdata, *, acting_user=None):
    """Push a changed name, phone or address to the auctions and clubs holding a copy.

    Shared by the contact info page and ``update_contact_info``. Returns sentences naming what changed.
    """
    from .models import AuctionHistory

    acting_user = acting_user or user
    new_name = f"{user.first_name} {user.last_name}".strip()
    new_phone = userdata.phone_number
    new_address = userdata.address
    told: list[str] = []

    for tos in recent_auctiontos_for(user):
        changes = []
        if tos.name != new_name:
            changes.append(f"name from '{tos.name}' to '{new_name}'")
            tos.name = new_name
        if tos.phone_number != new_phone:
            changes.append(f"phone from '{tos.phone_number}' to '{new_phone}'")
            tos.phone_number = new_phone
        if tos.address != new_address:
            changes.append(f"address from '{tos.address}' to '{new_address}'")
            tos.address = new_address
        if changes:
            tos.save()
            AuctionHistory.objects.create(
                auction=tos.auction,
                user=acting_user,
                action=f"Updated contact info for {new_name}: " + ", ".join(changes),
                applies_to="USERS",
            )
            told.append(str(tos.auction))

    for club_member in ClubMember.objects.filter(user=user, is_deleted=False).select_related("club"):
        changes = []
        if club_member.name != new_name:
            changes.append(f"name to '{new_name}'")
            club_member.name = new_name
        if club_member.phone_number != new_phone:
            changes.append(f"phone to '{new_phone}'")
            club_member.phone_number = new_phone
        if club_member.address != new_address:
            changes.append(f"address to '{new_address}'")
            club_member.address = new_address
        if changes:
            club_member.save()
            ClubHistory.objects.create(
                club=club_member.club,
                user=acting_user,
                action=f"Contact info updated for {user.get_full_name()}: " + ", ".join(changes),
                applies_to="MEMBERS",
            )
            told.append(club_member.club.name)

    return told
