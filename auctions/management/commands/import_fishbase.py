"""Load the species picklist from a pinned FishBase snapshot, plus the curated aquarium list.

    manage.py import_fishbase                 # the whole picklist: FishBase + the curated list
    manage.py import_fishbase --check-version # is there a newer snapshot?
    manage.py import_fishbase --dry-run       # parse and report, write nothing
    manage.py import_fishbase --databases slb # opt in to SeaLifeBase as well
    manage.py import_fishbase --purge slb     # delete rows from a source nothing points at

Three files are the whole dependency (see :mod:`auctions.fishbase` for why the version is pinned
and why SeaLifeBase is no longer loaded by default):

* ``species.parquet``  -- SpecCode, Genus, Species, FBname, Fresh/Brack/Saltwater, FamCode,
  Aquarium
* ``comnames.parquet`` -- ComName, Language, SpecCode, PreferredName, Misspelling
* ``families.parquet`` -- FamCode, Family, Order

joined on SpecCode and FamCode.  Known misspellings are dropped: surfacing those as suggestions
would teach people the wrong name for their fish.

Re-running is safe.  Rows are matched on (source, SpecCode) -- the two databases both number from
1, so the source is half the key -- which means a second run updates in place rather than
duplicating, and a species someone's lot already points at keeps its primary key.

Two passes run after the download, both of them idempotent and both worth running on their own
(``--only-categories``, ``--only-curated``) when the mapping or the list has changed but the
snapshot hasn't:

* the **curated aquarium list** (:mod:`auctions.aquarium_species`) -- plants, invertebrates, live
  foods, and the fish cultivars, which FishBase has none of
* **categories** (:mod:`auctions.species_categories`) -- family and order mapped onto the site's
  own Category rows, so a lot with a species stops needing the keyword guesser

and one that only matters the first time: **legacy rows**.  A site that has been running since
before the species list existed has a handful of hand-typed rows left over from the old
``Product`` table.  They are matched by scientific name, any lots pointing at them are moved onto
the real row, and the leftover is deleted; anything that doesn't match is left alone with its
genus and epithet filled in so it at least searches properly.  ``--keep-legacy`` skips it.
"""

import io
import logging
import re

import httpx
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count

from auctions import aquarium_species
from auctions.fishbase import DATABASES, DEFAULT_DATABASES, FISHBASE_VERSION, available_versions, parquet_url
from auctions.models import Lot, Species, SpeciesCommonName, normalize_species_name
from auctions.species_categories import assign_categories

logger = logging.getLogger(__name__)

#: Only these become searchable common names.  FishBase carries 300+ languages; every extra one is
#: tens of thousands of rows that make an English-language search slower and noisier.
DEFAULT_LANGUAGES = ("English",)

#: Rows per bulk_create batch.  Big enough to be fast, small enough not to blow max_allowed_packet.
BATCH_SIZE = 2000


def _strip_nulls(value):
    """Return *value* as a clean string.

    FishBase columns contain embedded null bytes here and there -- ``fb_tbl()`` in rfishbase has
    explicit handling for the same thing.  MySQL rejects them on insert with an unhelpful error,
    so they come out here rather than in a debugging session later.
    """
    if value is None:
        return ""
    return str(value).replace("\x00", "").strip()


