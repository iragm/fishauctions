"""The operations that are the same whoever asked: web page, API, app or assistant.

A function here is the single implementation of one thing the site can do -- join an auction, check
somebody in, create a club member, finish setting up a new auction. Views call them, the club API
calls them, the mobile endpoints call them and ``palette_actions`` calls them, which is the point:
a rule enforced in a view is a rule the API does not have, and this file is where that stops being
possible.

Permission checks are the caller's job. Nothing here asks whether the user is allowed; by the time
a service function runs, that has been settled.
"""

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


def join_auction(user, auction, pickup_location, *, time_spent_reading_rules=0):
    """Sign ``user`` up for ``auction``, or bring their existing record up to date.

    Extracted verbatim from ``views.AuctionInfo.post`` so joining is one implementation with two
    callers: the Join button, and the assistant. It was a hundred lines inside a view method, which
    is why "join me up for the fall auction" could only ever be a link to the page -- and a link is
    a poor answer on a phone to somebody standing in the room.

    Returns ``(tos, created, problem)``. ``problem`` is ``""`` or one of ``"phone_number"`` /
    ``"address"``: the auction demands a detail this account has not got, and the two callers word
    that differently (the web redirects to the contact page, the assistant says what to do). It is
    returned rather than raised because it is not an error -- it is a step the person has to take.
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
        # An admin may have typed them in by email before they ever signed in, and they may also
        # have joined under their user id -- keep the oldest row as canonical and fold the other in.
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
                # Seed the email on creation so the record never takes the None->email transition
                # that used to trip AuctionTOS.save()'s email-change guard and clear this freshly
                # linked user. ``or None`` (not "") keeps the "no email" admin filter working,
                # which relies on email__isnull.
                "email": user.email or None,
            },
        )
    if pickup_location is not None:
        obj.pickup_location = pickup_location
    if obj.pickup_location and obj.pickup_location.pickup_by_mail and not userdata.address:
        return None, False, "address"
    obj.time_spent_reading_rules = max(obj.time_spent_reading_rules or 0, time_spent_reading_rules or 0)
    # Even if this row was originally added by hand, joining means they are not manually added.
    obj.manually_added = False
    if obj.email_address_status == "UNKNOWN":
        # If it bounced in the past, the user may have had a full inbox or something.
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
        # The club owns the bidder number and permissions here, so joining has to create or link
        # the member record. Shared with the app's proximity join.
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


def undo_check_in_auctiontos(tos, *, acting_user, note=""):
    """Un-check-in a participant: clear ``checked_in`` and say so in the auction's history.

    The reversal of :func:`check_in_auctiontos`, and new -- the web has no Undo on the check-in
    modal, because at a desk with a queue in front of it the fix for a wrong name is to check in
    the right one. An assistant needs it for a different reason: it mishears, and "undo that" has
    to reach the thing it just did.

    Deliberately does **not** touch ``bidding_allowed``. Checking somebody in turns it on, but so
    do half a dozen other things, and turning it back off on the strength of an undo would quietly
    stop somebody bidding who was allowed to before any of this happened.
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
    # tos.lot_owner rather than tos.user: an unlinked TOS would otherwise leave lot.user null and
    # lock the seller out of their own lot later. See AuctionTOS.lot_owner and Lot.is_owned_by.
    owner = tos.lot_owner(added_by)
    if owner:
        lot.user = owner
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
    """Whether *user* may copy *lot*. You can only clone your own lots (superusers, anything)."""
    if not (user and lot):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return bool(lot.user_id and lot.user_id == user.pk)


