from django.utils import timezone

from .models import AuctionTOS, ClubHistory, ClubMember

# Source of truth for ClubMember fields acceptable via API ingest.
# Note: ``first_name`` and ``last_name`` are accepted as aliases but stored as ``name``.
INGEST_ALLOWED_FIELDS = frozenset({"name", "email", "phone_number", "address", "memo"})


def map_fields(data: dict, api_key) -> dict:
    """Rename incoming keys using ClubAPIKeyFieldMap records for this api_key.

    Special case: if ``first_name`` and/or ``last_name`` are present (either sent
    directly or mapped to those names), they are combined into a single ``name``
    field (unless ``name`` is already set).
    """
    mapping = {m.external_field: m.internal_field for m in api_key.field_mappings.all()}
    result = {mapping.get(k, k): v for k, v in data.items()}

    first = (result.pop("first_name", "") or "").strip()
    last = (result.pop("last_name", "") or "").strip()
    if not result.get("name") and (first or last):
        result["name"] = f"{first} {last}".strip()

    return result


def create_club_member_from_api(validated_data: dict, club, api_key):
    """Create a ClubMember from API-validated data, skipping duplicates by email.

    Logs a ClubHistory entry and updates api_key.last_used_at.
    Returns (member, created: bool).
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
# In a club-managed auction (``Auction.is_club_managed``) the ClubMember owns the bidder number and
# the bidding/selling permissions, and every AuctionTOS is a shadow of one. So every path that adds
# somebody to such an auction has to find or create their member record and copy those fields down —
# otherwise the participant gets an auction-only bidder number the club has never heard of, no club
# admin screen can find them, and their number won't be theirs again next year.
#
# Callers: the web rules-page join (``AuctionInfo.post``), the app's proximity join
# (``auctions.mobile.services.checkin.join_auction``), the lot CSV import's seller creation
# (``ImportLotsFromCSV``) and the app's offline "add user" op (``auctions.mobile.services.offline``).
# Separate functions rather than one because the order differs: creating a ClubMember also creates
# its shadow AuctionTOS (see ``signals.propagate_clubmember_to_shadow_tos``), so a caller that has no
# AuctionTOS yet should ensure the member first and adopt that shadow
# (:func:`existing_tos_for_club_member`), while a caller that already has one lets
# ``AuctionTOS.save()`` merge the shadow away.


def ensure_club_member(
    auction, *, user=None, name="", email="", phone_number="", address="", bidder_number="", admin_edited=True
):
    """Find or create the ClubMember for a participant in *auction*; return (member, created).

    Matches an existing member by user link first, then by email (the same order as
    ``AuctionTOS.club_member_record``), and binds *user* onto a member that was added by email only.
    A newly created member is recorded in the club's history and always leaves here with a bidder
    number: *bidder_number* when one is asked for and still free in this club (an admin writing a
    number on a card at the door), otherwise a generated one. Returns ``(None, False)`` when the
    auction isn't club-managed — there is nothing to create, and no member record is wanted for a
    plain auction.

    Pass ``admin_edited=False`` when the person is signing themselves up rather than an admin adding
    them: it marks the new row as the member's own, so deleting their account deletes it instead of
    keeping it as one of the club's records (see :mod:`auctions.account_deletion`). It only ever
    applies to a row created here; an existing record keeps whatever it already says.
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
            # AuctionTOS.save() fills a blank name with "Unknown"; don't carry that into the club.
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
        # An auction that vets its participants must not hand out permissions through the back door.
        if auction.only_approved_sellers:
            member.selling_allowed = False
        if auction.only_approved_bidders:
            member.bidding_allowed = False
        if bidder_number and not ClubMember.objects.filter(club=club, bidder_number=bidder_number).exists():
            # Honour the number the admin is handing this person (club-unique, so only when free).
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


def existing_tos_for_club_member(auction, member):
    """The participant record already linked to *member* in *auction*, or None.

    Creating a ClubMember in a club-managed auction also creates its shadow AuctionTOS, so a caller
    that ensures the member and then wants a participant record has to adopt that row: a second row
    for the same person in one auction means two invoices and a bidder number that disagrees with the
    club's. Returns None when there is nothing to adopt (no member, or the signal skipped the auction
    because it is already invoiced), leaving the caller to create the row itself.
    """
    if member is None or not auction.is_club_managed:
        return None
    return AuctionTOS.objects.filter(auction=auction, clubmember=member).order_by("createdon").first()


def apply_club_member_to_tos(auction, tos, member):
    """Copy *member*'s bidder number and permissions onto *tos*. Mutates it; does not save.

    Callers save once afterwards with their own field handling. A no-op when the auction isn't
    club-managed or there is no member.
    """
    if member is None or not auction.is_club_managed:
        return tos
    tos.clubmember = member
    tos.bidder_number = member.bidder_number
    if auction.use_check_in_mode and not tos.checked_in:
        # Check-in mode: joining never grants bidding on its own. The member has to check in at the
        # event, which sets checked_in + bidding_allowed (mirrors the auto-add path in
        # signals.propagate_clubmember_to_shadow_tos).
        tos.bidding_allowed = False
    else:
        tos.bidding_allowed = member.bidding_allowed
    tos.selling_allowed = member.selling_allowed
    return tos


