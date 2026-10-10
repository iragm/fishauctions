"""Putting a club on the site: the checks an admin's ``add_club`` proposal and a member's own "add your
club" page share, and the page's one write.

A club a member adds is created at ``PROSPECT``, which ``Club.objects.listed()`` keeps off the map, the
finder and every dropdown. The member becomes its admin straight away, so they can set it up while it
waits, and an :class:`~auctions.models.AgentProposal` asking to list it lands on
``/admin-dashboard/proposals/`` beside the agents' proposals: approving it runs ``set_club_stage``, the
same step an agent would have proposed. Rejecting it leaves the club where it was, unlisted.
"""

from __future__ import annotations

from typing import Any

from auctions import club_import
from auctions.palette_actions import _flag, _str, untrusted_short

#: Clubs one person may have waiting for approval at once.
MAX_WAITING = 3

#: What the proposals page says proposed a member's club.
SURFACE = "Add your club page"


def web_address(value: str) -> str:
    """A typed web address with its scheme, or ``""``. "example.org" is what people paste."""
    value = value.strip()
    if value and not value.startswith(("http://", "https://")):
        value = f"https://{value}"
    return value[:255]


def club_fields(params: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """``add_club``'s arguments as ``Club`` fields, or ``({}, why not)``.

    Run twice: when the proposal is made, so the agent hears about a duplicate while the owner is
    still in the conversation, and again on approval, since another club may have been added since.
    """
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    from auctions.models import Club

    name = _str(params, "name")[:255]
    if not name:
        return {}, "Which club? add_club needs its name."
    homepage = web_address(_str(params, "homepage"))
    facebook_page = web_address(_str(params, "facebook_page"))
    if homepage and not club_import.is_a_club_host(homepage):
        return {}, (
            f"{homepage} isn't the club's own website. A Facebook page goes in facebook_page; "
            "leave homepage blank if it has no site of its own."
        )
    contact_email = _str(params, "contact_email")[:255]
    if contact_email:
        try:
            validate_email(contact_email)
        except ValidationError:
            return {}, f"{contact_email} isn't an email address."
    methods = {value for value, _label in Club.CONTACT_METHOD_CHOICES}
    contact_method = _str(params, "contact_method").lower()
    if contact_method not in methods:
        return {}, "contact_method is one of: " + ", ".join(sorted(methods - {""})) + "."
    if not contact_method:
        contact_method = Club.EMAIL if contact_email else Club.FACEBOOK if facebook_page and not homepage else ""
    found = club_import.ImportedClub(name=name, homepage=homepage)
    existing = club_import.find_existing(found, Club.objects.all())
    if existing is not None:
        return {}, f"That looks like {existing.name} (club {existing.pk}), already on the site."
    listed = _flag(params, "listed")
    return {
        "name": name,
        "abbreviation": _str(params, "abbreviation")[:255] or None,
        "homepage": homepage or None,
        "facebook_page": facebook_page or None,
        "location": _str(params, "location")[:500] or None,
        "contact_email": contact_email or None,
        "contact_method": contact_method,
        "outreach_stage": Club.PROSPECT if listed is False else Club.LISTED,
        "notes": _str(params, "notes")[:300] or None,
    }, ""


def waiting_for(user):
    """The approvals of clubs ``user`` added that nobody has decided yet."""
    from auctions.models import AgentProposal

    return AgentProposal.objects.filter(proposed_by=user, surface=SURFACE, status=AgentProposal.STATUS_PENDING)


def add_my_club(user, params: dict[str, Any]):
    """Create a club a member says they run, off the map, and ask the site's admins to list it.

    Returns ``(club, "")``, or ``(None, why not)`` with nothing written. The duplicate check names a
    club only when it is already public: an unlisted one is the outreach queue's business.
    """
    from django.db import transaction

    from auctions import geocoding, services
    from auctions.models import AgentProposal, Club

    if waiting_for(user).count() >= MAX_WAITING:
        return None, "Your other clubs are still waiting for approval."
    homepage = web_address(_str(params, "homepage"))
    existing = club_import.find_existing(
        club_import.ImportedClub(name=_str(params, "name"), homepage=homepage), Club.objects.all()
    )
    if existing is not None:
        if existing.is_listed:
            return None, f"{existing.name} is already on the site."
        return None, "That club is already waiting for approval."
    if homepage and not club_import.is_a_club_host(homepage):
        return None, "That isn't the club's own website; a Facebook page goes under Facebook."
    fields, problem = club_fields({**params, "listed": False, "contact_method": "", "notes": ""})
    if problem:
        return None, problem
    fields["notes"] = f"Added by account {user.pk} on the add your club page."
    found = geocoding.geocode(fields["location"] or "")
    if found:
        fields["location_coordinates"] = found["coordinates"]
    with transaction.atomic():
        club = Club.objects.create(**fields)
        services.ensure_club_admin(club, user, note="who added the club", actor=user)
        place = found["address"] if found else fields["location"] or "nowhere given"
        # Fenced: a stranger typed all of it, and agents on /mcp/admin/ read this page.
        AgentProposal.objects.create(
            summary=f"List {untrusted_short(club.name)} on the map"[:200],
            reason=(
                "A member added this club and is now its admin; rejecting leaves it off the map. "
                f"Website: {untrusted_short(club.homepage) or 'none'}. "
                f"Facebook: {untrusted_short(club.facebook_page) or 'none'}. Meets: {untrusted_short(place)}."
            ),
            steps=[{"tool": "set_club_stage", "arguments": {"club": club.pk, "stage": Club.LISTED}}],
            proposed_by=user,
            surface=SURFACE,
        )
    return club, ""
