"""Prompts: the recipes, offered to the *person* rather than to the model.

MCP's prompts are the primitive this server was missing for the longest, and the reason to want
them is not that they are new syntax. A tool is chosen by a model reading a description; a prompt
is chosen by a person picking it out of a menu. That difference is what makes a prompt the only
safe place on this server for a **multi-step recipe**: an instruction the model follows because a
tool result told it to is the whole prompt-injection problem, and an instruction the model follows
because somebody picked it off a menu is just a menu.

So the recipes that were prose in ``INSTRUCTIONS`` and in the resolver docstrings live here, where
they cost nothing until somebody asks for one -- which is also the token argument: none of this is
in the system prompt, and ``prompts/list`` is a few hundred bytes.

Four of them, and every one is a job that is several tool calls in a particular order with a
particular thing to be careful about. Anything that is one call is a tool and does not belong here.

Nothing in a prompt is filled in from a tool result. The arguments come from the person (a host
offers ``completion/complete`` to help them, and :func:`complete` answers it out of the auctions
and clubs they are actually in), and everything else is a constant written in this file. A prompt
that interpolated a lot description would be a prompt-injection surface with a menu entry.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from auctions import palette_actions


class Argument(NamedTuple):
    name: str
    description: str
    required: bool = False
    #: What ``completion/complete`` should offer for it: "auction", "club", or "" for free text.
    completes: str = ""


class Prompt(NamedTuple):
    name: str
    title: str
    description: str
    arguments: tuple[Argument, ...]
    #: ``{placeholders}`` are filled from the arguments; nothing else is interpolated.
    body: str


_AUCTION = Argument("auction", "Which auction. Its title or its slug — see my_context.", False, "auction")
_CLUB = Argument("club", "Which club. Its name or its slug — see my_context.", False, "club")


PROMPTS: tuple[Prompt, ...] = (
    Prompt(
        "run_check_in",
        "Work the door",
        "Check people in to an in-person auction, one at a time, and fix the mistakes a door table actually makes.",
        (_AUCTION,),
        "I am working the check-in desk at {auction} and will read you names as people arrive.\n"
        "\n"
        "Call my_context first and tell me which auction you have landed on, so we both know. "
        "Then, for each name I give you:\n"
        "\n"
        "1. check_in with the name exactly as I said it.\n"
        "2. Read the bidder number back to me. That is the number I write on their card, so say "
        "it even when nothing went wrong.\n"
        "3. If the reply says they were added from the club's members, tell me — it means this is "
        "their first time through the door and they are not on last year's list.\n"
        "\n"
        "Two things go wrong at a door and both are mine, not yours. If I give you a name that "
        "matches more than one person, read me the options and wait; do not pick. If I say I "
        "checked in the wrong person, use undo_check_in for that one person — there is no way to "
        "clear the whole room in one call and you should not look for one.\n"
        "\n"
        "Do not add lots, do not set winners, and do not touch invoices while we are doing this.",
    ),
    Prompt(
        "chase_unpaid",
        "Chase unpaid invoices",
        "Find everyone who still owes the club money after an auction, and draft what to send them.",
        (_AUCTION,),
        "Help me chase the unpaid invoices for {auction}.\n"
        "\n"
        "1. list_people with status='unpaid'. It returns 15 at a time — keep calling it with the "
        "offset it tells you until you have all of them, and say how many there are in total "
        "before you start writing anything.\n"
        "2. Some of those are people the *club* owes, not people who owe the club. The reply says "
        "which way round each one is. Separate the two lists and label them; a treasurer chasing "
        "somebody they owe money to is the worst version of this job.\n"
        "3. For the ones who owe: draft one short message each, naming the amount and how to pay. "
        "Show me the drafts. Do not send anything — there is no tool here that sends them and you "
        "should not go looking for a way.\n"
        "\n"
        "Do not mark anything paid. If I tell you somebody has paid, use set_invoice_status for "
        "that one person and read the new total back to me.",
    ),
    Prompt(
        "set_up_next_year",
        "Set up next year's auction",
        "Copy an auction you already ran, then check the handful of things that are different this time.",
        (_AUCTION, Argument("when", "When it starts, e.g. 2027-04-17T10:00.", False)),
        "Set up next year's version of {auction}, starting {when}.\n"
        "\n"
        "1. create_auction, copying that one. It only ever copies, so the fees, the rules text, "
        "the custom fields and the pickup locations all come across and nothing is invented.\n"
        "2. Then read me back, from describe_auction: the start date, the lot submission dates, "
        "and the pickup times. Those are the four things a copy gets wrong, because they are "
        "shifted from last year's rather than chosen.\n"
        "3. Tell me it is not listed publicly. A copy is never promoted, on purpose — promoting it "
        "is update_auction_setting and it is a decision to make on purpose, not part of copying.\n"
        "\n"
        "Do not change the fees. If they are wrong I will tell you which one.",
    ),
    Prompt(
        "write_announcement",
        "Write a club announcement",
        "Draft something to send to a club's members, and check where it will land before it goes.",
        (_CLUB,),
        "Help me write an announcement for {club}.\n"
        "\n"
        "First describe_club and tell me what is connected: whether it has Discord, an email "
        "provider, and a website snippet. That decides where this can go, and I would rather know "
        "before I write it than after.\n"
        "\n"
        "Then ask me what it is about and draft it. Two or three sentences: every channel carries "
        "the whole announcement and there is no page to read the rest on, so it has to be complete "
        "in itself. Do not write a subject line — the site writes that.\n"
        "\n"
        "When I am happy with it, send_club_announcement with the channels I name. Tell me it goes "
        "out after a short delay and that retract_announcement is what stops it. Do not send it "
        "until I have read the draft back.",
    ),
)

BY_NAME = {prompt.name: prompt for prompt in PROMPTS}


def descriptors() -> list[dict[str, Any]]:
    """The ``prompts/list`` answer."""
    return [
        {
            "name": prompt.name,
            "title": prompt.title,
            "description": prompt.description,
            "arguments": [
                {"name": argument.name, "description": argument.description, "required": argument.required}
                for argument in prompt.arguments
            ],
        }
        for prompt in prompt_list()
    ]


def prompt_list() -> tuple[Prompt, ...]:
    """Every prompt. Not filtered by permission, and that is the same call ``resources/list`` makes.

    A prompt is a recipe with no data in it. Offering "chase unpaid invoices" to somebody who runs
    no auction costs them a menu entry that answers with a refusal the first time they use it;
    filtering the menu would instead tell anybody who listed it which of these jobs they are
    allowed to do, which is a fact about their permissions and buys nothing.
    """
    return PROMPTS


def render(name: str, arguments: dict[str, Any] | None) -> dict[str, Any] | None:
    """One prompt, filled in. ``None`` when there is no prompt by that name.

    An argument the person did not give is left as a readable placeholder rather than blank, so
    "chase the unpaid invoices for the auction you are in" still reads as a sentence -- and the
    model then has to ask, which is the right thing for it to do.
    """
    prompt = BY_NAME.get(name)
    if prompt is None:
        return None
    given = {key: str(value) for key, value in (arguments or {}).items() if value not in (None, "")}
    filled = {
        argument.name: given.get(argument.name) or f"(ask me which {argument.name})" for argument in prompt.arguments
    }
    return {
        "description": prompt.description,
        "messages": [
            {
                "role": "user",
                "content": {"type": "text", "text": prompt.body.format(**filled)},
            }
        ],
    }


#: How many suggestions ``completion/complete`` returns. The spec caps a page at 100; a person
#: picking an auction out of a dropdown does not want ninety-nine of them.
COMPLETION_LIMIT = 20


def complete(user, kind: str, typed: str) -> list[str]:
    """Values to offer for one prompt argument. The half of prompts that makes them usable.

    An auction slug is exactly the thing a person cannot type from memory, and without this a
    prompt argument is a free-text box that gets the auction wrong -- which is worse than no
    argument at all, because the recipe then runs confidently against last spring's auction.

    Scoped to what this person is actually in (``palette_actions._my_auctions``, the same list
    ``my_context`` answers with), so completing an argument can never enumerate the site.
    """
    typed = (typed or "").strip().lower()
    if kind == "auction":
        values = []
        for auction in palette_actions._my_auctions(user, limit=COMPLETION_LIMIT * 2):
            values.append(auction.title)
        matches = [value for value in values if typed in value.lower()] if typed else values
        return matches[:COMPLETION_LIMIT]
    if kind == "club":
        from auctions.models import ClubMember

        names = list(
            ClubMember.objects.filter(user=user, is_deleted=False)
            .select_related("club")
            .values_list("club__name", flat=True)
        )
        names = [name for name in names if name]
        matches = [name for name in names if typed in name.lower()] if typed else names
        return sorted(set(matches))[:COMPLETION_LIMIT]
    return []


def completes(name: str, argument: str) -> str:
    """What kind of thing one prompt argument is, for :func:`complete`. ``""`` for free text."""
    prompt = BY_NAME.get(name)
    if prompt is None:
        return ""
    for candidate in prompt.arguments:
        if candidate.name == argument:
            return candidate.completes
    return ""
