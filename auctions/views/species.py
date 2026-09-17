"""Adding species and common names, and the superuser's cleanup queue.

``/species/new/`` and ``/species/name/`` are open to auction runners; a non-superuser's row is
unapproved and scoped to them and their club. The gaps page approves, merges or rejects.
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

    ``?varieties=1`` includes strains, for the lot forms' "search all species" box (the way to a species
    the matcher missed). Without it, nominal species only, for the "strain of" field.
    """

    def get_queryset(self):
        # Same visibility as suggestions.
        queryset = visible_species(self.request.user)
        if self.request.GET.get("varieties") != "1":
            # Nominal only; hybrids pass parent__isnull but can't have strains.
            queryset = queryset.filter(parent__isnull=True, is_hybrid=False)
        if self.q:
            queryset = queryset.filter(
                Q(scientific_name__icontains=self.q)
                | Q(common_name__icontains=self.q)
                | Q(variety__icontains=self.q)
                | Q(common_names__name_normalized__icontains=normalize_species_name(self.q))
            ).distinct()
        # Suggestions' ordering: freshwater and in-trade first (species_matching._rank).
        return queryset.order_by("-freshwater", "trade_rank", "scientific_name")

    def get_result_label(self, result):
        return format_html("{}", result.label)


class SpeciesGapsView(AdminOnlyViewMixin, TemplateView):
    """Lot names that should have a species and don't, grouped by name, as a work queue.

    It doesn't guess which are hardware; each column is evidence (breeder claims, categories, the
    matcher's last verdict) and the reader judges. Only names of stopwords and numbers are dropped.
    """

    template_name = "species_gaps.html"

    #: Enough for a sitting; the tail is one-off names.
    LIMIT = 100

    def get_context_data(self, **kwargs):
        from auctions.species_matching import base_words, normalize

        context = super().get_context_data(**kwargs)
        # Only auctions with the field on; elsewhere nobody was offered the choice.
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
        # Merged in Python: the normalisation is Python.
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
                # Resolves now; these lots predate it or the seller declined.
                entry["verdict"] = "matches a species"
                entry["verdict_detail"] = verdict.species.label
            elif verdict.is_a_gap:
                # Identified but not on the list: a row for the curated CSV, and the cache row heals on import.
                entry["verdict"] = "missing from the list"
                entry["verdict_detail"] = verdict.scientific_name
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
        # Remembered wrong species: worse than a remembered "no", and shown nowhere else.
        context["mappings"] = list(
            SpeciesSearchCache.objects.filter(species__isnull=False)
            .select_related("species", "created_by")
            .order_by("-hits", "-createdon")[:50]
        )
        # Species admins added, visible only to them until approved here.
        context["pending"] = list(
            Species.objects.filter(approved=False)
            .select_related("added_by", "category", "parent", "club")
            .annotate(lots=Count("lot"))
            .order_by("-id")[:50]
        )
        # Retired pairings, undoable only here (species_matching.record_choice).
        context["rejections"] = list(SpeciesNameRejection.objects.select_related("species").order_by("-createdon")[:50])
        # Both halves of a pair carry the flag; show one line per pair, stably ordered by pk.
        flagged = list(
            Species.objects.filter(possible_duplicate__isnull=False)
            .select_related("possible_duplicate", "category", "added_by", "club")
            .annotate(lots=Count("lot"))
            .order_by("pk")[:100]
        )
        # One query for the other halves' lot counts.
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
    """Delete one remembered answer so the name is worked out again. The undo for
    :func:`species_matching.remember`.
    """

    def post(self, request, pk):
        row = get_object_or_404(SpeciesSearchCache, pk=pk)
        name = row.search_text
        row.delete()
        messages.success(request, f"Forgot the remembered answer for “{name}”.  It will be looked up again.")
        return redirect("species_gaps")


class SpeciesNameRejectionDeleteView(AdminOnlyViewMixin, View):
    """Let a retired pairing be matched again, usually because the rejections were about the lot names,
    not the species. The escape hatch for :func:`species_matching.record_choice`.
    """

    def post(self, request, pk):
        row = get_object_or_404(SpeciesNameRejection, pk=pk)
        name, species = row.search_text, row.species
        row.delete()
        messages.success(request, f"“{name}” may be matched to {species.label} again.")
        return redirect("species_gaps")


