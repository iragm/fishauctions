"""Turn recurring assistant answers into zero-token shortcuts.

The command palette's assistant costs one or more model calls per query, every time, forever. But
a lot of that traffic is the same handful of phrases: "where do I pay my dues", "print my labels",
"what does everyone owe me". Each time, the model is asked the same question and gives the same
answer.

This command finds those and writes them down. A phrase that has been asked at least
``--min-count`` times and resolved to the *same* destination every single time is one the model
never needs to be asked about again: it becomes a :class:`~auctions.models.CommandPalettePage`
pointing at that route, and from then on ``palette_assist.shortcut_match`` answers it locally for
nothing.

**The model's own repeated answers are the ground truth**, which is what makes this safe. Nothing
here scores or guesses at what a query means -- a phrase is only ever written down when the
assistant has already agreed with itself about it several times over. Unanimity is required, not a
majority: one disagreement and the phrase is left alone, because a query that resolves two
different ways is one where context matters and a fixed shortcut would be wrong some of the time.

The shortcut is still resolved per user at the point of use (``route:<key>`` targets go through
``palette_routes.resolve_route``, which re-runs every permission check), so writing one down never
grants access to anything.

Usage::

    manage.py mine_palette_shortcuts                  # report what it would create
    manage.py mine_palette_shortcuts --apply          # create them
    manage.py mine_palette_shortcuts --min-count 10   # be stricter
"""

import logging
from collections import defaultdict

from django.core.management.base import BaseCommand

from auctions import command_palette, palette_assist, palette_routes
from auctions.models import CommandPalettePage, LLMUsage

logger = logging.getLogger(__name__)

#: How many times a phrase must have been asked before it is worth writing down. Low enough to
#: catch the long tail on a busy site, high enough that one person experimenting doesn't create
#: shortcuts for everybody.
DEFAULT_MIN_COUNT = 5


class Command(BaseCommand):
    help = "Propose command palette shortcuts from queries the assistant keeps answering the same way."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually create the shortcuts. Without this, the command only reports.",
        )
        parser.add_argument(
            "--min-count",
            type=int,
            default=DEFAULT_MIN_COUNT,
            help=f"How many times a phrase must have resolved the same way (default {DEFAULT_MIN_COUNT}).",
        )

    def handle(self, *args, **options):
        min_count = options["min_count"]
        apply_changes = options["apply"]

        candidates, rejected = self.mine(min_count)

        if not candidates and not rejected:
            self.stdout.write(
                "No navigations recorded yet. This reads LLMUsage.destination, which is only "
                "written for queries the assistant answers with a navigation."
            )
            return

        existing = self.existing_phrases()
        created = skipped = 0
        for phrase, (route_key, count) in sorted(candidates.items(), key=lambda item: -item[1][1]):
            if phrase in existing:
                skipped += 1
                continue
            route = palette_routes.get_route(route_key)
            label = route.label if route else route_key
            self.stdout.write(f"  {count:5}x  {phrase!r} -> {route_key} ({label})")
            if apply_changes:
                CommandPalettePage.objects.create(
                    search_term=phrase[:200],
                    target=f"{command_palette.ROUTE_TARGET_PREFIX}{route_key}"[:100],
                    title=label[:200],
                    description="Created by mine_palette_shortcuts from repeated assistant answers.",
                )
                created += 1

        self.stdout.write("")
        self.stdout.write(f"{len(candidates)} phrase(s) resolved the same way at least {min_count} times.")
        if skipped:
            self.stdout.write(f"{skipped} already had a shortcut.")
        if rejected:
            self.stdout.write(
                f"{len(rejected)} phrase(s) were common enough but resolved inconsistently, so they "
                "were left for the model. The most disputed:"
            )
            for phrase, routes in sorted(rejected.items(), key=lambda item: -len(item[1]))[:10]:
                self.stdout.write(f"  {phrase!r} -> {', '.join(sorted(routes))}")
        if apply_changes:
            self.stdout.write(self.style.SUCCESS(f"Created {created} shortcut(s)."))
        elif candidates:
            self.stdout.write("Nothing was written. Re-run with --apply to create these.")

    def mine(self, min_count):
        """Group recorded navigations by normalized phrase.

        Returns ``(candidates, rejected)``: phrases that always resolved to one destination, and
        phrases common enough to qualify that didn't.
        """
        destinations = defaultdict(set)
        counts = defaultdict(int)
        rows = LLMUsage.objects.filter(success=True).exclude(destination="").exclude(query="")
        for query, destination in rows.values_list("query", "destination"):
            phrase = palette_assist.normalize_query(query)
            if not phrase:
                continue
            destinations[phrase].add(destination)
            counts[phrase] += 1

        candidates = {}
        rejected = {}
        for phrase, routes in destinations.items():
            if counts[phrase] < min_count:
                continue
            if len(routes) == 1:
                candidates[phrase] = (next(iter(routes)), counts[phrase])
            else:
                rejected[phrase] = routes
        return candidates, rejected

    def existing_phrases(self):
        """Every phrase already covered by a shortcut, normalized the same way as the queries."""
        phrases = set()
        for page in CommandPalettePage.objects.all():
            for phrase in command_palette._page_phrases(page):
                phrases.add(palette_assist.normalize_query(phrase))
        return phrases
