"""Prompts: multi-step recipes offered to the *person* to pick off a menu, not to the model.

A tool is chosen by a model reading a description; a prompt is chosen by a person, which is why a
prompt is the only safe place for a multi-step recipe -- an instruction the model follows because
somebody picked it off a menu is not prompt injection. Nothing in a prompt body is filled in from a
tool result; only the person's own arguments are interpolated.
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
        "build_an_integration",
        "Build an integration",
        "Work out what this site's API can already do, which key it needs, and what to write against it.",
        (_CLUB, Argument("goal", "What the integration should do, e.g. 'put our lots on our WordPress site'.", False)),
        "I want to build an integration for {club}: {goal}.\n"
        "\n"
        "Work out what this site already does before you write a line of it.\n"
        "\n"
        "1. my_context, then describe_club. Tell me which club you have landed on and what is "
        "already connected to it.\n"
        "2. club_website_snippets. If what I asked for is our events, our current auction, our "
        "latest announcement or our breeder award leaderboard on our own website, that is an "
        "embed — no key, no code, no integration. Say so and stop.\n"
        "3. club_api, for the keys we already have and what each one may do. Then call it again "
        "with the topic that covers what I asked for, and read the endpoints properly: the "
        "request shapes, the filters and the error codes are all written down there.\n"
        "4. read_source, only if the documentation leaves something out and the behaviour "
        "matters. That is this site's own published code, so it is the real answer.\n"
        "\n"
        "Then, before any code, tell me three things: which endpoints you will call, which key it "
        "will use, and — if no key of ours has the right permissions — the exact tick boxes I "
        "need and the page to tick them on. You cannot make a key and you cannot read the secret "
        "of one; I paste that in myself, and it is only ever shown once.\n"
        "\n"
        "Two rules for the code. The key comes out of the environment, never out of a literal in "
        "the file. And a key that reads private lot information never goes on a public page — if "
        "the page is public, ask for one without it.\n"
        "\n"
        "One more thing worth saying out loud: if this is a job somebody does by hand a few times "
        "a year, the tools you already have here do it, and the honest answer is that there is "
        "nothing to build. If it is the opposite — this site genuinely cannot do it — say so "
        "plainly and then request_a_skill, once, with what I was trying to do. Do not invent an "
        "endpoint that is not in the documentation.",
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
    from . import icons

    return [
        {
            "name": prompt.name,
            "title": prompt.title,
            "description": prompt.description,
            "arguments": [
                {"name": argument.name, "description": argument.description, "required": argument.required}
                for argument in prompt.arguments
            ],
            "icons": icons.for_prompt(prompt),
        }
        for prompt in prompt_list()
    ]


def prompt_list() -> tuple[Prompt, ...]:
    """Every prompt, unfiltered by permission -- filtering the menu would leak who can do what."""
    return PROMPTS


def render(name: str, arguments: dict[str, Any] | None) -> dict[str, Any] | None:
    """One prompt, filled in; ``None`` for an unknown name. A missing argument becomes a readable
    placeholder so the model has to ask, rather than blank."""
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


#: How many suggestions ``completion/complete`` returns.
COMPLETION_LIMIT = 20


def complete(user, kind: str, typed: str) -> list[str]:
    """Values to offer for one prompt argument, scoped to what this person is actually in so
    completion can never enumerate the site."""
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
