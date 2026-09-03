"""Adding species and common names, and the superuser's queue for cleaning them up.

``/species/new/`` and ``/species/name/`` are open to anyone who runs an auction, and a
non-superuser's row is created unapproved and scoped to them and their club. The gaps page is where
a superuser approves, merges or rejects them.
"""

import logging

from dal import autocomplete
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import (
    Count,
    Max,
    Q,
)
from django.db.models.base import Model as Model
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.html import format_html
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.generic import TemplateView, View
from django.views.generic.edit import (
    CreateView,
    FormView,
)

from auctions.forms import (
    SpeciesAdminForm,
    SpeciesCommonNameForm,
)
from auctions.models import (
    Lot,
    Species,
    SpeciesNameRejection,
    SpeciesSearchCache,
    normalize_species_name,
)
from auctions.species_matching import remember as remember_species
from auctions.species_matching import (
    visible_species,
)

from .base import AdminOnlyViewMixin, AuctionAdminAnywhereViewMixin

logger = logging.getLogger(__name__)


class SpeciesAutocomplete(LoginRequiredMixin, autocomplete.Select2QuerySetView):
    """Search the whole species list by hand.

    Two callers, and the difference between them is ``?varieties=1``:

    * the "strain of" field on the add-species form, which wants **nominal species only** -- a
      strain of a strain is not a thing, and offering one would build a chain nothing else in the
      codebase knows how to walk.
    * the "search all species" box on the lot forms, which wants the strains too: "Blue Dream" is
      exactly the kind of thing somebody falls back to searching for.

    That second caller is why this exists on a lot form at all.  The picker there is filled in
    only by :class:`SpeciesSuggestions`, from the lot name -- so when the matcher came back with
    nothing (FishBase files *Labidochromis caeruleus* under "Blue streak hap", so "Yellow lab"
    finds nothing without the model) or came back with the wrong five, there was no way to reach
    the right species at all, for the seller or for the auction admin editing the lot afterwards.
    """

    def get_queryset(self):
        # Same visibility rule the suggestions follow: an unapproved species is offered to the
        # person who added it and to nobody else.  Searching by hand must not be the way round it.
        queryset = visible_species(self.request.user)
        if self.request.GET.get("varieties") != "1":
            # Nominal species only.  ``is_hybrid`` as well as ``parent``: a cross is not a strain of
            # anything, so it passes ``parent__isnull=True`` while being exactly the sort of row a
            # "strain of" box must never offer -- a strain of it would inherit a genus it hasn't got.
            queryset = queryset.filter(parent__isnull=True, is_hybrid=False)
        if self.q:
            queryset = queryset.filter(
                Q(scientific_name__icontains=self.q)
                | Q(common_name__icontains=self.q)
                | Q(variety__icontains=self.q)
                # The names people actually type; the same column every other lookup matches on.
                | Q(common_names__name_normalized__icontains=normalize_species_name(self.q))
            ).distinct()
        # Same ordering as the suggestions: the fish somebody actually keeps, first, and a
        # freshwater one ahead of the reef fish that shares its name.  See species_matching._rank.
        return queryset.order_by("-freshwater", "trade_rank", "scientific_name")

    def get_result_label(self, result):
        return format_html("{}", result.label)


