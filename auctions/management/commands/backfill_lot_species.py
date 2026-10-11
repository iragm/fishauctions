"""Attach a species to lots that predate the species list.

Three passes: ``--status`` reports what the list covers and how many lots have no species; no flags
runs the automatic pass, using ``suggest_species(..., use_llm=False)`` and applying an answer only
when exactly one candidate matches; ``--review`` works through the names the matcher can't settle,
commonest first, applying each decision to every spelling and remembering it.

Category is not derived by default: it comes from the species, and moving categories can flip a lot
between BAP tracks while an existing ``BapAward`` reflects the old one. Writes use ``update()``
rather than ``save()`` to avoid re-deriving it. ``--set-category`` opts back in for Uncategorized
lots with no award. ``--dry-run`` writes nothing.

    manage.py backfill_lot_species --status
    manage.py backfill_lot_species --dry-run --auction my-club-fall-auction
    manage.py backfill_lot_species --limit 200
    manage.py backfill_lot_species --set-category
    manage.py backfill_lot_species --review --limit 500
"""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from auctions.models import Species, SpeciesCommonName
from auctions.species_admin import (
    MAX_CHOICES,
    NameGroup,  # noqa: F401 -- re-exported: tests and older notes import these from here
    apply_species,
    automatic_answer,
    eligible_lots,
    group_key,  # noqa: F401
    name_counts,
    remember_not_a_species,
    review_groups,
)
from auctions.species_admin import status as list_status
from auctions.species_categories import CategoryResolver, hint_for
from auctions.species_matching import (
    normalize,
    species_already_named,
    species_carrying_common_name,
    split_scientific_name,
    suggest_species,
    visible_species,
)


