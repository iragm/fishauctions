"""Two rules that keep an admin change page from querying once per row it shows.

**A foreign key is a lookup box, not a dropdown** (:func:`use_lookup_widgets`). A plain
`ModelChoiceField` renders an `<option>` per row of the target table and calls `str()` on each, so
`contact_person` on one pickup location listed every `AuctionTOS` on the site, three queries apiece.
Forty-seven fields point at `auth.User` alone and `Species` is 36,000 rows. `autocomplete_fields`
fixes it, and `raw_id_fields` covers targets with no `search_fields`.

Applied to the registry rather than declared on fifty `ModelAdmin` classes, because a rule spelled
out fifty times has fifty places to forget it. `auctions/test_admin_performance.py` asserts no
dropdown over an unbounded table survives.

**An inline says what its rows cost** (:class:`FlatInline`). `list_select_related` is a changelist
setting and does nothing to an inline, so an inline row whose `__str__` names two other objects
costs two queries a row with no obvious cause.
"""

from typing import ClassVar

from django.db.models import ForeignKey, ManyToManyField

#: Tables small enough to enumerate in a `<select>`: a fixed vocabulary somebody maintains, not rows
#: the site accumulates. Add only for a table that cannot grow with traffic.
BOUNDED_TABLES = frozenset(
    {
        # `Category` is the lot category list -- 32 rows on the live site.
        "auctions.Category",
        "auctions.Location",
        "auctions.GeneralInterest",
        "auctions.SpeakerTopic",
        "sites.Site",
        "auth.Group",
        "auth.Permission",
    }
)


class FlatInline:
    """Mixin for an inline whose cost doesn't grow with the rows it shows.

    - ``inline_select_related``: the foreign keys a row renders, including ones reached only through a
      `__str__` (`Bid.__str__` names the bidder and the lot, and `Lot.__str__` names its auction).
    - ``inline_shared_choices``: a dropdown small enough to keep. Every form in a formset copies every
      field, and a copied `ModelChoiceField` re-reads its table, so even a three-row lookup costs one
      query per row. Freezing `choices` before the copy is the only place to stop it.

    A count a row prints is the third way and has no declaration: annotate it in the inline's own
    ``get_queryset``, as ``AdCampaignInline`` does.
    """

    inline_select_related: ClassVar[tuple[str, ...]] = ()
    inline_shared_choices: ClassVar[tuple[str, ...]] = ()

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if self.inline_select_related:
            # Guarded: `select_related()` with no arguments follows every non-null FK, which for
            # `Lot` is fourteen joins.
            queryset = queryset.select_related(*self.inline_select_related)
        return queryset

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        formfield = super().formfield_for_foreignkey(db_field, request, **kwargs)
        if formfield is not None and db_field.name in self.inline_shared_choices:
            formfield.choices = list(formfield.choices)
        return formfield


def _is_ours(model_admin):
    """Only our own admin classes. A third-party admin is that package's business."""
    return type(model_admin).__module__.split(".")[0] == "auctions"


def _already_placed(model_admin):
    return (
        set(model_admin.raw_id_fields)
        | set(model_admin.autocomplete_fields)
        | set(model_admin.filter_horizontal)
        | set(model_admin.filter_vertical)
    )


def _named_on_the_page(model_admin):
    """The field names this admin puts on its form, or None if it takes the model's defaults.

    A proxy model's admin and an inline rendering only `__str__` would otherwise collect a widget for
    every relation on the model.
    """
    declared = list(model_admin.fields or ())
    for _title, options in model_admin.fieldsets or ():
        declared.extend(options.get("fields", ()))
    names = set()
    for entry in declared:
        # A row of a fieldset can be a tuple of fields sharing one line.
        names.update([entry] if isinstance(entry, str) else entry)
    return names or None


def _unbounded_relations(model_admin):
    """The editable relations on this admin's model that point at a table with no ceiling."""
    placed = _already_placed(model_admin)
    on_the_page = _named_on_the_page(model_admin)
    for field in model_admin.model._meta.get_fields():
        if not isinstance(field, ForeignKey | ManyToManyField) or field.auto_created:
            continue
        if not field.editable or field.name in placed:
            continue
        if on_the_page is not None and field.name not in on_the_page:
            continue
        if field.related_model._meta.label in BOUNDED_TABLES:
            continue
        yield field


def _searchable(site, model):
    """Can `model` back an autocomplete? It needs its own admin page and something to search.

    Exactly the conditions Django's `admin.E039`/`E040` checks demand.
    """
    target_admin = site._registry.get(model)
    return target_admin is not None and bool(target_admin.get_search_fields(request=None))


def every_admin(site):
    """Every admin page and inline, with the object to set the widget on.

    An inline is instantiated per request, so the widget goes on the class; a `ModelAdmin` is the
    long-lived registry instance, which keeps one admin's widgets out of a subclass registered for a
    different model.
    """
    for model, model_admin in site._registry.items():
        yield type(model_admin).__name__, model_admin, model_admin
        for inline_class in model_admin.inlines:
            yield inline_class.__name__, inline_class(model, site), inline_class


def use_lookup_widgets(site):
    """Give every unbounded foreign key a search box instead of a dropdown.

    Returns what it changed as ``{admin name: {field: widget}}``. Idempotent: a field that already has a
    widget is left alone.
    """
    changed = {}
    for name, model_admin, target in list(every_admin(site)):
        if not _is_ours(model_admin):
            continue
        autocomplete, raw_id = [], []
        for field in _unbounded_relations(model_admin):
            (autocomplete if _searchable(site, field.related_model) else raw_id).append(field.name)
        if not autocomplete and not raw_id:
            continue
        if autocomplete:
            target.autocomplete_fields = tuple(target.autocomplete_fields) + tuple(autocomplete)
        if raw_id:
            target.raw_id_fields = tuple(target.raw_id_fields) + tuple(raw_id)
        changed[name] = dict.fromkeys(autocomplete, "autocomplete") | dict.fromkeys(raw_id, "raw_id")
    return changed