class SpeciesGapsView(AdminOnlyViewMixin, TemplateView):
    """The lots that should have a scientific name and don't, as a work queue.

    The sibling of the command palette's bounce list, and it exists for the same reason: the
    interesting thing about a lookup is not the ones that worked, it is the repeated ones that
    didn't.  A lot name showing up here forty times is either a species missing from the list or a
    name the matcher can't connect to one, and both are fixed by the same button.

    Grouped by lot name rather than listed per lot, because forty lots called "blue dream shrimp"
    are one problem and one decision, not forty.

    What it deliberately does **not** do is guess which of these are hardware.  There is no signal
    that separates "sponge filter" from "sponge" reliably, and a filter that hid things would hide
    the ones worth finding.  Instead every column is a piece of evidence -- how many sellers said
    they bred it, what categories the lots landed in, what the matcher decided last time -- and
    the reader does the judging.  The one exception is names made entirely of stopwords and
    numbers, which cannot name anything.
    """

    template_name = "species_gaps.html"

    #: Enough to work through in a sitting.  The tail is a long list of one-off lot names, and
    #: anything appearing once is not yet a pattern worth adding a species for.
    LIMIT = 100

    def get_context_data(self, **kwargs):
        from auctions.species_matching import base_words, normalize

        context = super().get_context_data(**kwargs)
        # Only auctions that asked: a lot in an auction with the field switched off has no species
        # because nobody was ever offered the choice, which is not a gap.
        missing = Lot.objects.filter(
            species__isnull=True, is_deleted=False, banned=False, auction__use_scientific_name=True
        )
        rows = (
            missing.exclude(lot_name="")
            .values("lot_name")
            .annotate(
                # Lot's primary key is lot_number, not id.
                lots=Count("pk"),
                bred=Count("pk", filter=Q(i_bred_this_fish=True)),
                newest=Max("date_posted"),
            )
            .order_by("-lots", "-newest")[: self.LIMIT * 3]
        )
        # Two lot names that normalise the same are the same problem.  Merged here rather than in
        # SQL because the normalisation is Python (see species_matching.normalize).
        merged = {}
        for row in rows:
            if not base_words(row["lot_name"]):
                continue
            key = normalize(row["lot_name"])
            if not key:
                continue
            entry = merged.setdefault(
                key, {"lot_name": row["lot_name"], "lots": 0, "bred": 0, "newest": row["newest"], "key": key}
            )
            entry["lots"] += row["lots"]
            entry["bred"] += row["bred"]
            entry["newest"] = max(entry["newest"], row["newest"]) if row["newest"] else entry["newest"]

        verdicts = {
            cache_row.search_text: cache_row
            for cache_row in SpeciesSearchCache.objects.filter(search_text__in=list(merged)).select_related("species")
        }
        for key, entry in merged.items():
            verdict = verdicts.get(key)
            if verdict is None:
                entry["verdict"] = "never looked up"
                entry["verdict_detail"] = ""
            elif verdict.species_id:
                # The name resolves fine; these lots predate the answer, or the seller said no.
                entry["verdict"] = "matches a species"
                entry["verdict_detail"] = verdict.species.label
            elif verdict.source == "llm":
                entry["verdict"] = "not a species"
                entry["verdict_detail"] = "decided by the language model"
            else:
                entry["verdict"] = "not a species"
                entry["verdict_detail"] = "chosen by a person"

        context["gaps"] = sorted(merged.values(), key=lambda entry: (-entry["bred"], -entry["lots"]))[: self.LIMIT]
        context["total_missing"] = missing.count()
        context["total_with_species"] = Lot.objects.filter(
            species__isnull=False, is_deleted=False, auction__use_scientific_name=True
        ).count()
        context["rejected"] = list(
            SpeciesSearchCache.objects.filter(species__isnull=True).order_by("-hits", "-createdon")[:25]
        )
        # The other half of the same table, and the half that was invisible.  A remembered *wrong*
        # species is strictly worse than a remembered "no": it outranks search_matches, it is
        # shared by every club, and it ends up on a printed label and in a breeder award.  There
        # was nowhere on the site it could be seen, let alone undone.
        context["mappings"] = list(
            SpeciesSearchCache.objects.filter(species__isnull=False)
            .select_related("species", "created_by")
            .order_by("-hits", "-createdon")[:50]
        )
        # Species an auction admin added to solve a problem in front of them.  Each one is
        # currently offered to that person and to nobody else, and approving it is the whole of
        # the admin's job here -- see Species.approved and species_matching.visible_species.
        context["pending"] = list(
            Species.objects.filter(approved=False)
            .select_related("added_by", "category", "parent", "club")
            .annotate(lots=Count("lot"))
            .order_by("-id")[:50]
        )
        # Pairings the site has retired, and the only place they can be undone.  Kept next to the
        # cache tables because they are the same subject read the other way round: what the matcher
        # has been told *not* to answer.  See species_matching.record_choice.
        context["rejections"] = list(SpeciesNameRejection.objects.select_related("species").order_by("-createdon")[:50])
        # Rows that look like another row.  Both halves of a pair carry the flag, so the listing is
        # de-duplicated here and the reader sees one line per decision rather than two.  Ordered by
        # pk so that line is stable from one page load to the next.
        flagged = list(
            Species.objects.filter(possible_duplicate__isnull=False)
            .select_related("possible_duplicate", "category", "added_by", "club")
            .annotate(lots=Count("lot"))
            .order_by("pk")[:100]
        )
        # One query for the lot counts of the other halves rather than one per row: both sides of
        # a pair carry the flag, but only the side that came back from the query above is annotated.
        other_lots = dict(
            Lot.objects.filter(species__in=[species.possible_duplicate_id for species in flagged])
            .values_list("species")
            .annotate(count=Count("pk"))
        )
        duplicates = []
        seen_pairs = set()
        for species in flagged:
            other = species.possible_duplicate
            pair = tuple(sorted((species.pk, other.pk)))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            duplicates.append(
                {
                    "species": species,
                    "other": other,
                    "other_lots": other_lots.get(other.pk, 0),
                    "same_scientific_name": bool(
                        species.scientific_name
                        and species.scientific_name.lower() == other.scientific_name.lower()
                        and species.variety.lower() == other.variety.lower()
                    ),
                }
            )
        context["duplicates"] = duplicates
        context["species_total"] = Species.objects.count()
        context["species_added_here"] = Species.objects.filter(source="admin").count()
        return context