class Command(BaseCommand):
    help = "Set Lot.species on historical lots from their lot name, automatically and then by hand."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would change and write nothing.  With --review, list the questions and ask none.",
        )
        parser.add_argument(
            "--auction",
            help="Only lots in this auction, by slug.  Default is every auction that uses scientific names.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            help="Stop after this many distinct lot names (or, with --review, questions).",
        )
        parser.add_argument(
            "--set-category",
            action="store_true",
            help=(
                "Also file the lot under its species' category, but only where the lot is "
                "Uncategorized and has no BAP award.  Off by default; see the module docstring."
            ),
        )
        parser.add_argument(
            "--status",
            action="store_true",
            help="Report what the species list covers and how many lots are missing a species, then stop.",
        )
        parser.add_argument(
            "--review",
            action="store_true",
            help="Work through the lot names the matcher could not settle on its own, commonest first.",
        )
        parser.add_argument(
            "--include-unmatched",
            action="store_true",
            help="With --review, also ask about names that matched nothing -- where the answer is usually a new species.",
        )
        parser.add_argument(
            "--min-lots",
            type=int,
            default=2,
            help="With --review, ignore names on fewer than this many lots.  A one-off is not yet a pattern.",
        )
        parser.add_argument(
            "--scan",
            type=int,
            default=5000,
            help=(
                "With --review, how many distinct lot names to run the matcher over before asking "
                "anything.  Commonest first, so the default covers far more lots than it sounds "
                "like; 0 means every name, which on a site with tens of thousands takes a while."
            ),
        )

    def handle(self, *args, **options):
        self.dry_run = options["dry_run"]
        self.set_category = options["set_category"]
        try:
            self.lots = eligible_lots(options["auction"])
        except LookupError as error:
            raise CommandError(str(error)) from error

        if options["status"]:
            self._status()
            return
        if options["review"]:
            self._review(options)
            return
        self._auto(options)

    # ------------------------------------------------------------------ shared

    def _names(self, limit=None):
        return name_counts(self.lots, limit)

    def _apply(self, species, names, *, teach=False):
        """:func:`~auctions.species_admin.apply_species` with this run's flags; ``(lots, refiled)``."""
        return apply_species(
            self.lots, species, names, set_category=self.set_category, teach=teach, dry_run=self.dry_run
        )

    # ------------------------------------------------------------------ status

    def _status(self):
        """Is there a list worth matching against, and how much is left to do."""
        numbers = list_status(self.lots)
        self.stdout.write("Species list")
        for source, count in sorted(numbers["species_by_source"].items()):
            self.stdout.write(f"  {source:<14}{count}")
        if not numbers["curated_by_category"]:
            self.stdout.write(
                self.style.WARNING(
                    "  The curated list is not loaded, so no plant, shrimp, snail or live-food lot can "
                    "match anything.  Run: manage.py import_fishbase --only-curated"
                )
            )
        else:
            self.stdout.write(
                "  curated by category: "
                + ", ".join(f"{name} {count}" for name, count in numbers["curated_by_category"])
            )
        self.stdout.write(
            f"  {numbers['strains']} of those are named strains (Blue Dream, Halfmoon...) "
            f"and {numbers['crosses']} are crosses (Tibee, Flowerhorn)"
        )

        done = numbers["lots_with_species"]
        missing = numbers["lots_without_species"]
        self.stdout.write("")
        self.stdout.write(
            f"Lots: {done} of {done + missing} have a species ({numbers['percent_done']}%); {missing} to go, "
            f"{numbers['distinct_names_without_species']} distinct names."
        )

    # ------------------------------------------------------------------ pass one

    def _auto(self, options):
        """Apply the matcher's answer wherever it is unambiguous, one lot name at a time."""
        names = self._names(options["limit"])
        self.stdout.write(f"{len(names)} distinct lot name(s) with no species.")
        answers = {}
        by_source = {}
        matched_names = 0
        matched_lots = 0
        categorised = 0

        for row in names:
            name = row["lot_name"]
            key = normalize(name)
            if not key:
                continue
            if key not in answers:
                answers[key] = automatic_answer(name)
            species, source = answers[key]
            if species is None:
                continue
            lots, refiled = self._apply(species, [name])
            if not lots:
                continue
            matched_names += 1
            matched_lots += lots
            categorised += refiled
            by_source[source] = by_source.get(source, 0) + lots
            verb = "would set" if self.dry_run else "setting"
            self.stdout.write(
                f"  {verb} {species.label} on {lots} lot(s) called {name!r} (via {source})"
                + (f", and filing {refiled} of them under {species.category}" if refiled else "")
            )

        summary = (
            f"{matched_names} name(s) matched, covering {matched_lots} lot(s)"
            + (f", {categorised} refiled" if self.set_category else "")
            + f".  By source: {', '.join(f'{source} {count}' for source, count in sorted(by_source.items())) or 'none'}"
        )
        if self.dry_run:
            self.stdout.write(self.style.WARNING(f"Dry run — nothing written.  {summary}"))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
        self.stdout.write(
            "Next: manage.py backfill_lot_species --review --dry-run  (the ones only a person can settle)"
        )

    # ------------------------------------------------------------------ pass two

    def _groups(self, options):
        """The questions left after the automatic pass, biggest first."""
        self.stdout.write(f"Working through {len(self._names(options['scan'] or None))} lot name(s)...")
        return review_groups(
            self.lots,
            include_unmatched=options["include_unmatched"],
            min_lots=options["min_lots"],
            scan=options["scan"],
            limit=options["limit"],
            progress=lambda done, total: self.stdout.write(f"  {done}/{total}"),
        )

    def _review(self, options):
        groups = self._groups(options)
        total = sum(group.lots for group in groups)
        self.stdout.write(f"{len(groups)} name(s) worth a decision, covering {total} lot(s).")
        if self.dry_run:
            for index, group in enumerate(groups, start=1):
                self.stdout.write(f"{index:>4}. {self._headline(group)}")
                for species in group.candidates[:MAX_CHOICES]:
                    self.stdout.write(f"        {species.label}")
            self.stdout.write(self.style.WARNING("Dry run — nothing written.  Drop --dry-run to work through these."))
            return

        decided = 0
        touched = 0
        for index, group in enumerate(groups, start=1):
            self.stdout.write("")
            self.stdout.write(f"[{index}/{len(groups)}] {self._headline(group)}")
            outcome = self._ask_about(group)
            if outcome is None:
                self.stdout.write("Stopped.")
                break
            if outcome:
                decided += 1
                touched += outcome
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"{decided} decision(s), covering {touched} lot(s)."))

    def _headline(self, group):
        """The commonest spelling, then the others it stands for."""
        others = ", ".join(f"{name!r}×{count}" for name, count in group.spellings[1:4])
        if len(group.spellings) > 4:
            others += f", +{len(group.spellings) - 4} more"
        bred = f", {group.bred} bred" if group.bred else ""
        return f"{group.display!r} — {group.lots} lot(s){bred}" + (f", also called {others}" if others else "")

    def _ask(self, prompt):
        """One line from the operator. A method so a test can answer without a terminal."""
        try:
            return input(prompt).strip()
        except EOFError:
            return "q"

    def _ask_about(self, group):
        """One question. Returns lots written, 0 for skipped, or None to stop the run."""
        candidates = list(group.candidates)
        while True:
            for number, species in enumerate(candidates[:MAX_CHOICES], start=1):
                category = f"  · {species.category}" if species.category else ""
                trade = "  · in the hobby" if species.trade_rank == Species.TRADE_RANK_SPECIES else ""
                self.stdout.write(f"   {number:>2}) {species.label}{category}{trade}")
            if not candidates:
                self.stdout.write("    (the list offers nothing for this name)")
            answer = self._ask("   number, s=search, a=add species, n=not a species, enter=skip, q=quit: ")
            if answer in {"q", "quit"}:
                return None
            if not answer:
                return 0
            if answer.isdigit() and 1 <= int(answer) <= len(candidates[:MAX_CHOICES]):
                return self._decide(group, candidates[int(answer) - 1])
            if answer in {"n", "no"}:
                return self._not_a_species(group)
            if answer.startswith("s"):
                query = answer[1:].strip() or self._ask("   search for: ")
                if query:
                    candidates = self._search(query)
                    if not candidates:
                        self.stdout.write("    nothing found — try a genus, or a=add species")
                continue
            if answer in {"a", "add"}:
                species = self._add_species(group)
                if species:
                    return self._decide(group, species)
                continue
            self.stdout.write("    ?")

    def _search(self, query):
        """Species matching *query*, wider than the matcher since a person is deciding."""
        found = {species.pk: species for species in suggest_species(query, use_llm=False)[0]}
        typed = query.strip()
        wide = visible_species().filter(
            Q(scientific_name__istartswith=typed)
            | Q(common_name__icontains=typed)
            | Q(common_names__name__icontains=typed)
        )
        for species in wide.select_related("category").distinct()[:MAX_CHOICES]:
            found.setdefault(species.pk, species)
        return list(found.values())[:MAX_CHOICES]

    def _decide(self, group, species):
        """Write one decision, after checking what it covers when that isn't obvious."""
        names = group.names
        if len(names) > 1:
            self.stdout.write(f"    {len(names)} spellings: {', '.join(repr(name) for name in names[:8])}")
            if self._ask("    apply to all of them? [Y/n]: ").lower() in {"n", "no"}:
                names = names[:1]
        lots, refiled = self._apply(species, names, teach=True)
        self.stdout.write(
            self.style.SUCCESS(f"    {species.label} set on {lots} lot(s)")
            + (f", {refiled} refiled under {species.category}" if refiled else "")
        )
        return lots

    def _not_a_species(self, group):
        """Remember "not a species" so nothing asks about this name again."""
        remember_not_a_species(group.names)
        self.stdout.write(f"    remembered {group.key!r} as not a species")
        return 0

    def _add_species(self, group):
        """Add a species, or a strain or cross of one, without leaving the review.

        A blank scientific name adds a cross (see :attr:`~auctions.models.Species.is_hybrid`).
        """
        typed = self._ask("    scientific name (Genus species), or blank for a cross: ")
        genus, epithet = split_scientific_name(typed)
        is_hybrid = not typed
        variety = self._ask("    what the trade calls the cross: " if is_hybrid else "    strain/cultivar, or blank: ")
        if is_hybrid and not variety:
            return None
        parent = None
        if variety and not is_hybrid:
            parent = visible_species().filter(genus__iexact=genus, species__iexact=epithet, variety="").first()
            if not parent:
                self.stdout.write(f"    {genus} {epithet} is not on the list yet — add the plain species first.")
                return None
        clash = species_already_named(genus, epithet, variety, is_hybrid=is_hybrid)
        if clash:
            self.stdout.write(f"    {clash.label} is already on the list.")
            return clash
        common_name = self._ask(f"    common name [{group.display}]: ") or group.display
        # Refused for the same reason the club API refuses it: one name on two species.
        carrying = species_carrying_common_name(common_name)
        if carrying:
            self.stdout.write(f"    “{common_name}” already names {carrying.label}.")
            common_name = self._ask("    another common name, or blank to skip: ")
        species = Species(
            genus=genus,
            species=epithet,
            variety=variety,
            parent=parent,
            is_hybrid=is_hybrid,  # save() clears genus/epithet/parent when this is set
            common_name=common_name[:255],
            source="admin",
            in_trade_override=True,  # a club sold one -- see Species.in_aquarium_trade
            freshwater=parent.freshwater if parent else True,
            family=parent.family if parent else "",
            order=parent.order if parent else "",
        )
        species.category = parent.category if parent else CategoryResolver().resolve(hint_for(species))
        species.save()
        if common_name:
            SpeciesCommonName.objects.create(
                species=species, name=common_name[:255], language="English", is_preferred=True, source="admin"
            )
        if species.genus:
            Species.recompute_trade_ranks(genus=species.genus)
        self.stdout.write(self.style.SUCCESS(f"    added {species.label}"))
        return species