class Command(BaseCommand):
    help = "Populate the Species table from a pinned FishBase snapshot"

    def add_arguments(self, parser):
        parser.add_argument(
            "--check-version",
            action="store_true",
            help="List the snapshots on the mirror and say whether the pinned one is current, then exit.",
        )
        # Not --version: Django's base command already owns that flag.
        parser.add_argument(
            "--snapshot",
            default=FISHBASE_VERSION,
            help=f"Snapshot to load (default: the pinned {FISHBASE_VERSION}).",
        )
        parser.add_argument(
            "--languages",
            default=",".join(DEFAULT_LANGUAGES),
            help="Comma-separated FishBase languages to import common names for.",
        )
        parser.add_argument(
            "--databases",
            default=",".join(DEFAULT_DATABASES),
            help=(
                "Which databases to load: fb (FishBase, fish) and/or slb (SeaLifeBase, marine "
                "invertebrates).  Default: fb.  SeaLifeBase is opt-in; see auctions/fishbase.py."
            ),
        )
        parser.add_argument("--dry-run", action="store_true", help="Parse and report, but write nothing.")
        parser.add_argument("--limit", type=int, default=0, help="Only import this many species (for testing).")
        parser.add_argument(
            "--skip-curated",
            action="store_true",
            help="Don't load the curated aquarium list (plants, invertebrates, live foods, cultivars).",
        )
        parser.add_argument(
            "--only-curated",
            action="store_true",
            help="Load only the curated aquarium list, then stop.  No download.",
        )
        parser.add_argument(
            "--only-categories",
            action="store_true",
            help="Only re-map families and orders onto the site's categories, then stop.  No download.",
        )
        parser.add_argument(
            "--keep-legacy",
            action="store_true",
            help="Don't fold hand-added rows left over from the old Product table into the imported ones.",
        )
        parser.add_argument(
            "--only-legacy",
            action="store_true",
            help=(
                "Only report on (and, without --dry-run, fold in) hand-added species left over from "
                "the old Product table.  No download.  Run it with --dry-run first."
            ),
        )
        parser.add_argument(
            "--purge",
            default="",
            help=(
                "Delete every species from this source (fb/slb, or a source name) that no lot points at.  "
                "Use after turning SeaLifeBase off to get the 100k rows back out of the picker."
            ),
        )

    def handle(self, *args, **options):
        if options["check_version"]:
            self._check_version()
            return
        if options["purge"]:
            self._purge(options["purge"], dry_run=options["dry_run"])
            return
        if options["only_categories"]:
            self._assign_categories(dry_run=options["dry_run"])
            self._recompute_trade_ranks(dry_run=options["dry_run"])
            return
        if options["only_curated"]:
            self._load_curated(dry_run=options["dry_run"])
            self._assign_categories(dry_run=options["dry_run"])
            self._recompute_trade_ranks(dry_run=options["dry_run"])
            return
        if options["only_legacy"]:
            self._merge_legacy(dry_run=options["dry_run"])
            return

        try:
            import pyarrow.parquet as parquet
        except ImportError:
            self.stderr.write(
                self.style.ERROR(
                    "pyarrow is needed to read the FishBase parquet files.  It is in requirements.in; "
                    "run `pip install pyarrow` if this environment predates that."
                )
            )
            return

        version = options["snapshot"]
        languages = {name.strip() for name in options["languages"].split(",") if name.strip()}
        databases = [name.strip() for name in options["databases"].split(",") if name.strip() in DATABASES]
        if not databases:
            self.stderr.write(self.style.ERROR(f"--databases must name at least one of: {', '.join(DATABASES)}"))
            return

        total_created = total_updated = 0
        for database in databases:
            self.stdout.write(
                f"Loading {DATABASES[database]} {version} (languages: {', '.join(sorted(languages)) or 'all'})"
            )
            families = self._families(parquet, version, database)
            species_table = self._read(parquet, "species", version, database)
            species_rows = self._species_rows(species_table, DATABASES[database], families, limit=options["limit"])
            self.stdout.write(f"  {len(species_rows)} species")

            comnames_table = self._read(parquet, "comnames", version, database)
            common_rows = self._common_name_rows(comnames_table, languages, set(species_rows))
            self.stdout.write(f"  {sum(len(names) for names in common_rows.values())} common names")

            if options["dry_run"]:
                self.stdout.write(self.style.WARNING("Dry run — nothing written."))
                self._report_sample(species_rows, common_rows)
                continue

            created, updated = self._save(species_rows, common_rows, DATABASES[database])
            total_created += created
            total_updated += updated
        if options["dry_run"]:
            return
        self.stdout.write(self.style.SUCCESS(f"Done: {total_created} species added, {total_updated} updated."))
        if not options["skip_curated"]:
            self._load_curated(dry_run=False)
        if not options["keep_legacy"]:
            self._merge_legacy(dry_run=False)
        self._assign_categories(dry_run=False)
        self._recompute_trade_ranks(dry_run=False)

    def _load_curated(self, *, dry_run):
        """The plants, invertebrates, live foods and cultivars FishBase doesn't carry."""
        self.stdout.write(f"Loading the curated aquarium list ({aquarium_species.DATA_FILE.name})")
        result = aquarium_species.load(dry_run=dry_run)
        self.stdout.write(
            f"  {result.created} added, {result.updated} updated, {result.adopted} taught names only, "
            f"{result.common_names} common names added"
        )
        for skipped in result.skipped:
            self.stdout.write(self.style.WARNING(f"  skipped {skipped}"))
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — nothing written."))

    def _recompute_trade_ranks(self, *, dry_run):
        """Rebuild the "does anybody keep this?" ranking the suggestions are ordered by."""
        self.stdout.write("Ranking species by whether the hobby keeps them")
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — nothing written."))
            return
        changed = Species.recompute_trade_ranks()
        counts = {
            rank: Species.objects.filter(trade_rank=rank).count()
            for rank in (Species.TRADE_RANK_SPECIES, Species.TRADE_RANK_GENUS, Species.TRADE_RANK_NONE)
        }
        self.stdout.write(
            f"  {changed} updated — {counts[Species.TRADE_RANK_SPECIES]} in the hobby, "
            f"{counts[Species.TRADE_RANK_GENUS]} in a genus that is, "
            f"{counts[Species.TRADE_RANK_NONE]} neither"
        )

    def _assign_categories(self, *, dry_run):
        """Map family and order onto this site's Category rows."""
        self.stdout.write("Assigning categories from family and order")
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — nothing written."))
            return
        changed, resolver = assign_categories()
        self.stdout.write(f"  {changed} species categorised")
        # The whole mapping, not just the failures.  Every site names its categories differently
        # and the interesting mistake is not a hint that matched nothing -- a club with no Plants
        # category doesn't sell plants -- it is a hint that matched something *unexpected*, which
        # is invisible unless the answers are printed.
        for hint, category in resolver.report():
            self.stdout.write(f"    {hint:<26} -> {category.name if category else '—'}")
        counts = {
            row["category__name"] or "—": row["n"]
            for row in Species.objects.values("category__name").annotate(n=Count("pk")).order_by("-n")
        }
        self.stdout.write("  species per category: " + ", ".join(f"{name} {n}" for name, n in counts.items()))
        if resolver.unmatched_hints:
            # Not an error: a club with no Plants category is a club that doesn't sell plants.
            self.stdout.write(
                self.style.WARNING(
                    "  no category on this site matches: "
                    + ", ".join(resolver.unmatched_hints)
                    + " (add one, or add its name to CATEGORY_CANDIDATES in species_categories.py)"
                )
            )

    def _merge_legacy(self, *, dry_run=False):
        """Fold the old hand-typed ``Product`` rows into the imported species list.

        These are the rows a long-running site already had when the picklist arrived: a scientific
        name typed by a person, no SpecCode, ``source="manual"``.  They are not wrong so much as
        unmanaged -- no common names, no family, no habitat -- and while one sits next to the
        imported row for the same fish the picker offers the same species twice.

        Matching is on the scientific name and nothing else, which is safe precisely because it is
        strict: a legacy row that doesn't match an imported name is left exactly where it is, with
        its genus and epithet split out so that it at least turns up in a search.
        """
        legacy = list(Species.objects.filter(source="manual", speccode__isnull=True))
        if not legacy:
            return
        self.stdout.write(f"Folding in {len(legacy)} hand-added species")
        merged = repaired = 0
        for species in legacy:
            name = (species.scientific_name or "").strip()
            replacement = (
                Species.objects.filter(scientific_name__iexact=name, variety="")
                .exclude(pk=species.pk)
                .exclude(source="manual")
                .order_by("source")
                .first()
                if name
                else None
            )
            if replacement:
                lot_count = Lot.objects.filter(species=species).count()
                self.stdout.write(f"  merge {name or species.pk!r} -> {replacement.label} ({lot_count} lots)")
                if not dry_run:
                    Lot.objects.filter(species=species).update(species=replacement)
                    species.delete()
                merged += 1
                continue
            # Kept, but made searchable: the old table never split the name up, and the genus
            # column is what every lookup in species_matching indexes on.  Migration 0385 already
            # did this on deploy; this is here for rows added by hand since.
            if name and not species.genus:
                parts = re.split(r"\s+", name)
                species.genus = parts[0][:100]
                species.species = " ".join(parts[1:])[:150]
                if not dry_run:
                    species.save()
                repaired += 1
        self.stdout.write(f"  {merged} merged into imported species, {repaired} kept and split into genus + epithet")
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — nothing written."))

    def _purge(self, source, *, dry_run):
        """Delete every unused species from one source.  Lots keep whatever they point at."""
        source = DATABASES.get(source, source)
        queryset = Species.objects.filter(source=source)
        total = queryset.count()
        if not total:
            self.stdout.write(f"No species with source={source}.")
            return
        in_use = set(Lot.objects.filter(species__source=source).values_list("species_id", flat=True).distinct())
        # A variety would go with its parent on the cascade.  Keeping the parent is the honest fix,
        # so anything with children counts as in use too.
        in_use |= set(Species.objects.filter(parent__source=source).values_list("parent_id", flat=True).distinct())
        removable_pks = list(queryset.exclude(pk__in=in_use).values_list("pk", flat=True))
        self.stdout.write(
            f"{source}: {total} species, {total - len(removable_pks)} in use, {len(removable_pks)} to delete"
        )
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — nothing written."))
            return
        # In batches: Django's cascade handling pulls every related row into memory, and a single
        # delete() over 100,000 species and their common names is how you find that out the hard way.
        for index in range(0, len(removable_pks), BATCH_SIZE):
            batch = removable_pks[index : index + BATCH_SIZE]
            SpeciesCommonName.objects.filter(species_id__in=batch).delete()
            Species.objects.filter(pk__in=batch).delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {len(removable_pks)} species and their common names."))

    def _check_version(self):
        # FishBase and SeaLifeBase are published together and share a version, so checking one is
        # enough to know whether the pin is stale.
        versions = available_versions()
        newest = versions[-1] if versions else "?"
        self.stdout.write(f"Snapshots on the mirror: {', '.join(versions)}")
        self.stdout.write(f"Pinned in auctions/fishbase.py: {FISHBASE_VERSION}")
        if newest != FISHBASE_VERSION:
            self.stdout.write(
                self.style.WARNING(
                    f"{newest} is newer.  Bump FISHBASE_VERSION deliberately and re-run the import; "
                    "it is pinned so a new snapshot can't change the species list on its own."
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS("Up to date."))

    def _read(self, parquet, table, version, database):
        """Download one parquet file into memory and return it as a pyarrow Table."""
        url = parquet_url(table, version, database)
        self.stdout.write(f"  fetching {url}")
        with httpx.Client(timeout=300, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
        return parquet.read_table(io.BytesIO(response.content))

    @staticmethod
    def _column(table, name):
        """A column as a Python list, or a list of Nones when the snapshot doesn't carry it."""
        if name not in table.column_names:
            return [None] * table.num_rows
        return table.column(name).to_pylist()

    def _families(self, parquet, version, database):
        """``{famcode: (family, order)}``.

        A separate 664-row table, joined on FamCode, because the species table only carries the
        code.  A snapshot without it isn't an error -- the columns just stay blank, and every
        species falls back to the keyword guesser it used before.
        """
        try:
            table = self._read(parquet, "families", version, database)
        except httpx.HTTPError:
            self.stdout.write(self.style.WARNING("  no families table in this snapshot; family/order left blank"))
            return {}
        codes = self._column(table, "FamCode")
        families = self._column(table, "Family")
        orders = self._column(table, "Order")
        rows = {}
        for index, code in enumerate(codes):
            if code is None:
                continue
            rows[int(code)] = (_strip_nulls(families[index])[:100], _strip_nulls(orders[index])[:100])
        self.stdout.write(f"  {len(rows)} families")
        return rows

    def _species_rows(self, table, source, families=None, limit=0):
        """``{speccode: {...field values...}}`` for every usable species row."""
        families = families or {}
        codes = self._column(table, "SpecCode")
        genera = self._column(table, "Genus")
        species = self._column(table, "Species")
        fbnames = self._column(table, "FBname")
        fresh = self._column(table, "Fresh")
        brack = self._column(table, "Brack")
        salt = self._column(table, "Saltwater")
        famcodes = self._column(table, "FamCode")
        aquarium = self._column(table, "Aquarium")
        rows = {}
        for index, code in enumerate(codes):
            genus = _strip_nulls(genera[index])
            epithet = _strip_nulls(species[index])
            if code is None or not genus:
                continue
            famcode = famcodes[index]
            family, order = families.get(int(famcode), ("", "")) if famcode is not None else ("", "")
            rows[int(code)] = {
                "genus": genus[:100],
                "species": epithet[:150],
                "common_name": _strip_nulls(fbnames[index])[:255],
                # FishBase stores these as -1/0 rather than booleans.
                "freshwater": bool(fresh[index]),
                "brackish": bool(brack[index]),
                "saltwater": bool(salt[index]),
                "family": family,
                "order": order,
                # Free text in the source ("commercial", "never/rarely"...).  Stored as-is and
                # interpreted by Species.AQUARIUM_TRADE_VALUES rather than turned into a boolean
                # here, so a new value in a future snapshot is visible instead of silently false.
                "aquarium_use": _strip_nulls(aquarium[index])[:30],
                "source": source,
            }
            if limit and len(rows) >= limit:
                break
        return rows

    def _common_name_rows(self, table, languages, known_codes):
        """``{speccode: [(name, language, is_preferred), ...]}``, misspellings dropped."""
        names = self._column(table, "ComName")
        langs = self._column(table, "Language")
        codes = self._column(table, "SpecCode")
        preferred = self._column(table, "PreferredName")
        misspellings = self._column(table, "Misspelling")
        rows = {}
        seen = set()
        for index, code in enumerate(codes):
            if code is None or int(code) not in known_codes:
                continue
            if misspellings[index]:
                continue
            language = _strip_nulls(langs[index])
            if languages and language not in languages:
                continue
            name = _strip_nulls(names[index])
            if not name:
                continue
            # FishBase repeats the same name for a species across sources; one row each is plenty.
            key = (int(code), name.lower())
            if key in seen:
                continue
            seen.add(key)
            rows.setdefault(int(code), []).append((name[:255], language[:50], bool(preferred[index])))
        return rows

    def _report_sample(self, species_rows, common_rows):
        for code in list(species_rows)[:5]:
            row = species_rows[code]
            names = ", ".join(name for name, _, _ in common_rows.get(code, [])[:4])
            self.stdout.write(f"  {row['genus']} {row['species']} — {names or 'no common names'}")

    @transaction.atomic
    def _save(self, species_rows, common_rows, source):
        """Upsert species by (source, SpecCode), then replace their common names wholesale.

        Keyed on the source as well as the code because FishBase and SeaLifeBase both number their
        species from 1 -- matching on SpecCode alone makes the second import silently overwrite
        tens of thousands of rows from the first.

        Replacing rather than merging the common names keeps a re-import from accumulating names
        the source has since removed, and it is cheap: the whole set for one species is a handful
        of rows.
        """
        existing = {
            species.speccode: species for species in Species.objects.filter(source=source, speccode__isnull=False)
        }
        created = updated = 0
        to_create = []
        for code, fields in species_rows.items():
            species = existing.get(code)
            if species is None:
                to_create.append(Species(speccode=code, scientific_name="", **fields))
                created += 1
                continue
            changed = False
            for field, value in fields.items():
                if getattr(species, field) != value:
                    setattr(species, field, value)
                    changed = True
            if changed:
                # Not bulk_update: save() is what rebuilds scientific_name from genus + species.
                species.save()
                updated += 1
        for index in range(0, len(to_create), BATCH_SIZE):
            batch = to_create[index : index + BATCH_SIZE]
            for species in batch:
                # bulk_create skips save(), so build the denormalised columns here.
                species.scientific_name = " ".join(part for part in (species.genus, species.species) if part)
                species.common_name_normalized = normalize_species_name(species.common_name)
            Species.objects.bulk_create(batch, batch_size=BATCH_SIZE)

        by_code = {
            species.speccode: species for species in Species.objects.filter(source=source, speccode__in=species_rows)
        }
        # Replace this snapshot's names, and *only* this snapshot's names.  The extra
        # ``source=source`` is the whole reason SpeciesCommonName has a source column: without it
        # this line was the reason no hobby name could ever be added to a FishBase species and
        # survive.  FishBase has no idea that Labidochromis caeruleus is a "yellow lab" -- it files
        # it under "Blue streak hap" -- so those names have to be ours, and before this they lasted
        # exactly until somebody bumped FISHBASE_VERSION and re-ran the import.
        SpeciesCommonName.objects.filter(
            source=source, species__source=source, species__speccode__in=list(common_rows)
        ).delete()
        common_objects = []
        for code, names in common_rows.items():
            species = by_code.get(code)
            if not species:
                continue
            for name, language, is_preferred in names:
                common_objects.append(
                    SpeciesCommonName(
                        species=species,
                        name=name,
                        # bulk_create skips save(); this column is what every lookup matches on.
                        name_normalized=normalize_species_name(name),
                        language=language,
                        is_preferred=is_preferred,
                        source=source,
                    )
                )
        SpeciesCommonName.objects.bulk_create(common_objects, batch_size=BATCH_SIZE)
        return created, updated
