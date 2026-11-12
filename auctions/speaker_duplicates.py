"""Duplicate speakers in the directory: finding them, and folding one into another.

The NEC import stores "Last, First", people adding themselves type "First Last", and two clubs can add
the same person, so one speaker can end up as several rows. ``possible_matches`` warns somebody adding
themselves; ``duplicate_groups`` feeds the admin connector's ``find_duplicate_speakers``; and
``merge_speakers`` is what an approved ``merge_speakers`` proposal runs. A "This is me" claim
(``SpeakerClaim``) is settled by ``link_speaker_to_account``, which merges the claimant's own profile in
first when they already have one.
"""

import difflib
import re
import unicodedata

from django.db import transaction

#: Fields the kept record takes from the other one when its own is blank.
TEXT_FIELDS = ("bio", "programs", "email", "phone", "website", "facebook_page")

#: How alike two names must be (difflib ratio over the name keys) to be listed as a likely pair.
FUZZY_NAME_RATIO = 0.88

_HONORIFICS = {"dr", "mr", "mrs", "ms", "jr", "sr", "ii", "iii", "prof"}


def name_key(name):
    """A name reduced to what two spellings of one person share: "First Last", lower case, no accents,
    punctuation, honorifics or initials.
    """
    name = (name or "").strip()
    if name.count(",") == 1:
        last, first = (part.strip() for part in name.split(","))
        if last and first:
            name = f"{first} {last}"
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-z\s]", " ", name.casefold()).split()
    return " ".join(word for word in words if len(word) > 1 and word not in _HONORIFICS)


def _first_and_last(key):
    words = key.split()
    return (words[0], words[-1]) if len(words) > 1 else (key, key)


def possible_matches(name, exclude_pk=None):
    """Live speakers whose name could be this one: the same key, or the same first and last word."""
    from auctions.models import Speaker

    key = name_key(name)
    if not key:
        return []
    first_last = _first_and_last(key)
    # The last word narrows it in the database; the key decides.
    last_word = key.split()[-1]
    candidates = Speaker.objects.filter(is_deleted=False, name__icontains=last_word)
    if exclude_pk:
        candidates = candidates.exclude(pk=exclude_pk)
    return [
        speaker
        for speaker in candidates.order_by("pk")
        if name_key(speaker.name) == key or _first_and_last(name_key(speaker.name)) == first_last
    ]


def _normalized_url(url):
    url = (url or "").strip().lower()
    url = re.sub(r"^https?://(www\.)?", "", url)
    return url.rstrip("/")


def duplicate_groups():
    """Pairs of live speakers that are probably one person, each with why: ``[(a, b, [reasons])]``.

    A pair rather than a group because each merge is one step a person approves. Matched on the name key,
    the first and last name, a fuzzy name, and a shared email address or website.
    """
    from auctions.models import Speaker

    speakers = list(Speaker.objects.filter(is_deleted=False).order_by("pk"))
    keys = {speaker.pk: name_key(speaker.name) for speaker in speakers}
    pairs = {}

    def note(a, b, reason):
        pair = (a, b) if a.pk < b.pk else (b, a)
        reasons = pairs.setdefault((pair[0].pk, pair[1].pk), (pair[0], pair[1], []))[2]
        if reason not in reasons:
            reasons.append(reason)

    def by(value_of, reason):
        seen = {}
        for speaker in speakers:
            value = value_of(speaker)
            if not value:
                continue
            for other in seen.get(value, []):
                note(other, speaker, reason)
            seen.setdefault(value, []).append(speaker)

    by(lambda speaker: keys[speaker.pk], "same name")
    by(
        lambda speaker: " ".join(_first_and_last(keys[speaker.pk])) if keys[speaker.pk] else "",
        "same first and last name",
    )
    by(lambda speaker: (speaker.email or "").strip().casefold(), "same email address")
    by(lambda speaker: _normalized_url(speaker.website), "same website")
    # Fuzzy names, within one first letter of the last name so it isn't n squared over the whole list.
    by_initial = {}
    for speaker in speakers:
        key = keys[speaker.pk]
        if key:
            by_initial.setdefault(key.split()[-1][0], []).append(speaker)
    for group in by_initial.values():
        for index, speaker in enumerate(group):
            for other in group[index + 1 :]:
                matcher = difflib.SequenceMatcher(None, keys[speaker.pk], keys[other.pk])
                if matcher.quick_ratio() >= FUZZY_NAME_RATIO and matcher.ratio() >= FUZZY_NAME_RATIO:
                    note(speaker, other, "similar name")
    return sorted(pairs.values(), key=lambda row: (-len(row[2]), row[0].pk, row[1].pk))


