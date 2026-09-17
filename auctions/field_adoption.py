"""Which settings has anybody ever changed, reconstructed from the rows rather than a changelog.

``AuctionEditForm`` puts 43 fields on one page and ``Auction`` carries 105, and the argument for
hiding one behind *Advanced* -- or deleting it -- rests on how many organizers have moved it off its
default. ``AuctionHistory.changed_fields`` (:mod:`auctions.history`) answers that from the day it
shipped; this is the retroactive half.

A stored value is compared against the model's declared default over every auction. It cannot tell
"chose the default" from "never looked", but the interesting answer is the zero, and a zero here is
real. A field changed and changed back reads as untouched, so this under-counts rather than over.

``off_default`` covers the whole history and is blind to undone changes; ``edits`` is exact, counts
undone changes, and knows nothing from before the field shipped. Both zero is a deletion candidate;
a high ``off_default`` is load-bearing; ``edits`` much higher than ``off_default`` is a field people
struggle with, which belongs in the friction report rather than behind *Advanced*.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from dataclasses import field as dataclass_field

from django.core.cache import cache
from django.db.models import Count, Q

logger = logging.getLogger(__name__)

CACHE_KEY = "auction_field_adoption_v1"
CACHE_SECONDS = 60 * 60
# A field on this many auctions or fewer, with no edits behind it, is unused. Not zero: one auction
# off-default is as likely a test row or an import as a decision.
UNUSED_THRESHOLD = 1
# Under this share of auctions a field is "rare": a candidate for Advanced rather than deletion.
RARE_FRACTION = 0.02


@dataclass
class FieldAdoption:
    """One field's answer to "has anybody ever changed this"."""

    name: str
    label: str
    off_default: int
    edits: int
    total: int
    default: object = None
    default_known: bool = True
    sections: list[str] = dataclass_field(default_factory=list)

    @property
    def fraction(self) -> float:
        return (self.off_default / self.total) if self.total else 0.0

    @property
    def percent(self) -> float:
        return round(self.fraction * 100, 1)

    @property
    def verdict(self) -> str:
        """``unused`` | ``rare`` | ``used``, or ``unmeasured`` for a field with no default.

        A field with no declared default -- the dates, mostly -- has nothing to compare against, and saying
        "100% of auctions changed it" would be worse than saying nothing.
        """
        if not self.default_known:
            return "unmeasured"
        if self.off_default <= UNUSED_THRESHOLD and self.edits == 0:
            return "unused"
        if self.fraction < RARE_FRACTION and self.edits <= UNUSED_THRESHOLD:
            return "rare"
        return "used"


def model_field_default(model, name):
    """``(default, known)`` for a model field, or ``(None, False)`` when it declares none.

    ``NOT_PROVIDED`` and a callable default mean the same thing: no single value every untouched row
    shares.
    """
    try:
        field = model._meta.get_field(name)
    except Exception:
        return None, False
    if not field.has_default():
        # A nullable field with no default still has one: rows land as NULL.
        if getattr(field, "null", False):
            return None, True
        return None, False
    default = field.get_default()
    if callable(default):
        return None, False
    return default, True


def _at_default_filter(name, default):
    """The ``Q`` matching rows still sitting on the default."""
    if default is None:
        return Q(**{f"{name}__isnull": True})
    if default == "":
        # A blank CharField is stored as '' by the form and as NULL by several of the migrations
        # that added one. Both mean untouched.
        return Q(**{name: ""}) | Q(**{f"{name}__isnull": True})
    return Q(**{name: default})


def form_field_names(form_class):
    """The model-backed field names a form edits, in layout order.

    Declared fields are dropped: ``user_cut`` and ``club_member_cut`` are two views onto
    ``winning_bid_percent_to_club``, and counting both would double-count the split.
    """
    meta = getattr(form_class, "_meta", None) or getattr(form_class, "Meta", None)
    names = list(getattr(meta, "fields", None) or [])
    return names


def history_edit_counts(history_model, owner_field):
    """``{field_name: number of distinct owners with an edit naming it}``.

    One pass over the changelog rather than 40 full scans. Counting owners rather than rows is what
    makes this comparable with ``off_default``, which is per-auction.
    """
    owners = defaultdict(set)
    rows = history_model.objects.exclude(changed_fields__isnull=True).values_list(owner_field, "changed_fields")
    for owner_id, changed in rows.iterator(chunk_size=2000):
        if not changed or not isinstance(changed, dict):
            continue
        for name in changed:
            owners[name].add(owner_id)
    return {name: len(ids) for name, ids in owners.items()}


def field_adoption(model, form_class, history_model, owner_field, queryset=None, sections=None):
    """The adoption table for one form: a :class:`FieldAdoption` per model-backed field.

    One aggregate query for every field -- 40 conditional counts in a single scan -- plus one pass over
    the changelog.
    """
    queryset = model.objects.all() if queryset is None else queryset
    names = [name for name in form_field_names(form_class) if model_field_default(model, name)[1] is not None]
    defaults = {name: model_field_default(model, name) for name in names}
    measurable = [name for name in names if defaults[name][1]]
    total = queryset.count()
    at_default = {}
    if measurable and total:
        try:
            at_default = queryset.aggregate(
                **{
                    f"f_{index}": Count("pk", filter=_at_default_filter(name, defaults[name][0]))
                    for index, name in enumerate(measurable)
                }
            )
            at_default = {name: at_default[f"f_{index}"] for index, name in enumerate(measurable)}
        except Exception:
            # A default that can't be compared in SQL would take the whole table down with it.
            logger.exception("field_adoption aggregate failed for %s", model.__name__)
            at_default = {}
    edits = history_edit_counts(history_model, owner_field)
    sections = sections or {}
    results = []
    for name in names:
        default, known = defaults[name]
        known = known and name in at_default
        results.append(
            FieldAdoption(
                name=name,
                label=_verbose_name(model, name),
                off_default=(total - at_default[name]) if known else 0,
                edits=edits.get(name, 0),
                total=total,
                default=default,
                default_known=known,
                sections=[section for section, members in sections.items() if name in members],
            )
        )
    return results


def _verbose_name(model, name):
    try:
        return str(model._meta.get_field(name).verbose_name)
    except Exception:
        return name.replace("_", " ").capitalize()


def auction_field_adoption(use_cache=True):
    """:func:`field_adoption` for ``AuctionEditForm``, cached for an hour: it is two full scans of the two
    biggest tables, and the answer moves on the scale of weeks.
    """
    from auctions.forms import AuctionEditForm
    from auctions.models import Auction, AuctionHistory

    if use_cache:
        cached = cache.get(CACHE_KEY)
        if cached is not None:
            return cached
    results = field_adoption(
        Auction,
        AuctionEditForm,
        AuctionHistory,
        "auction_id",
        queryset=Auction.objects.filter(is_deleted=False),
    )
    if use_cache:
        cache.set(CACHE_KEY, results, CACHE_SECONDS)
    return results
