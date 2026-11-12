"""Export MCP selection scenarios or score recorded, independently observed client runs."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from auctions.mcp import evaluation, usage


class Command(BaseCommand):
    help = "Export the MCP evaluation set; --results scores JSONL client runs without calling tools or models."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument(
            "--results",
            type=Path,
            help="JSONL: case_id, client, ordered tools, optional completed/navigation_worked booleans",
        )

    def handle(self, *args, **options):
        try:
            if options["results"]:
                runs = [json.loads(line) for line in options["results"].read_text().splitlines() if line.strip()]
                result = evaluation.score(runs)
            else:
                result = {"catalog_version": usage.catalog_version(), "cases": evaluation.cases()}
        except (OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, indent=2, ensure_ascii=False))