def clone_lot_values(lot) -> dict:
    """The values from *lot* that a copy starts out with, keyed by field name.

    Model *instances* come back for the two foreign keys, which is what a form's ``initial`` wants.
    A caller building form *data* instead -- the command palette's relist does -- has to swap them
    for their pks, the same way it already does for ``species_category``.

    The scientific name is copied because relisting is the case it most obviously survives: it is
    the same fish, from the same breeder, a season later.  Whether it is kept is still the target
    auction's decision -- ``clean_species_for_auction`` drops it if that auction has the field
    switched off.

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


def promoting_makes_it_the_clubs_current_auction(auction, was_promoted) -> bool:
    """Turning promotion on makes an auction its club's current one. Returns True if it did.

    Extracted from ``views.AuctionUpdate.form_valid`` so the edit page and
    ``palette_actions.update_auction_setting`` cannot disagree about what promoting an auction
    means. The web version says so in a message; the assistant says so in its answer, and both are
    reading the same rule.

    Only on the transition. An auction that was already promoted must not steal the club's current
    auction back every time somebody saves an unrelated setting.
    """
    if was_promoted or not auction.promote_this_auction or not auction.club_id:
        return False
    club = auction.club
    if club.current_auction_id == auction.pk:
        return False
    club.current_auction = auction
    club.save(update_fields=["current_auction"])
    return True


#: Auction settings a copy inherits.  Anything a person set on the source auction and would expect
#: to find again next year belongs here: a field left off this list is silently reset to the model
#: default by the copy, which is how a copied auction came back with the custom checkbox switched
#: off while still carrying the name the club had given it.  ``tests.AuctionCloneCustomFieldsTests``
#: reads it and fails if the custom fields form grows a field this list does not carry.
#:
#: It lives here rather than on ``AuctionCreateView`` because the copy button on the create page is
#: no longer the only caller: ``palette_actions.create_auction`` makes the same copy for an agent,
#: and two lists would diverge on the first field somebody added.
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

#: What a participant row records about *one evening* rather than about the person, and what a copy
#: therefore has to blank.  Copying an auction copies its people when the source says to -- that is
#: the point of ``copy_users_when_copying_this_auction`` -- but it was doing it with ``tos.pk = None``
#: on a loaded row, which carries every column across, and several of these columns are answers to
#: "what happened at the last one".
#:
#: ``checked_in`` is the one that does real damage: an auction that uses check-in mode opens with
#: everybody already through the door, so the desk has nothing to do and the ``not checked_in``
#: guard on bidding never fires.  ``door_prize_called`` is the same mistake in a smaller place (last
#: year's winners are ineligible for this year's draw), and ``possible_duplicate`` is worse than
#: stale -- it is a foreign key pointing at a row in the *old* auction, so the duplicate warning on
#: the new users page links somewhere else entirely.  The two confirmation-email flags and the
#: seconds spent reading the rules are per-auction by definition: nobody has been emailed about this
#: auction yet, and nobody has read rules that may since have been rewritten.
#:
#: Everything not listed is deliberately carried: the name, the contact details, the bidder number,
#: the admin memo and the permissions are facts about the person, which is why a club copies an
#: auction in the first place.
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
    """The auction "copy my last auction" means, or ``None`` if they have never run one.

    Ordered by ``-date_start`` rather than by ``-date_end``, which is what this used to do and got
    wrong for exactly the clubs that copy the most: an in-person auction has no ``date_end`` at
    all, so on MariaDB every one of them sorted *behind* every online auction, and a club that has
    only ever run in-person auctions was offered whichever one the database happened to return.
    ``date_start`` is set on all of them.
    """
    from .models import Auction

    for auction in Auction.objects.exclude(is_deleted=True).filter(created_by=user).order_by("-date_start")[:20]:
        if auction.permission_check(user):
            return auction
    return None


def clone_auction(source, *, title, date_start, created_by, note=""):
    """Create a new auction carrying everything from ``source`` except its dates and its bids.

    Extracted from ``views.AuctionCreateView.form_valid`` so the copy button on the create page and
    :func:`auctions.palette_actions.create_auction` produce the same auction rather than two that
    drift.  What comes across: every setting in :data:`AUCTION_FIELDS_TO_CLONE`, the pickup
    locations with their times shifted to the new dates, the custom dropdown options, and -- only
    when the source says so *and* the copy is not club-managed -- the people, minus everything in
    :data:`PER_RUN_TOS_STATE`.

    The dates are *offsets*, not values: how long the source ran, and how far ahead of it lot
    submission and online bidding opened.  A copy made a year later therefore opens for lots the
    same number of days before the auction as last year's did.
    """
    from .models import Auction, AuctionDropdown, PickupLocation

    auction = Auction(title=title, created_by=created_by, date_start=date_start)
    # Never inherited, whatever the source says. An auction is listed publicly by being promoted on
    # purpose, and a copy of a promoted auction is not that decision being made a second time.
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
    # No ``cloned_from`` is written, because ``Auction`` has no such column -- the create view has
    # been assigning one to a transient attribute for years and throwing it away on save. Where the
    # copy came from is recorded where somebody can actually read it: the "Created auction by
    # copying X" line ``finish_new_auction`` writes to the auction's own history.
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

    # A club-managed auction never copies its people, whatever the source says. In that mode the
    # participants *are* the club's members: "all" creates a shadow row for every one of them, and
    # "checkin" creates the row when somebody walks through the door -- which is the entire point of
    # check-in mode, and pre-filling it from last year's list is exactly the thing that mode exists
    # to stop. Copying would also drag across everybody who has since left the club, since the
    # setting knows nothing about who is still a member.
    if source.copy_users_when_copying_this_auction and not auction.is_club_managed:
        for tos in AuctionTOS.objects.filter(auction=source):
            # in tos.save(), bid permissions are reset if there's no pk
            # to preserve them, we store them here, then resave again once the new instance is created
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
    """The bookkeeping every newly created auction gets, however it was created.

    One history line saying where it came from, the creator's club if they have the run of it, the
    "auction they last used" pointer, and the club's own admins as auction admins.  ``note`` is
    :func:`auctions.palette_actions.via`, so a club reading its own history can tell an auction an
    assistant copied from one somebody made on the site.
    """
    from .views import check_club_permission
    from .views.auction_pages import _add_club_admins_as_auction_tos

    action = "Created auction"
    if copied_from:
        action += f" by copying {copied_from}"
    if note:
        action += f" {note}"
    auction.create_history(applies_to="RULES", action=action, user=created_by)
    # Associate auction with the creator's club if they have admin or manage_auctions permission
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
    # Add club admin members as AuctionTOS admins (works for copied auctions with locations,
    # and for new auctions once a pickup location exists — also called from PickupLocationsCreate)
    _add_club_admins_as_auction_tos(auction, created_by)


# --- breeder award points ----------------------------------------------------
#
# The club's BAP/HAP/CAP review desk: which lots are waiting for a decision, and what taking one
# does. Both halves were methods on views -- ``ClubBapLotsView.get_queryset`` and
# ``LotBapPointsView.post`` -- so the only way to ask "what am I approving?" or to answer it was
# to be a browser holding a session cookie and a rendered table.


def bap_review_lots(club):
    """Every lot in this club's auctions that its points desk could have an opinion about.

    The base queryset behind the Pending BAP page, extracted from ``ClubBapLotsView`` so the
    ``points_queue`` skill lists exactly the rows the page lists. The status filtering on top of it
    is ``filters.ClubBapLotFilter``'s, for the same reason: "pending" means one particular
    combination of three columns and there must be only one place that says which.

    ``Exists(matching_member)`` is the load-bearing clause -- a lot only reaches this table when its
    seller is a member of *this* club, matched on the account or on the email, which is what stops a
    club's review queue filling up with lots sold by strangers at a shared auction.
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


