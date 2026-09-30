"""What clubs type into invoice adjustment notes and custom lot fields, grouped by common terms.

Temporary: it feeds the help. :func:`group_by_term` puts each text in the group of its most widely used
word, counted by auctions rather than rows so one busy club can't make a term look common.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from django.db.models import Count

from .models import Auction, AuctionDropdown, AuctionRandomOption, InvoiceAdjustment, Lot
from .species_matching import singularize

#: A term: three or more characters, a letter first.
_WORD = re.compile(r"[a-z][a-z0-9]{2,}")
_STOPWORDS = frozenset(
    "the and for with from per this that its your our their not yes all any each other are was were has have "
    "had will can use used get got off one two you who what when where which into out".split()
)
#: Wording shown per group.
PHRASES_PER_GROUP = 8


@dataclass
class Row:
    text: str
    auction_id: int | None
    weight: int = 1
    extra: dict = field(default_factory=dict)


@dataclass
class Group:
    term: str
    rows: list[Row]
    columns: dict = field(default_factory=dict)

    @property
    def uses(self):
        return sum(row.weight for row in self.rows)

    @property
    def auctions(self):
        return len({row.auction_id for row in self.rows})

    @property
    def phrases(self):
        counts = Counter()
        for row in self.rows:
            counts[phrase(row.text) or "(blank)"] += row.weight
        return counts.most_common(PHRASES_PER_GROUP)


def phrase(text):
    """*text* lowercased with digits and punctuation dropped, so "Table fee $5" and "table fee" match."""
    return " ".join(re.sub(r"[^a-z]+", " ", (text or "").lower()).split())


def terms(text):
    """``{term: word}``: each word in *text* worth grouping on, keyed by its singular."""
    return {singularize(word): word for word in _WORD.findall((text or "").lower()) if word not in _STOPWORDS}


def _index(rows):
    """Each row's terms, the auctions using each term, and each term's commonest spelling."""
    row_terms = [terms(row.text) for row in rows]
    auctions_by_term = defaultdict(set)
    spellings = defaultdict(Counter)
    for row, found in zip(rows, row_terms):
        for term, word in found.items():
            auctions_by_term[term].add(row.auction_id)
            spellings[term][word] += row.weight
    labels = {term: counts.most_common(1)[0][0] for term, counts in spellings.items()}
    return row_terms, auctions_by_term, labels


def group_by_term(rows):
    """``(groups, singles)``: groups whose term two or more auctions use, biggest first, and the rest."""
    row_terms, auctions_by_term, labels = _index(rows)
    by_term = defaultdict(list)
    for row, found in zip(rows, row_terms):
        term = max(found, key=lambda t: (len(auctions_by_term[t]), t)) if found else ""
        by_term[term].append(row)
    groups = sorted(
        (Group(labels.get(term, ""), grouped) for term, grouped in by_term.items()),
        key=lambda g: (-g.auctions, -g.uses, g.term),
    )
    return [g for g in groups if g.auctions > 1], [g for g in groups if g.auctions <= 1]


def top_terms(rows, limit=40):
    """The most widely used terms overall, by auctions, overlapping unlike :func:`group_by_term`."""
    _, auctions_by_term, labels = _index(rows)
    ranked = sorted(auctions_by_term.items(), key=lambda item: (-len(item[1]), item[0]))
    return [(labels[term], len(auctions)) for term, auctions in ranked[:limit]]


def section(rows, describe=None):
    """A grouped section for the page. *describe* adds columns to each group from its rows."""
    groups, singles = group_by_term(rows)
    for group in groups + singles:
        group.columns = describe(group.rows) if describe else {}
    return {
        "groups": groups,
        "singles": singles,
        "terms": top_terms(rows),
        "rows": sum(row.weight for row in rows),
        "auctions": len({row.auction_id for row in rows}),
    }


def adjustments():
    rows = [
        Row(notes, auction_id, extra={"type": kind, "amount": amount})
        for notes, auction_id, kind, amount in InvoiceAdjustment.objects.values_list(
            "notes", "invoice__auction_id", "adjustment_type", "amount"
        )
    ]

    def describe(grouped):
        amounts = [row.extra["amount"] for row in grouped]
        return {
            "charges": sum(row.extra["type"] == "ADD" for row in grouped),
            "discounts": sum(row.extra["type"] == "DISCOUNT" for row in grouped),
            "median": statistics.median(amounts) if amounts else None,
        }

    return section(rows, describe)


def _lots_by_auction(**filters):
    lots = Lot.objects.filter(is_deleted=False, **filters)
    return dict(lots.values_list("auction_id").annotate(n=Count("pk")).order_by())


def _options_by_auction(model, auction_ids):
    options = defaultdict(list)
    for auction_id, value in model.objects.filter(auction_id__in=auction_ids).values_list("auction_id", "value"):
        options[auction_id].append(value)
    return options


def _field_section(auctions, name_field, lots, options=None):
    """One row per auction using the field, its name as the text."""
    auctions = list(auctions.values_list("pk", name_field))
    options = options or {}

    def describe(grouped):
        common = Counter(phrase(value) for row in grouped for value in {*options.get(row.auction_id, [])})
        return {
            "lots": sum(lots.get(row.auction_id, 0) for row in grouped),
            "options": common.most_common(PHRASES_PER_GROUP),
        }

    return section([Row(name, pk) for pk, name in auctions], describe)


def custom_fields():
    """Every custom lot field clubs switched on, by what they named it, plus what sellers typed in the text one."""
    auctions = Auction.objects.filter(is_deleted=False)
    text = auctions.exclude(custom_field_1="disable")
    checkbox = auctions.filter(use_custom_checkbox_field=True)
    dropdown = auctions.exclude(use_custom_dropdown_field="disable")
    random = auctions.filter(use_custom_random_field=True)
    typed = [
        Row(value, auction_id, weight=n)
        for auction_id, value, n in Lot.objects.filter(is_deleted=False, auction__in=text)
        .exclude(custom_field_1="")
        .values_list("auction_id", "custom_field_1")
        .annotate(n=Count("pk"))
        .order_by()
    ]
    return [
        {
            "title": "Custom text field",
            **_field_section(text, "custom_field_1_name", _lots_by_auction(auction__in=text, custom_field_1__gt="")),
        },
        {
            "title": "What sellers typed in the custom text field",
            "typed": True,
            **section(typed),
        },
        {
            "title": "Custom checkbox",
            **_field_section(
                checkbox, "custom_checkbox_name", _lots_by_auction(auction__in=checkbox, custom_checkbox=True)
            ),
        },
        {
            "title": "Custom dropdown",
            **_field_section(
                dropdown,
                "custom_dropdown_name",
                _lots_by_auction(auction__in=dropdown, custom_dropdown__gt=""),
                _options_by_auction(AuctionDropdown, dropdown.values("pk")),
            ),
        },
        {
            "title": "Custom random field",
            **_field_section(
                random,
                "custom_random_name",
                _lots_by_auction(auction__in=random, custom_random__gt=""),
                _options_by_auction(AuctionRandomOption, random.values("pk")),
            ),
        },
    ]