class SpeciesSearchCacheForgetView(AdminOnlyViewMixin, View):
    """Delete one remembered answer, so the next lookup works the name out again.

    The undo for :func:`auctions.species_matching.remember`.  That cache is written by the lot
    forms on a seller's first save as well as by the language model, it is shared by every club,
    and it is consulted *before* the token search -- so one wrong row quietly outranks the species
    list itself for everybody, forever.  Until this existed the only way to remove one was the
    Django admin, and the gaps page didn't list the wrong ones at all, so nobody knew to look.

    Deleting is the whole fix: the name simply falls through to the matcher again next time.
    """

    def post(self, request, pk):
        row = get_object_or_404(SpeciesSearchCache, pk=pk)
        name = row.search_text
        row.delete()
        messages.success(request, f"Forgot the remembered answer for “{name}”.  It will be looked up again.")
        return redirect("species_gaps")


class SpeciesNameRejectionDeleteView(AdminOnlyViewMixin, View):
    """Let a name be matched to a species again after the site retired the pairing.

    The escape hatch for :func:`auctions.species_matching.record_choice`.  Enough sellers taking a
    species off the lots called something retires that pairing for good -- deliberately, because
    otherwise the language model answers the same question the same way and the answer is written
    straight back.  "For good" needs a way out, and this is it: usually because the rejections were
    really about the *lot names* ("blue dream shrimp" cleared by people selling something else) and
    the pairing was right all along.
    """

    def post(self, request, pk):
        row = get_object_or_404(SpeciesNameRejection, pk=pk)
        name, species = row.search_text, row.species
        row.delete()
        messages.success(request, f"“{name}” may be matched to {species.label} again.")
        return redirect("species_gaps")


class SpeciesDuplicateDismissView(AdminOnlyViewMixin, View):
    """ "These two are not the same species."  Clears the flag on both sides.

    Two species really can share a designated common name -- FishBase gives "Peppered cory" to two
    different *Corydoras* -- so a flag that could only be resolved by merging would force a wrong
    answer.  Cleared on both rows, so the pair does not come back on the next save of either.
    """

    def post(self, request, pk):
        species = get_object_or_404(Species, pk=pk)
        other = species.possible_duplicate
        Species.objects.filter(pk=species.pk).update(possible_duplicate=None)
        if other:
            Species.objects.filter(pk=other.pk).update(possible_duplicate=None)
        messages.success(request, f"{species.label} is not a duplicate.")
        return redirect("species_gaps")


