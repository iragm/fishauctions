"""Recover the individual talk titles from an imported speaker's run-on "Programs:" list.

    manage.py split_speaker_talks --dry-run --limit 20    # look at it first
    manage.py split_speaker_talks                          # write it back

The NEC WordPress export flattened each speaker's bulleted talk list into one unbroken
string, and there is nothing left to split on -- no delimiter, no markup, no line breaks:

    Killifish of Madagascar Filtration: Making a Debruyn Filter and a Working
    Mattenfilter Demo (Workshop) Seahorses, Pipefish, and Seadragons This Is the Golden
    Age of Aquarium Fish Tetras ...

The live site, its WordPress REST API and the 2023 Wayback snapshot all serve exactly this
same flattened text, so re-fetching recovers nothing -- the markup was lost upstream, before
this data ever reached northeastcouncil.org.  Splitting on capital letters doesn't work
either: titles contain internal capitals ("This Is the Golden Age of Aquarium Fish") and
some start lowercase.

So this asks the model where the boundaries are, and writes the titles back into the same
`programs` field one per line.  No schema change, and the speaker panel already renders that
field with `white-space: pre-line`, so the result shows up as a list.

Safety: the prompt forbids inventing, reordering or rewording, and every reply is checked --
if the split doesn't put the original characters back together, it's rejected and the
speaker is left exactly as they were.  Run `--dry-run` first.
"""

import json
import re
import time

from django.core.management.base import BaseCommand

from auctions.llm import LLMError, get_provider
from auctions.models import LLMUsage, Speaker

SYSTEM_PROMPT = """You split a run-on list of aquarium-society talk titles into the separate titles.

You are given one string: several talk titles that were concatenated with no separator when
a website exported them. Reply with JSON only:

  {"talks": ["Killifish of Madagascar", "Seahorses, Pipefish, and Seadragons"]}

Rules:
- Return the titles in the order they appear.
- Copy the text EXACTLY. Do not reword, retitle, fix spelling, expand abbreviations, change
  punctuation or capitalisation, or add anything. Concatenating your titles back together
  with single spaces must reproduce the input exactly.
- Do not invent titles and do not drop any text. Every word of the input belongs to some title.
- Titles often contain internal capitals, colons, parenthetical notes like "(Workshop)" or
  "(with Tony Terceira)", and bracketed editor's notes. Keep those with the title they belong to.
- A repeated title is fine -- these lists really do repeat. Return it twice.
- If you genuinely cannot tell where the boundaries are, return the whole input as a single
  title rather than guessing.

Reply with the JSON object and nothing else."""

#: Anything longer than this is a bio that lost its "Programs:" marker, not a talk list.
MAX_INPUT_CHARACTERS = 4000

#: Below this there's nothing to split -- a single short title.
MIN_INPUT_CHARACTERS = 40


def normalize(text):
    """Collapse whitespace so a split can be compared against its input."""
    return re.sub(r"\s+", " ", text or "").strip()


def split_is_faithful(original, talks):
    """True when `talks` is a pure split of `original` -- nothing added, dropped or reworded.

    This is the whole safety story for this command.  A model that decides to tidy up a title
    would silently corrupt the directory, and nobody would notice until a club booked a talk
    that doesn't exist, so the rejoined split has to match the input character for character
    (modulo whitespace) or the answer is thrown away.
    """
    if not talks:
        return False
    if any(not normalize(talk) for talk in talks):
        return False
    return normalize(" ".join(talks)) == normalize(original)


class Command(BaseCommand):
    help = "Use the configured LLM to split each speaker's run-on talk list into one title per line."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show the split without saving.")
        parser.add_argument("--limit", type=int, default=0, help="Only process this many speakers.")
        parser.add_argument(
            "--include-split",
            action="store_true",
            help="Also re-examine speakers whose talks are already on separate lines.",
        )
        parser.add_argument("--sleep", type=float, default=0.0, help="Seconds to wait between speakers.")

    def handle(self, *args, **options):
        provider = get_provider()
        if not provider.is_configured():
            self.stderr.write(
                self.style.ERROR(
                    "No LLM provider is configured. Set LLM_PROVIDER / LLM_MODEL and the API key in "
                    ".env (see .env.example) before running this."
                )
            )
            return

        queryset = Speaker.objects.filter(is_deleted=False).exclude(programs="").order_by("name")
        candidates = [
            speaker
            for speaker in queryset
            if len(speaker.programs) >= MIN_INPUT_CHARACTERS
            and len(speaker.programs) <= MAX_INPUT_CHARACTERS
            and (options["include_split"] or "\n" not in speaker.programs)
        ]
        if options["limit"]:
            candidates = candidates[: options["limit"]]

        if not candidates:
            self.stdout.write("No speakers have a run-on talk list to split.")
            return

        dry_run = options["dry_run"]
        self.stdout.write(f"Splitting {len(candidates)} talk lists with the {provider.model or provider.name} model.")
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — nothing will be written."))

        split = rejected = failed = 0
        for speaker in candidates:
            talks = self._ask_model(provider, speaker)
            if talks is None:
                failed += 1
                continue
            if not split_is_faithful(speaker.programs, talks):
                # The model rewrote something. Leave the speaker alone and say so -- a silently
                # "improved" talk title is worse than an unsplit one.
                rejected += 1
                self.stdout.write(self.style.WARNING(f"  {speaker.name:35.35}  rejected (split didn't match input)"))
                continue

            split += 1
            self.stdout.write(f"  {speaker.name:35.35}  {len(talks)} talks")
            for talk in talks:
                self.stdout.write(f"      • {talk}")
            if not dry_run:
                # update() rather than save(): nothing here should re-trigger the geocode signal.
                Speaker.objects.filter(pk=speaker.pk).update(programs="\n".join(talks))
            if options["sleep"]:
                time.sleep(options["sleep"])

        summary = f"{split} talk lists split, {rejected} rejected as unfaithful, {failed} model errors."
        self.stdout.write(self.style.SUCCESS(summary))

    def _ask_model(self, provider, speaker):
        """Ask the provider to split one talk list.  Returns a list of titles, or None."""
        content = json.dumps({"run_on_talk_list": speaker.programs}, ensure_ascii=False)
        try:
            result = provider.complete_json(SYSTEM_PROMPT, [{"role": "user", "content": content}])
        except LLMError as error:
            self.stderr.write(self.style.WARNING(f"  {speaker.name}: {error}"))
            return None
        LLMUsage.objects.create(
            user=None,
            model=result.model,
            prompt_tokens=result.prompt_tokens,
            cached_prompt_tokens=result.cached_prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
            query=f"split talks: {speaker.name}"[:600],
            response_kind="speaker_talks",
            success=True,
        )
        data = result.data if isinstance(result.data, dict) else {}
        talks = data.get("talks")
        if not isinstance(talks, list) or not all(isinstance(talk, str) for talk in talks):
            return None
        return [talk.strip() for talk in talks if talk.strip()]
