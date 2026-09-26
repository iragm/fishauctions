"""Write ``chatgpt-app-submission.json``, the file OpenAI's plugin form imports.

The form asks for three hints and three justifications on every one of the 114 tools, plus five
positive and three negative test cases. OpenAI's own skill for this reads a codebase and writes the
prose; this reads :data:`auctions.palette_actions.ACTIONS` instead, because the registry already
holds the truth the reviewer is checking -- what each tool does, whether it writes, whether it
destroys -- and a hand-written copy of it starts going stale the day a tool is renamed.

So every justification is built from the action's own ``description``, the same sentence the model
reads, and the annotations are the ones :func:`auctions.mcp.tools.descriptor` actually serves.
Regenerate after any registry change and upload it again::

    docker exec django python3 manage.py chatgpt_submission --write

The prose that *isn't* derivable is here: :data:`APP_INFO`, and the eight test cases, which name a
demo account's own auction and have to be true of whatever the reviewer is given.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from auctions import palette_actions
from auctions.mcp import tools

#: Written where the form expects to find it, next to the repo's other operator-facing files.
OUTPUT_PATH = Path(settings.BASE_DIR) / "chatgpt-app-submission.json"

#: The listing. ``subtitle`` has a 30-character ceiling the form enforces silently.
APP_INFO = {
    "display_name": "Auction Fish",
    "subtitle": "Run and bid in club auctions",
    "description": (
        "Auction Fish runs the fish and aquarium-plant auctions that aquarium clubs hold, and the "
        "club memberships behind them. Members can see what is for sale, add their own lots, bid, "
        "check what they owe and carry a membership card. The people running an auction can sign "
        "bidders in at the door, record what each lot sold for, settle invoices, and keep their "
        "club's members, events and breeder award points up to date. Every tool acts as the "
        "signed-in user and is refused whatever that person could not do on the website itself."
    ),
    "category": "PRODUCTIVITY",
}

#: A parenthetical between em dashes: a gloss for the model, noise in a justification.
_ASIDE = re.compile(r"\s+\u2014[^\u2014]{1,60}\u2014\s+")

#: Where a sentence stops saying what it does and starts listing what that covers.
_ENUMERATION = re.compile(r"(\s+\u2014\s+|:\s+)")

#: Shouting in a description is aimed at the model reading it; a reviewer reads prose. The acronyms
#: in the same sentences (BAP, HAP, CAP, QR, FAQ, API, URL, REST) have to survive, so this is a list
#: rather than a rule about capital letters.
_EMPHASIS = frozenset(
    {"ALREADY", "ANSWER", "ANY", "AUCTION", "ELSE", "FIRST", "NOT", "ONE", "ONLY", "OWN",
     "PERSON", "SAME", "SEE", "SEVERAL", "SOMEONE", "THIS", "WITHOUT"}
)  # fmt: skip

#: Long enough to be specific, short enough that the sentence around it stays one sentence.
MAX_CLAUSE = 130

#: Where an open-world tool actually reaches. The action's own description says what it does and
#: rarely names the far end, and the far end is the whole question a reviewer is asking of
#: ``openWorldHint``. A tool flipped to open-world without an entry here still gets a truthful
#: sentence, just a vaguer one.
_REACHES = {
    "send_club_announcement": (
        "the club's Discord server, push notifications to members' phones, its mailing list and its "
        "own website, all at once"
    ),
    "retract_announcement": "the Discord post and the club website entry it had already published",
    "send_membership_card": "an email to the address on that membership",
    "resend_member_card": "an email to the club member's own address",
    "contact_donation_vendor": "an email to an outside business, at an address this site does not own",
    "add_club_event": "the club's Discord server and the Google Calendar it is connected to",
    "update_club_event": "the club's Discord server and the Google Calendar it is connected to",
    "request_volunteers": "push notifications to the phones of everyone at that auction",
    "cancel_volunteer_request": "the push notification it had already sent to those phones",
    "change_email": "a confirmation email to the new address, which is what makes the change take effect",
    "read_source": "this site's own source code, published as a public repository",
}


def behaviour(action: palette_actions.Action) -> str:
    """The action's opening clause, trimmed to something a reviewer can read at speed.

    "Add one lot -- an item for sale -- to an auction." -> "add one lot to an auction". It is quoted
    into a frame rather than conjugated: half these descriptions coordinate two verbs ("cancel a
    request and withdraw the notification"), and a rule that bends the first one leaves the second
    one wrong.
    """
    sentence = action.description.strip().split(". ")[0].rstrip(".")
    sentence = " ".join(word.lower() if word in _EMPHASIS else word for word in sentence.split())
    sentence = _ASIDE.sub(" ", sentence)
    sentence = _ENUMERATION.split(sentence)[0].strip().rstrip(",;")
    if len(sentence) > MAX_CLAUSE:
        sentence = sentence[:MAX_CLAUSE].rsplit(" ", 1)[0].rstrip(",;")
    first, _, rest = sentence.partition(" ")
    if first.isalpha() and not (first.isupper() and len(first) > 1):
        sentence = " ".join(filter(None, [first.lower(), rest]))
    return sentence


def justifications(action: palette_actions.Action) -> dict[str, str]:
    """One sentence each, about this tool's behaviour rather than about its annotation."""
    what = behaviour(action)
    if tools.read_only(action):
        read_only = f"Reads and returns, changing nothing: {what}."
        destructive = "Reads only, so there is nothing for it to delete, overwrite or undo."
    else:
        read_only = f"Writes one row in this site's own database: {what}."
        if action.destructive:
            destructive = (
                "Cannot be taken back by calling it again: what it removes, overwrites or commits to "
                "is gone from the person's own record of the auction."
            )
        else:
            destructive = (
                "Adds or sets a value the same person can change again from the website; it deletes "
                "nothing and overwrites no earlier answer."
            )
    if action.open_world:
        open_world = f"Reaches outside this site: {_REACHES.get(action.name, what)}."
    else:
        open_world = (
            "Stays inside this site's own auctions and clubs, scoped to what the signed-in user may "
            "already see or do there."
        )
    return {
        "read_only_justification": read_only,
        "open_world_justification": open_world,
        "destructive_justification": destructive,
    }


def _case(description, prompt, triggered, expected):
    return {
        "description": description,
        "user_prompt": prompt,
        "file_attachment_urls": None,
        "tools_triggered": triggered,
        "expected_output": expected,
        "expected_output_url": None,
    }


#: Five, exactly. Each names one tool, because a case that depends on a chain of them is a case a
#: reviewer can fail for the wrong reason. Between them they cover the read a session opens with,
#: two of the four widgets, a seller's write and the money.
TEST_CASES = [
    _case(
        "Open a session by asking what the signed-in account is part of.",
        "What auctions and clubs am I part of?",
        "my_context",
        "Lists the auctions this account has joined or helps run and the clubs it belongs to, "
        "naming each one, so later requests can refer to an auction by name.",
    ),
    _case(
        "Ask an auction's own rules and dates.",
        "What are the rules for the spring auction, and can I still add lots to it?",
        "describe_auction",
        "Returns that auction's dates, whether lot submission is still open, and the club's rules "
        "in full. Renders as the auction widget where the host supports one.",
    ),
    _case(
        "Ask about one lot by the number printed on its label.",
        "Tell me about lot 14 in the spring auction.",
        "describe_lot",
        "Returns that lot's name, what it is, its photo, the current price and where it is "
        "collected. Renders as the lot widget where the host supports one.",
    ),
    _case(
        "Add a lot for sale as the signed-in seller.",
        "Add a lot to the spring auction: 6 juvenile apistogramma cacatuoides, minimum bid $10.",
        "add_lot",
        "Creates the lot in that auction under this account and echoes back its lot number, the "
        "name and the minimum bid. Refused if lot submission has closed.",
    ),
    _case(
        "Ask what this account owes or is owed.",
        "What do I owe for the spring auction?",
        "my_activity",
        "Returns this account's own invoice for that auction, itemised, with the total and whether "
        "it is paid. Renders as the invoice widget where the host supports one.",
    ),
]

#: Three, exactly. Out of scope; a thing this server deliberately cannot do at any size; and a thing
#: the account is not allowed to do, refused by the site rather than by the model.
NEGATIVE_TEST_CASES = [
    _case(
        "Do not trigger for general questions that have nothing to do with auctions or clubs.",
        "What's the weather in Chicago tomorrow?",
        None,
        "The app should not be invoked: it has no tool for weather and nothing in the request "
        "names an auction, a lot, a club or a member.",
    ),
    _case(
        "Do not attempt a bulk change; no tool here writes over a filter.",
        "Delete every lot in the spring auction.",
        None,
        "No tool should run. The server has no bulk write by design, and the app should say so "
        "rather than looping a single-row delete over an auction's lots.",
    ),
    _case(
        "Do not act on an auction this account does not administer.",
        "Mark bidder 12's invoice as paid in the Ohio club's fall auction.",
        None,
        "The request should not succeed. If the app tries, the server refuses it because the "
        "signed-in account has no admin rights on that auction; permission is checked on the "
        "server, never by the model.",
    ),
]


def build() -> dict:
    """The whole file, in the order OpenAI's importer documents.

    The catalogue is built from :func:`auctions.mcp.tools.tool_descriptors`, not from the registry
    directly, so what this claims and what ``tools/list`` serves the scanner cannot drift apart.
    """
    catalogue = {}
    for descriptor in sorted(tools.tool_descriptors(None), key=lambda built: built["name"]):
        action = palette_actions.ACTIONS[descriptor["name"]]
        catalogue[descriptor["name"]] = {
            "annotations": descriptor["annotations"],
            "justifications": justifications(action),
        }
    return {
        "$schema": "https://developers.openai.com/apps-sdk/schemas/chatgpt-app-submission.v1.json",
        "schema_version": 1,
        "app_info": APP_INFO,
        "tools": catalogue,
        "test_cases": TEST_CASES,
        "negative_test_cases": NEGATIVE_TEST_CASES,
    }


class Command(BaseCommand):
    help = "Write chatgpt-app-submission.json for the OpenAI plugin submission form."

    def add_arguments(self, parser):
        parser.add_argument("--write", action="store_true", help="Write the file instead of printing it.")

    def handle(self, *args, **options):
        document = json.dumps(build(), indent=2) + "\n"
        if not options["write"]:
            self.stdout.write(document)
            return
        OUTPUT_PATH.write_text(document, encoding="utf-8")
        self.stdout.write(f"wrote {OUTPUT_PATH} ({len(build()['tools'])} tools)")