#: The three things a points desk can decide about one lot. ``undo`` is not a fourth decision so
#: much as the absence of one: it puts the lot back in the pending queue it came out of.
BAP_DECISIONS = ("approve", "deny", "undo")


def review_lot_points(lot, club, *, acting_user, decision, bap=0, hap=0, cap=0):
    """Approve, deny, or un-decide one lot's breeder award points. Returns the ``BapAward`` or ``None``.

    Extracted from ``views.LotBapPointsView.post``, which rendered the row's buttons back to htmx
    and so could not be called by anything that wasn't a browser. The three branches are that view's
    own, unchanged in what they write, and the caller does the permission check
    (``permission_manage_bap``) exactly as both callers already did.

    The one thing that is new is that **undo writes a history line** -- except on a lot nobody has
    decided, where it does nothing at all. Approve and deny always wrote one; undo silently rolled
    either of them back, which was survivable while the only way to press it was to be looking at
    the table, and is not survivable now that an assistant can press it -- "every write is in the
    history with who did it" is most of what makes handing this to an agent reasonable, and a write
    that leaves no trace is the exception that would prove it wrong.

    Note what ``deny`` deliberately does *not* touch: ``bap_auto_reason`` stays as the system left
    it. That column is the site's own verdict on eligibility and stays worth showing next to a
    human's decision to overrule it.
    """
    from .models import BapAward

    if decision not in BAP_DECISIONS:
        message = f"{decision!r} is not one of {BAP_DECISIONS}"
        raise ValueError(message)
    seller = _bap_seller_name(lot)

    if decision == "undo":
        existing = BapAward.objects.filter(lot=lot).first()
        if not existing and not lot.manually_approved:
            # Nothing was ever decided, so there is nothing to take back and nothing worth a
            # history line. A quiet no-op rather than a refusal, because ``review_points`` declares
            # itself idempotent and a host retrying a dropped connection must not get an error for
            # a call that already worked. The page cannot reach this at all: a pending row has no
            # Undo button on it.
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
    """The club member who would be credited for this lot: its seller, by account then by email.

    The same two-step lookup ``Lot.unsold_lot_no_bap_reason``, ``LotBapPointsView`` and
    ``BapAwardAdminView`` each wrote out for themselves. The email half is what makes points work
    at all for somebody who has been in the club for years and never made an account here.
    """
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


#: How far back a participant row counts as "current" when contact details change.
#:
#: The contact info page has always pushed a corrected name, phone or address into the auctions the
#: person is *currently* in, and thirty days is what it means by that. Older rows are left alone
#: deliberately: an ``AuctionTOS`` is a record of who stood at a desk on a particular Saturday, and
#: rewriting last spring's address because somebody moved this week would falsify it.
CONTACT_INFO_RECENT_DAYS = 30


def recent_auctiontos_for(user):
    """The participant rows a contact-info change should follow into.

    ``manually_added`` rows are excluded because an auction admin typed those by hand for this
    person, and an admin's correction outranks the account's own details.
    """
    from datetime import timedelta

    cutoff = timezone.now() - timedelta(days=CONTACT_INFO_RECENT_DAYS)
    return AuctionTOS.objects.filter(
        user=user,
        manually_added=False,
        createdon__gte=cutoff,
    ).select_related("auction")


def propagate_contact_info(user, userdata, *, acting_user=None):
    """Push a changed name, phone or address out to the auctions and clubs that hold a copy.

    Extracted from ``views.UserLocationUpdate.form_valid`` so the assistant's ``update_contact_info``
    does exactly what the contact info page does. The copies are the point of the design: an
    ``AuctionTOS`` and a ``ClubMember`` each hold their own name and address so that a club's records
    survive the account being deleted -- which means a person who moves has to be able to correct
    all of them at once, and there is one function that knows where they all are.

    Returns a list of sentences naming what it touched, so a caller with no page to put a message on
    can say it instead.
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