class SpeciesMergeView(AdminOnlyViewMixin, View):
    """Fold one species row into another.  Superusers only, on purpose.

    A duplicate is almost always a club's hand-added row sitting next to one of FishBase's 36,000,
    and merging is not reversible: the lots, the strains and the hobby names move, and one of the
    two rows stops existing.  Which name the whole site keeps is a decision for whoever maintains
    the list, not for whichever admin happened to add the second row -- which is also why the
    button lives on the gaps page and not on the lot form.

    ``keep`` is the row that survives; the pk in the URL is the one being folded in.
    """

    def post(self, request, pk):
        duplicate = get_object_or_404(Species, pk=pk)
        keep = get_object_or_404(Species, pk=request.POST.get("keep") or 0)
        if keep.pk == duplicate.pk:
            messages.error(request, "A species cannot be merged into itself.")
            return redirect("species_gaps")
        # A variety and its own parent are not two copies of one species; merging them would move
        # the strain's lots onto the nominal species and lose the strain.
        if keep.parent_id == duplicate.pk or duplicate.parent_id == keep.pk:
            messages.error(request, "That is a strain and its parent species, not a duplicate.  Nothing was merged.")
            return redirect("species_gaps")
        losing_label = duplicate.label
        moved = keep.merge_duplicate(duplicate)
        messages.success(
            request,
            f"Merged {losing_label} into {keep.label}: "
            f"{moved.get('lots', 0)} lot(s), {moved.get('common_names', 0)} common name(s), "
            f"{moved.get('varieties', 0)} strain(s) and {moved.get('remembered_names', 0)} "
            "remembered name(s) moved.",
        )
        return redirect("species_gaps")


class SpeciesApproveView(AdminOnlyViewMixin, View):
    """Promote one species from "the person who added it" to "everybody".

    The entire approval workflow.  An auction admin adding a species at a check-in table gets a
    row only they can see (:attr:`Species.approved`); this is the button that makes it part of the
    shared list, and it is a superuser's call because that list is a shared asset.

    Approving is also what lets the name be *remembered*: :func:`species_matching.remember`
    refuses to write an unapproved species into a cache every club reads, so the mapping from the
    lot name is written here instead, at the moment the species becomes everyone's.
    """

    def post(self, request, pk):
        species = get_object_or_404(Species, pk=pk)
        if not species.approved:
            species.approved = True
            species.save()
            # The names arrived with it and are scoped the same way, so they become everybody's at
            # the same moment.  Without this the species would be on the shared list while the
            # words people actually type for it stayed private to one club.
            species.common_names.filter(approved=False).update(approved=True)
            # The genus tier is a statement about siblings, and this row was invisible to the last
            # pass that worked one out.
            Species.recompute_trade_ranks(genus=species.genus)
            for lot_name in (
                Lot.objects.filter(species=species)
                .exclude(lot_name="")
                .order_by()
                .values_list("lot_name", flat=True)
                .distinct()
            )[:20]:
                remember_species(lot_name, species, source="user", user=species.added_by)
        messages.success(request, f"{species.label} is now suggested for everyone.")
        return redirect("species_gaps")