def suggested_keep(a, b):
    """Which of two duplicates to keep: the older record, so its address (and any search ranking) stays.

    The person's own profile still wins on content: ``merge_speakers`` takes their text over the other's.
    """
    return (a, b) if a.pk < b.pk else (b, a)


def merge_problem(closed, kept):
    """Why two records can't be merged, or ``""``."""
    if closed is None or kept is None:
        return "There is no live speaker by that slug."
    if closed.pk == kept.pk:
        return "Those are the same speaker."
    if closed.user_id and kept.user_id and closed.user_id != kept.user_id:
        return "Both speakers belong to different site accounts."
    return ""


def merge_speakers(closed, kept):
    """Fold ``closed`` into ``kept`` and retire ``closed``, whose address then redirects to ``kept``.

    The person's own profile (the one linked to their account) wins wherever both have something; the
    other fills its blanks. Topics are unioned; tags, comments and claims move across.
    """
    from auctions.models import Speaker, SpeakerClaim, SpeakerTag

    with transaction.atomic():
        closed = Speaker.objects.select_for_update().get(pk=closed.pk)
        kept = Speaker.objects.select_for_update().get(pk=kept.pk)
        theirs_wins = bool(closed.user_id and not kept.user_id)
        for field in TEXT_FIELDS:
            mine, other = getattr(kept, field), getattr(closed, field)
            if other and (theirs_wins or not mine):
                setattr(kept, field, other)
        if closed.location and (theirs_wins or not kept.location):
            kept.location = closed.location
            kept.location_coordinates = closed.location_coordinates
            kept.latitude, kept.longitude = closed.latitude, closed.longitude
        # The photo moves as one: an upload and a pasted URL are two ways of giving the same thing.
        closed_has_photo = bool(closed.image or closed.url)
        if closed_has_photo and (theirs_wins or not (kept.image or kept.url)):
            kept.image = closed.image
            kept.cloudflare_image_id = closed.cloudflare_image_id
            kept.url = closed.url
        if theirs_wins:
            kept.user_id = closed.user_id
            kept.visibility = closed.visibility
            if closed.name:
                kept.name = closed.name
        # Not created_by: that is who may edit a page nobody's account owns, and an imported page
        # belongs to nobody but a superuser until the person themselves is linked.
        if not kept.club_id:
            kept.club_id = closed.club_id
        if closed.imported_from_nec and not kept.imported_from_nec:
            # The kept page carries the NEC credit, and a re-import finds it by the post id.
            post_id = closed.wordpress_post_id
            Speaker.objects.filter(pk=closed.pk).update(wordpress_post_id=None)
            kept.imported_from_nec = True
            kept.source_url = closed.source_url
            if kept.wordpress_post_id is None:
                kept.wordpress_post_id = post_id
        kept.save()
        kept.topics.add(*closed.topics.all())
        for tag in SpeakerTag.objects.filter(speaker=closed):
            if SpeakerTag.objects.filter(speaker=kept, user_id=tag.user_id, tag=tag.tag).exists():
                tag.delete()
            else:
                SpeakerTag.objects.filter(pk=tag.pk).update(speaker=kept)
        closed.comments.update(speaker=kept)
        for claim in SpeakerClaim.objects.filter(speaker=closed):
            if SpeakerClaim.objects.filter(speaker=kept, user_id=claim.user_id).exists():
                claim.delete()
            else:
                SpeakerClaim.objects.filter(pk=claim.pk).update(speaker=kept)
        # Anything already merged into the closed record now points straight at the kept one.
        Speaker.objects.filter(merged_into=closed).update(merged_into=kept)
        Speaker.objects.filter(pk=closed.pk).update(is_deleted=True, merged_into=kept, user=None)
    return kept


def link_speaker_to_account(speaker, user):
    """Settle a "This is me" claim: the speaker becomes this account's profile.

    A claimant who already has a profile of their own has it merged into the claimed one, so they end
    up with one page. Open claims on the speaker are closed either way.
    """
    from auctions.models import Speaker, SpeakerClaim

    with transaction.atomic():
        own = Speaker.objects.filter(user=user, is_deleted=False).exclude(pk=speaker.pk).order_by("pk")
        for profile in own:
            merge_speakers(profile, speaker)
        Speaker.objects.filter(pk=speaker.pk).update(user=user)
        SpeakerClaim.objects.filter(speaker=speaker, status=SpeakerClaim.STATUS_OPEN).update(
            status=SpeakerClaim.STATUS_DECLINED
        )
        SpeakerClaim.objects.filter(speaker=speaker, user=user).update(status=SpeakerClaim.STATUS_APPROVED)
    speaker.refresh_from_db()
    return speaker