def check_in_auctiontos(tos, *, acting_user, bidder_number="", note=""):
    """Check a participant in: stamp ``checked_in``, allow bidding, optionally set their bidder number.

    Extracted verbatim from ``views.AuctionCheckIn.post`` so the web check-in modal and the
    command palette's ``check_in`` action share one implementation. Idempotent -- checking in
    someone who is already checked in only writes an auction history entry.

    ``note`` is appended to the history entry. The palette passes "(command palette)" so its own
    check-ins read the same as every other write it makes -- without it, ``recent_changes`` and any
    audit built on it were blind to the single most common thing the assistant does at a live event,
    purely because that one write happens to go through shared code.

    Permission is the caller's job (both callers gate on ``can_add_edit_people``).
    Returns the ``AuctionTOS``.
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


def draw_door_prize(auction, *, acting_user):
    """Pick a random checked-in participant who hasn't won a door prize yet.

    Extracted verbatim from ``views.AuctionDoorPrizes.post`` so the door-prize page and the command
    palette's ``draw_door_prize`` action draw from the same pool by the same rule. ``door_prize_called``
    is a per-person timestamp, so "already won one" is simply "has one set", and drawing twice can
    never pick the same person.

    ``secrets.choice`` rather than ``random.choice``: this is a draw in front of a room, and the
    only defensible answer to "was that rigged?" is a cryptographic RNG.

    Permission is the caller's job (both callers gate on ``can_add_edit_people``).
    Returns the winning ``AuctionTOS``, or ``None`` when nobody is left to draw.
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


# Why lots can't be added, keyed so each caller can pick its own destination/wording.
LOT_ADD_BLOCK_NO_TOS = "no_tos"
LOT_ADD_BLOCK_SELLING_NOT_ALLOWED = "selling_not_allowed"
LOT_ADD_BLOCK_SUBMISSION_ENDED = "submission_ended"
LOT_ADD_BLOCK_BULK_DISABLED = "bulk_disabled"


def lot_add_block(auction, tos, is_admin, *, bulk=True):
    """Return ``(code, message)`` explaining why lots can't be added here, or ``None`` when they can.

    Extracted verbatim from ``views.BulkAddLots.dispatch`` so the bulk-add page and the command
    palette's ``add_lot`` action enforce exactly the same rules (joined the auction, selling
    allowed, submission still open, bulk adding enabled). Admins bypass every check but the
    first, exactly as they do on the page.

    ``bulk=False`` skips only the ``allow_bulk_adding_lots`` check, which is about the bulk-add
    *page* rather than permission to sell: an auction with bulk adding turned off still lets
    people add lots one at a time, so the palette (which adds exactly one) passes ``False``.
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
    """Attach a new lot to its seller/auction and save it, mirroring ``views.BulkAddLots.post``.

    Sets the seller TOS, auction, owning user and ``added_by``, then saves. Callers are
    responsible for the seller's invoice afterwards (see ``recalculate_seller_invoice``) --
    the bulk-add page does that once per batch rather than once per lot.
    Shared with the palette's ``add_lot`` action so a lot added by voice is identical to one
    added on the bulk-add page.
    """
    lot.auctiontos_seller = tos
    lot.auction = auction
    if tos.user:
        lot.user = tos.user
    lot.added_by = added_by
    lot.save()
    return lot


def recalculate_seller_invoice(auction, tos):
    """Make sure the seller has an invoice for this auction and recalculate it.

    Same three lines every lot-creating view runs after saving; shared so the palette's
    ``add_lot`` action can't forget it.
    """
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
# The button on the lot page (``view_lot_images.html``) links to ``new_lot?copy=<pk>``, which
# pre-fills the form from the old lot (``forms.CreateLotForm.__init__``) and then copies its images
# once the new lot is saved (``views.LotValidation.form_valid``). The palette's ``add_lot`` does the
# same thing without a form in between -- it creates the lot outright -- so the field list, the
# ownership rule and the image copy live here rather than in either caller. Change what "copy"
# means once and both paths change together.


#: The lot fields "Copy to new lot" carries over. Read by ``CreateLotForm`` to pre-fill the form and
#: by the palette's ``add_lot`` to seed a lot directly.
CLONE_LOT_FIELDS = (
    "lot_name",
    "quantity",
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
    """Whether *user* may copy *lot*. You can only clone your own lots (superusers, anything)."""
    if not (user and lot):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return bool(lot.user_id and lot.user_id == user.pk)


def clone_lot_values(lot) -> dict:
    """The values from *lot* that a copy starts out with, keyed by field name.

    Permission is the caller's job -- call :func:`user_can_clone_lot` first.
    """
    return {field: getattr(lot, field) for field in CLONE_LOT_FIELDS}


def copy_lot_images(original_lot, new_lot):
    """Copy every image from *original_lot* onto the already-saved *new_lot*. Returns the new rows.

    Extracted verbatim from ``views.LotValidation.form_valid``. Both rows point at the same file, so
    they share the same Cloudflare image rather than re-uploading it, and a picture of an item that
    has already sold is demoted from "the exact item" to "representative" -- because it isn't.

    Only images are copied, not watchers, views, or any other related model. Permission is the
    caller's job (:func:`user_can_clone_lot`).
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
            # both rows share the same file, so they share the same Cloudflare image
            new_image.cloudflare_image_id = original_image.cloudflare_image_id
        # if the original lot sold, this picture sure isn't of the actual item
        if original_lot.winner and original_image.image_source == "ACTUAL":
            new_image.image_source = "REPRESENTATIVE"
        new_image.save()
        copies.append(new_image)
    return copies