def species_page_success_url(request):
    """Where to go after adding a species or naming one.  ``?next=`` wins, for everybody.

    It used to be "the gaps page if you are a superuser, ``?next=`` otherwise", and the superuser
    half was wrong in exactly the case these pages are opened from: the button is on the auction
    admin's *lot editor*, it opens in a new tab, and it carries a ``next=`` back to the lot list.
    A superuser clicking it was thrown onto the admin work queue with the lot they were fixing
    left behind in the other tab.  Whoever wrote the link knows where the person came from; the
    permissions they happen to hold do not.

    Checked against the host, because it arrives as a query parameter and a redirect is the whole
    payload.
    """
    following = request.GET.get("next") or request.POST.get("next") or ""
    if following and url_has_allowed_host_and_scheme(
        following, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return following
    if request.user.is_superuser:
        return reverse("species_gaps")
    return reverse("selling")


class LotNameSpeciesMixin:
    """The lots a ``?lot_name=`` in the URL is talking about.

    Shared by the two pages that fix a gap on the species list -- adding the species
    (:class:`SpeciesCreateView`) and naming one that is already there
    (:class:`SpeciesCommonNameCreateView`).  Both arrive from the same links carrying the same
    lot name, and both end by putting the species on every lot called that, so the awkward part
    -- finding those lots when the name has an apostrophe in it -- is written once.
    """

    def _lot_name(self):
        return (self.request.GET.get("lot_name") or self.request.POST.get("lot_name") or "").strip()[:200]

    def _matching_lots(self):
        """The lots this name would be attached to.  Never touches a lot that already has one.

        Matched on the *normalised* name as well as literally, because half the links into this
        page carry a normalised name in the first place: the gaps page groups by it, and the "not a
        species" table has nothing else to link with -- the search cache only ever stored the
        normalised form.  On ``iexact`` alone every name whose original had an apostrophe or a
        capital came through here as "0 lots" and the button quietly did nothing.

        The ``icontains`` is only there to keep this off a full scan; the decision is the
        normalised comparison in Python, which is the same function the cache key and the matcher
        use.  It casts a deliberately wide net -- the longest few words *and* their singulars --
        because the narrowing runs on a name that has already lost its apostrophes: looking for
        "agassizs" would miss the very lot called "Agassiz's corydoras" this is here to find.
        """
        from auctions.species_matching import base_words, normalize, singularize

        name = self._lot_name()
        if not name:
            return Lot.objects.none()
        base = Lot.objects.filter(species__isnull=True, is_deleted=False, auction__use_scientific_name=True)
        # Only lots this person is actually responsible for.  This page used to be superusers
        # only, where "every lot on the site called this" was the right answer; it is open to
        # auction admins now, and the button would otherwise reach into other clubs' auctions.
        if not self.request.user.is_superuser:
            base = base.filter(auction__in=self.request.user.userdata.auctions_i_admin)
        normalized = normalize(name)
        words = sorted(base_words(name), key=len, reverse=True)[:3]
        if not normalized or not words:
            return base.filter(lot_name__iexact=name)
        narrowing = Q()
        for word in words:
            for form in {word, singularize(word)}:
                narrowing |= Q(lot_name__icontains=form)
        also = [
            pk
            for pk, lot_name in base.filter(narrowing).values_list("pk", "lot_name")
            if normalize(lot_name) == normalized
        ]
        return base.filter(Q(lot_name__iexact=name) | Q(pk__in=also))


class SpeciesCreateView(AuctionAdminAnywhereViewMixin, LotNameSpeciesMixin, CreateView):
    """Add a species, or a strain of one, from the site.

    Reached from :class:`SpeciesGapsView` with ``?lot_name=`` prefilled, which is the whole
    workflow: see a name that keeps coming up with no species, click it, fill in two boxes, and
    every lot with that name gets the species and the matcher learns the name for next time.

    Open to anyone who runs an auction, not just site superusers.  The reason is the check-in
    table: somebody is standing there with a bag of fish the picker has never heard of, and a
    workflow that ends in "email the site owner" ends in the lot going out with no scientific
    name.  What an auction admin adds is not everybody's, though -- it is ``approved=False``, so
    :func:`~auctions.species_matching.visible_species` offers it to them and to nobody else until
    a superuser ticks the box.  The imported list is a shared asset and one club's guess at a name
    has no business in another club's picker.
    """

    model = Species
    form_class = SpeciesAdminForm
    template_name = "species_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["lot_name"] = self._lot_name()
        kwargs["lot_count"] = self._matching_lots().count()
        # The form stamps added_by and decides approved from this.
        kwargs["added_by"] = self.request.user
        return kwargs

    def get_initial(self):
        initial = super().get_initial()
        name = self._lot_name()
        if name:
            # The lot name is the best guess at the common name -- it is what people call it.
            initial["common_name"] = name[:255]
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        name = self._lot_name()
        context["lot_name"] = name
        context["title"] = f"Add a species for “{name}”" if name else "Add a species"
        # Named in the page's first sentence, because "who will see this" is the question
        # somebody adding a species at a check-in table is really asking.  Often None -- see
        # UserData.only_club.
        context["club"] = self.request.user.userdata.only_club
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        species = self.object
        name = self._lot_name()
        attached = 0
        if name and form.cleaned_data.get("attach_to_lots"):
            from auctions.species_matching import remember as remember_species

            # save() rather than update(): it is what derives the lot's category from the species,
            # and these are tens of rows, not thousands.
            for lot in self._matching_lots()[:500]:
                lot.species = species
                lot.save()
                attached += 1
            # Teach the matcher, so the next person typing this name is offered it straight away.
            # A no-op while the species is unapproved -- remember() refuses to put one in a table
            # every club reads -- so the name is learned when the species is.
            remember_species(name, species, source="user", user=self.request.user)
        messages.success(
            self.request,
            f"Added {species.label}."
            + (f"  Set it on {attached} lot{'' if attached == 1 else 's'} called “{name}”." if attached else ""),
        )
        if not species.approved:
            messages.info(
                self.request,
                f"{species.label} is yours for now: it will be suggested on your lots and nobody "
                "else's until a site admin approves it for everyone.",
            )
        return response

    def get_success_url(self):
        return species_page_success_url(self.request)


class SpeciesCommonNameCreateView(AuctionAdminAnywhereViewMixin, LotNameSpeciesMixin, FormView):
    """Name a species that is **already** on the list, without opening the Django admin.

    The sibling of :class:`SpeciesCreateView`, and the one that should be reached more often.
    Most lot names with no scientific name are not a missing species: they are one of FishBase's
    36,000 filed under a name nobody in the hobby says.  *Labidochromis caeruleus* is "Blue streak
    hap" there and "yellow lab" everywhere else, and until this page existed an auction admin who
    hit that had exactly two options -- give up, or add a second *Labidochromis caeruleus*.  The
    duplicate table on the gaps page is made of people taking the second one.

    Open to anyone who runs an auction, on the same terms as adding a species: what a
    non-superuser writes is ``approved=False``, so it answers their own club's lookups and nobody
    else's until somebody approves it.  See
    :func:`~auctions.species_matching.visible_common_names`.

    Deliberately does **not** write to :class:`SpeciesSearchCache`.  The name itself is the
    teaching -- :func:`~auctions.species_matching.exact_matches` reads the name table before
    anything else runs -- and a cache row is global, so remembering the pairing here would push an
    unapproved club-scoped name into a table every club is served from.
    """

    form_class = SpeciesCommonNameForm
    template_name = "species_name_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["lot_name"] = self._lot_name()
        kwargs["lot_count"] = self._matching_lots().count()
        kwargs["added_by"] = self.request.user
        return kwargs

    def get_initial(self):
        initial = super().get_initial()
        # ?species= so the gaps page and the lot editor can open this with the answer already
        # picked; scoped, so a guessed id cannot be used to find out what another club added.
        wanted = self.request.GET.get("species") or ""
        if wanted.isdigit():
            initial["species"] = visible_species(self.request.user).filter(pk=int(wanted)).first()
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        name = self._lot_name()
        context["lot_name"] = name
        context["title"] = f"Name a species for \u201c{name}\u201d" if name else "Add a name to a species"
        # Who this name will answer for, so the page can say so.  Same rule as SpeciesCreateView:
        # a name is scoped by added_by and club exactly the way a species is, and often there is
        # no obvious club at all -- see UserData.only_club.
        context["club"] = self.request.user.userdata.only_club
        return context

    def form_valid(self, form):
        created = form.save()
        species = form.cleaned_data["species"]
        name = self._lot_name()
        attached = 0
        if name and form.cleaned_data.get("attach_to_lots"):
            # save() rather than update(): it is what derives the lot's category from the species,
            # and these are tens of rows, not thousands.
            for lot in self._matching_lots()[:500]:
                lot.species = species
                lot.save()
                attached += 1
        if created:
            written = ", ".join(f"\u201c{row.name}\u201d" for row in created)
            messages.success(
                self.request,
                f"{species.label} now answers to {written}."
                + (f"  Set it on {attached} lot{'' if attached == 1 else 's'}." if attached else ""),
            )
        else:
            messages.info(
                self.request,
                f"{species.label} already answered to {'that name' if len(form.cleaned_data['names']) == 1 else 'those names'}."
                + (f"  Set it on {attached} lot{'' if attached == 1 else 's'}." if attached else ""),
            )
        if any(not row.approved for row in created):
            messages.info(
                self.request,
                "That name is yours for now: it will be matched on your own lots and nobody "
                "else's until a site admin approves it for everyone.",
            )
        return redirect(species_page_success_url(self.request))
