"""Write ``chatgpt-app-submission.json``, the file OpenAI's plugin form imports.

The form asks for three hints and three justifications on every tool, plus five
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
    "add_lot": "the lot is listed on its auction's page, which anyone on the internet can read once the auction is public",
    "add_lots": "the lots are listed on their auction's page, which anyone on the internet can read once the auction is public",
    "answer_question": "the reply is posted publicly on the lot's page",
    "leave_feedback": "the rating and comment are shown on the other person's public profile",
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


#: What a destructive tool actually destroys, overwrites, sends for good, or commits to. OpenAI's
#: reviewer is checking exactly this, and it is never in the opening clause of a description, which
#: says what a tool is *for*. ``test_mcp.SubmissionFileTests`` fails a destructive tool with no entry.
_DESTROYS = {
    "delete_document": "It deletes a library document and its uploaded file for everyone who could read it.",
    "update_document": "It overwrites a library document's title, author, year or topics with the new ones.",
    "remove_lot_image": "It deletes the picture from the lot; putting it back means adding the image again.",
    "undo_check_in": "It clears a person's checked-in status, overwriting what the check-in desk recorded.",
    "refund_lot": (
        "It moves money: the refund comes off the invoice and the seller's payout, and for a card sale "
        "it is sent back through Square, which cannot be reversed from this site."
    ),
    "place_bid": "It places a bid that other bidders see at once and that the bidder cannot generally withdraw.",
    "retract_announcement": (
        "It deletes the announcement's Discord post and website entry, or cancels it for good if it "
        "had not gone out yet."
    ),
    "remove_dropdown_option": "It deletes one option from an auction's custom dropdown.",
    "remove_random_option": (
        "It deletes one option from an auction's custom random field and overwrites that value on every "
        "lot that had it with one of the remaining options."
    ),
    "cancel_volunteer_request": "It cancels a request for help and withdraws the notification sent with it.",
    "undo_last": "It reverses the user's previous change, overwriting whatever that change had set.",
    "contact_donation_vendor": "It sends an email to an outside business, which cannot be recalled once sent.",
    "undo_sale": "It clears the recorded winner and selling price of a lot.",
    "remove_lot": (
        "It deletes a lot from its auction, or deactivates a lot that is in no auction, which also "
        "removes the bids on it."
    ),
    "remove_bid": "It deletes a bid, and the lot's price falls back to the next-highest bid.",
    "remove_award": "It clears the points decision on a lot and takes back the points it had awarded.",
    "remove_person": (
        "It deletes a person's place in an auction, and is refused if they have an invoice, lots to "
        "sell or lots they won."
    ),
    "remove_invoice_adjustment": "It deletes one charge or discount line from an invoice that is still open.",
    "update_contact_info": (
        "It overwrites the user's name, phone number or address on their account and on the auctions "
        "and clubs that hold a copy of it."
    ),
    "update_username": "It replaces the user's username, which is also the address of their public page.",
    "change_email": (
        "It sends a confirmation email that cannot be recalled, and replaces the account's email "
        "address once that link is opened."
    ),
    "send_membership_card": (
        "It sends an email that cannot be recalled, though only to the address already on that "
        "membership and only with that member's own card."
    ),
    "resend_member_card": (
        "It sends an email that cannot be recalled, though only to the member's own address on file "
        "and never to a member marked do-not-contact."
    ),
    "set_lot_winner": (
        "It records a lot's winner and price, and when told to ignore errors it overwrites a sale "
        "that was already recorded."
    ),
    "update_person": (
        "It overwrites a participant's contact details, name, bidder number or note, and can take away "
        "their permission to bid or sell."
    ),
    "edit_lot": "It overwrites a lot's name, description, prices or other fields with new values.",
    "update_club_member": "It overwrites a club member's contact details, membership number, name or note.",
    "update_club_event": (
        "It overwrites an event's date, title or description, or calls it off, and pushes the change "
        "to the club's calendar and Discord."
    ),
    "send_club_announcement": (
        "Once its short retract window has passed it sends emails and push notifications that cannot "
        "be recalled; retract_announcement can still take down the Discord post and website entry."
    ),
    "update_club_setting": (
        "It overwrites one of a club's settings, including the wording of the welcome and renewal emails it sends."
    ),
    "update_pickup_location": (
        "It overwrites a pickup location's address, time or directions, which people may already have chosen."
    ),
    "rename_dropdown_option": "It overwrites the name of one option on an auction's custom dropdown.",
    "rename_random_option": "It overwrites the name of a random-field option on every lot that was given it.",
    "update_auction_setting": (
        "It overwrites one of an auction's settings, such as the minimum bid, the club's cut or "
        "whether the auction is listed publicly."
    ),
    "request_volunteers": (
        "It sends a push notification to everyone at the auction that cannot be unseen, though "
        "cancel_volunteer_request withdraws the request itself."
    ),
    "update_donation_vendor": ("It overwrites a donation vendor's status, contact details, notes or follow-up date."),
    "set_member_active": (
        "Deactivating a member takes away their membership until someone reactivates them, which "
        "revokes access even though nothing is deleted."
    ),
    "set_point_rule": (
        "It replaces what a genus or category is worth in a club's points program, which changes "
        "what later awards give."
    ),
    "set_lot_species": "It overwrites the scientific name recorded on a lot.",
    "leave_feedback": (
        "It overwrites any rating or comment the same person left on that lot before, and the result is public."
    ),
    "answer_question": (
        "It posts a public reply on the lot's page, which the seller cannot take back through this server."
    ),
}

#: Writes that reach outside the site or publish something, and are not destructive. The generic
#: sentence says whatever a tool does can be corrected afterwards, which a publication might not
#: be, so each says what takes it back.
_SENDS_BUT_KEEPS = {
    "add_club_event": (
        "It deletes nothing, and the event it posts can be moved, renamed or called off afterwards with "
        "update_club_event, which updates the calendar and Discord to match."
    ),
    "add_lot": "It only adds a new lot, deleting and overwriting nothing, and remove_lot takes it down again.",
    "add_lots": ("It only adds new lots, deleting and overwriting nothing, and remove_lot takes each one down again."),
}


def justifications(action: palette_actions.Action) -> dict[str, str]:
    """One sentence each, about this tool's behaviour rather than about its annotation.

    A read is not quite stateless: resolving an auction records it as ``last_auction_used``
    (``palette_actions.remember_auction``), and saying so is cheaper than a reviewer finding it.
    """
    what = behaviour(action)
    if action.danger == palette_actions.DANGER_NAVIGATE:
        read_only = (
            f"Only returns a link to a page on this site ({what}); nothing is saved unless the person "
            "does it on that page themselves."
        )
        destructive = "It only returns a link, so it deletes, overwrites and sends nothing."
    elif tools.read_only(action):
        read_only = f"Only looks up and returns data ({what})"
        if action.accepts("auction"):
            read_only += (
                "; the one thing it records is which auction the account last asked about, so the next "
                "request can leave it out"
            )
        read_only += "."
        destructive = "It only reads, so it deletes, overwrites and sends nothing."
    else:
        read_only = f"Changes data on this site: {what}."
        if action.destructive:
            destructive = _DESTROYS.get(
                action.name, "What it removes, overwrites or commits to cannot be put back by calling it again."
            )
        else:
            destructive = _SENDS_BUT_KEEPS.get(
                action.name,
                "It deletes nothing and takes no payment, and whatever it adds or changes can be "
                "corrected afterwards, on the website or with another tool here.",
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
