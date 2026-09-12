"""Two rules that keep an admin change page from querying once per row it shows.

Both exist because the Django admin's defaults are written for small tables, and almost every
table here grows with the site.

**A foreign key is a lookup box, not a dropdown** (:func:`use_lookup_widgets`). A plain
`ModelChoiceField` renders one `<option>` per row of the *target* table and calls `str()` on each
one, so the `contact_person` field on one pickup location was a list of every `AuctionTOS` on the
site, three queries deep apiece (`display_name` reads the auction, the user and the user's
`UserData`). Forty-seven fields across this admin point at `auth.User` alone, and `Species` is
already 36,000 rows. `autocomplete_fields` fixes it properly -- a search box that fetches matches
over AJAX and renders only what is selected -- and `raw_id_fields` covers the targets that have no
`search_fields` to search on. Making the field read-only would also fix it, but it takes the field
away; these keep it editable.

Applied to the registry rather than declared on each of the fifty-odd `ModelAdmin` classes, because
the interesting part is the *rule*, and a rule spelled out fifty times is a rule with fifty places
to forget it. `auctions/test_admin_performance.py` asserts no dropdown over an unbounded table
survives anywhere in the admin.

**An inline says what its rows cost** (:class:`FlatInline`). `list_select_related`
is a changelist setting and does nothing to an inline, so an inline row that renders a `__str__`
naming two other objects costs two queries a row with no obvious cause.
"""

from typing import ClassVar

from django.db.models import ForeignKey, ManyToManyField

#: Tables small enough to enumerate in a `<select>`: a fixed vocabulary somebody maintains, not
#: rows the site accumulates. Everything else gets a search box. Add to this only for a table that
#: cannot grow with traffic -- and if it can, the widget below is the answer, not an exception.
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
    """Mixin for an inline whose cost does not grow with the number of rows it shows.

    Two declarations, for two of the three ways an inline row pays for itself:

    - ``inline_select_related`` -- the foreign keys a row renders, including the ones it reaches
      only through a `__str__` (`Bid.__str__` names the bidder *and* the lot, and `Lot.__str__`
      names its auction, so a bid row three levels deep costs three queries without this).
    - ``inline_shared_choices`` -- a dropdown small enough to keep as a dropdown. Every form in a
      formset gets its own copy of every field, and a copy of a `ModelChoiceField` re-reads its
      table when it renders, so even a three-row lookup table costs one query per row on the page.
      Freezing `choices` on the field before it is copied is the only place that can be stopped.

    A count a row prints is the third way, and it has no declaration here: annotate it in a
    ``get_queryset`` of the inline's own, over ``super()``, so the annotation can be a named method
    on the model beside the property that reads it. ``AdCampaignInline`` is the example.
    """

    inline_select_related: ClassVar[tuple[str, ...]] = ()
    inline_shared_choices: ClassVar[tuple[str, ...]] = ()

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if self.inline_select_related:
            # Guarded, because `select_related()` with no arguments does not mean "nothing" -- it
            # means *follow every non-null FK on the model*, which for `Lot` is fourteen joins.
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
    """The field names this admin puts on its form, or None if it takes the model's default set.

    A proxy model's admin (`LotAutoCategoryAdmin` names three of `Lot`'s fields) and an inline that
    renders only `__str__` would otherwise collect a widget for every relation on the model,
    including the dozen the page does not show.
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
    """The editable relations on this admin's model that point at a table without a ceiling."""
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

    Exactly the two conditions Django's own `admin.E039`/`admin.E040` checks demand, so anything
    this says no to would have failed the system checks had we asked for an autocomplete anyway.
    """
    target_admin = site._registry.get(model)
    return target_admin is not None and bool(target_admin.get_search_fields(request=None))


def every_admin(site):
    """Every admin page and every inline on one, each with the object to set the widget on.

    An inline is instantiated per request, so the widget has to go on the class; a `ModelAdmin` is
    the long-lived instance in the registry, and setting it there keeps one admin's widgets from
    leaking into a subclass registered for a different model.
    """
    for model, model_admin in site._registry.items():
        yield type(model_admin).__name__, model_admin, model_admin
        for inline_class in model_admin.inlines:
            yield inline_class.__name__, inline_class(model, site), inline_class


def use_lookup_widgets(site):
    """Give every unbounded foreign key in our admin a search box instead of a dropdown.

    Returns what it changed, as ``{admin name: {field: widget}}`` -- which is what the test reads.
    Idempotent: a field that already has a widget is not touched, so calling this twice is a no-op.
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
