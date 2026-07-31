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
