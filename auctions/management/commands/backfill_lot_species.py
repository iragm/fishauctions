"""Attach a species to the lots that were sold before there was a species list to pick from.

The site can only *offer* a scientific name at the moment somebody types a lot name, so every lot
that already existed when the list landed stays blank forever unless something goes back over
them.  That is tens of thousands of lots -- "Tropheus duboisi", "guppies", "6 cardinal tetras" --
and all of them turn up on the species-gaps page as work nobody needs to do by hand, because the
matcher already knows the answer.

The matcher is the whole command.  ``suggest_species(..., use_llm=False)`` is the same code the
add-lot form runs, minus the paid call: exact names, the remembered answers, then token and phrase
search.  It is deliberately strict -- "no match" is a better answer than a plausible wrong one, and
a wrong species here ends up on a printed label and in breeder points -- so the rule below is
simply to take its answer when it gives exactly one, and to leave the lot alone otherwise.

Two things it does *not* do, both for the same reason: a lot's category is derived from its
species, and moving a lot into a new category can move it between the BAP, HAP and Culture tracks.

* It writes with ``update()`` rather than ``save()``.  ``Lot._do_save`` would re-derive the
  category from the species it has just been given, so a lot sitting in Cichlids that turns out to
  be a java fern would become Aquatic plants and its ``bap_placeholder`` would go BAP -> HAP --
  while any ``BapAward`` already recorded against it still holds BAP points.  The lot page and the
  award would then disagree, on historical data, silently.
* ``--set-category`` opts back into deriving the category, but only for lots that are currently
  Uncategorized *and* have no award recorded.  There is nothing there for a new category to
  contradict, and Uncategorized is where a lot lands when the guesser had no idea in the first
  place.

Start with ``--dry-run``: it prints exactly what would be set, grouped by lot name and by where
the answer came from, and writes nothing.

    manage.py backfill_lot_species --dry-run
    manage.py backfill_lot_species --dry-run --auction my-club-fall-auction
    manage.py backfill_lot_species --limit 200
    manage.py backfill_lot_species --set-category
"""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Q

from auctions.models import Auction, BapAward, Category, Lot
from auctions.species_matching import normalize, suggest_species


class Command(BaseCommand):
    help = "Set Lot.species on historical lots from their lot name, where the matcher is certain."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would change and write nothing.",
        )
        parser.add_argument(
            "--auction",
            help="Only lots in this auction, by slug.  Default is every auction that uses scientific names.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            help="Stop after this many distinct lot names.  The names are worked through commonest first.",
        )
        parser.add_argument(
            "--set-category",
            action="store_true",
            help=(
                "Also file the lot under its species' category, but only where the lot is "
                "Uncategorized and has no BAP award.  Off by default; see the module docstring."
            ),
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        slug = options["auction"]
        limit = options["limit"]
        set_category = options["set_category"]

        lots = Lot.objects.filter(
            species__isnull=True,
            is_deleted=False,
            banned=False,
            auction__use_scientific_name=True,
        ).exclude(lot_name="")
        if slug:
            if not Auction.objects.filter(slug=slug).exists():
                msg = f"No auction with slug {slug!r}."
                raise CommandError(msg)
            lots = lots.filter(auction__slug=slug)

        # Commonest first: a name on 400 lots is worth getting right, and --limit should spend
        # itself on those rather than on the long tail of one-offs.
        names = list(lots.values("lot_name").annotate(count=Count("pk")).order_by("-count", "lot_name"))
        if limit:
            names = names[:limit]
        self.stdout.write(f"{len(names)} distinct lot name(s) with no species.")

        uncategorized = Category.objects.filter(name="Uncategorized").values_list("pk", flat=True).first()
        # One answer per normalised name: "Guppies", "guppies" and "guppies " are one question.
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
                found, source = suggest_species(name, use_llm=False)
                # Exactly one, or nothing.  A shortlist is the matcher saying it cannot tell a
                # Chindongo saulosi from an Aulonocara saulosi, and neither can this command.
                answers[key] = (found[0], source) if len(found) == 1 else (None, source)
            species, source = answers[key]
            if species is None:
                continue

            # Materialised rather than left as a queryset: the filter above joins to Auction, and
            # the lot numbers are wanted twice anyway.
            pks = list(lots.filter(lot_name=name).values_list("pk", flat=True))
            if not pks:
                continue
            movable_pks = []
            if set_category and species.category_id and species.category_id != uncategorized:
                movable_pks = list(
                    Lot.objects.filter(pk__in=pks)
                    .filter(Q(species_category__isnull=True) | Q(species_category__name="Uncategorized"))
                    .exclude(pk__in=BapAward.objects.filter(lot__isnull=False).values("lot_id"))
                    .values_list("pk", flat=True)
                )

            matched_names += 1
            matched_lots += len(pks)
            categorised += len(movable_pks)
            by_source[source] = by_source.get(source, 0) + len(pks)
            verb = "would set" if dry_run else "setting"
            self.stdout.write(
                f"  {verb} {species.label} on {len(pks)} lot(s) called {name!r} (via {source})"
                + (f", and filing {len(movable_pks)} of them under {species.category}" if movable_pks else "")
            )
            if dry_run:
                continue
            # update(), not save(): see the module docstring.  Two statements rather than one so a
            # lot only moves category when it qualified above.
            Lot.objects.filter(pk__in=pks).update(species=species)
            if movable_pks:
                Lot.objects.filter(pk__in=movable_pks).update(
                    species_category_id=species.category_id,
                    category_automatically_added=True,
                    category_checked=True,
                )

        summary = (
            f"{matched_names} name(s) matched, covering {matched_lots} lot(s)"
            + (f", {categorised} refiled" if set_category else "")
            + f".  By source: {', '.join(f'{source} {count}' for source, count in sorted(by_source.items())) or 'none'}"
        )
        if dry_run:
            self.stdout.write(self.style.WARNING(f"Dry run — nothing written.  {summary}"))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