class SpeciesDuplicateDismissView(AdminOnlyViewMixin, View):
    """ "These two are not the same species." Clears the flag on both sides; some species really share a
    designated name.
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
    """Fold one species into another. Superusers only: irreversible, and which name the site keeps is the
    list maintainer's call. ``keep`` survives; the URL's pk is folded in.
    """

    def post(self, request, pk):
        duplicate = get_object_or_404(Species, pk=pk)
        keep = get_object_or_404(Species, pk=request.POST.get("keep") or 0)
        if keep.pk == duplicate.pk:
            messages.error(request, "A species cannot be merged into itself.")
            return redirect("species_gaps")
        # A strain and its parent aren't duplicates; merging would lose the strain.
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
    """Approve a species for everyone. Also writes the lot-name mapping that ``remember()`` refused while
    it was unapproved.
    """

    def post(self, request, pk):
        species = get_object_or_404(Species, pk=pk)
        if not species.approved:
            species.approved = True
            species.save()
            # Its names become everybody's at the same time.
            species.common_names.filter(approved=False).update(approved=True)
            # This row was invisible to the last genus-tier pass.
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
    """Where to go after adding or naming a species: ``?next=`` wins for everyone (these open from the lot
    editor in a new tab). Host-checked.
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
    """The lots a ``?lot_name=`` refers to, shared by the add-species and name-species pages."""

    def _lot_name(self):
        return (self.request.GET.get("lot_name") or self.request.POST.get("lot_name") or "").strip()[:200]

    def _matching_lots(self):
        """The lots this name would be attached to, never ones that already have a species.

        Matched on the normalised name too, since many links carry only that. The ``icontains`` keeps it off
        a full scan with a wide net (words and singulars); the decision is the normalised comparison.
        """
        from auctions.species_matching import base_words, normalize, singularize

        name = self._lot_name()
        if not name:
            return Lot.objects.none()
        base = Lot.objects.filter(species__isnull=True, is_deleted=False, auction__use_scientific_name=True)
        # Non-superusers: only lots in auctions they administer.
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
    """Add a species or strain from the site, usually from the gaps page with ``?lot_name=``.

    Open to auction runners, for the check-in table. What they add is unapproved and visible only to
    them until a superuser approves it. Matching lots get the species.
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
        from auctions.species_matching import normalize

        initial = super().get_initial()
        name = self._lot_name()
        if name:
            # The lot name is the best guess at the common name.
            initial["common_name"] = name[:255]
            # A gap row already knows the scientific name; don't make anyone retype it.
            gap = SpeciesSearchCache.objects.filter(search_text=normalize(name), species__isnull=True).first()
            if gap and gap.is_a_gap:
                initial["scientific_name_input"] = gap.scientific_name
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        name = self._lot_name()
        context["lot_name"] = name
        context["title"] = f"Add a species for “{name}”" if name else "Add a species"
        # Who will see it; often None (UserData.only_club).
        context["club"] = self.request.user.userdata.only_club
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        species = self.object
        name = self._lot_name()
        attached = 0
        if name and form.cleaned_data.get("attach_to_lots"):
            from auctions.species_matching import remember as remember_species

            # save(), not update(): it derives the category. Tens of rows.
            for lot in self._matching_lots()[:500]:
                lot.species = species
                lot.save()
                attached += 1
            # A no-op until approved; the name is learned when the species is.
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
    """Add a common name to a species already on the list; the usual fix ("yellow lab" for *Labidochromis
    caeruleus*), instead of a duplicate species.

    Same access and scoping as adding a species. Deliberately doesn't write ``SpeciesSearchCache``: the
    name table is read first anyway, and the cache is global.
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
        # ?species= preselects; scoped so a guessed id reveals nothing.
        wanted = self.request.GET.get("species") or ""
        if wanted.isdigit():
            initial["species"] = visible_species(self.request.user).filter(pk=int(wanted)).first()
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        name = self._lot_name()
        context["lot_name"] = name
        context["title"] = f"Name a species for \u201c{name}\u201d" if name else "Add a name to a species"
        # Who this name will answer for; often no club (UserData.only_club).
        context["club"] = self.request.user.userdata.only_club
        return context

    def form_valid(self, form):
        created = form.save()
        species = form.cleaned_data["species"]
        name = self._lot_name()
        attached = 0
        if name and form.cleaned_data.get("attach_to_lots"):
            # save(), not update(): it derives the category.
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
