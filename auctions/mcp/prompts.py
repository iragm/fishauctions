"""Prompts: multi-step recipes offered to the *person* to pick off a menu, not to the model.

A tool is chosen by a model reading a description; a prompt is chosen by a person, which is why a
prompt is the only safe place for a multi-step recipe -- an instruction the model follows because
somebody picked it off a menu is not prompt injection. Nothing in a prompt body is filled in from a
tool result; only the person's own arguments are interpolated.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from auctions import palette_actions
from auctions.documents.search import can_use_library


class Argument(NamedTuple):
    name: str
    description: str
    required: bool = False
    #: What ``completion/complete`` should offer for it: "auction", "club", or "" for free text.
    completes: str = ""
    #: What the body says when it isn't given; default "(ask me which <name>)".
    unsaid: str = ""


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
    Prompt(
        "work_donation_list",
        "Work the donation list from your email",
        "Run a club's donation requests from your own mailbox: record vendors' replies and what you sent, "
        "see who is due a nudge, and draft the next emails there.",
        (_CLUB,),
        "Work the donation list for {club} from my own email. You are my mail client and this site is "
        "the record: you read and write in my mailbox, and keep each vendor's row here up to date.\n"
        "\n"
        "First, check your tools. You need to search my email, read whole messages, and save drafts in "
        "it; Gmail's connector does all three. If you can't, stop and tell me how to connect my email "
        "to you (in Claude: Settings, Connectors, Gmail), and do none of what follows: a list that half "
        "matches my inbox is worse than one that doesn't. You only read my mail and save drafts in it. "
        "Don't label, archive, move, mark as read or delete anything, so the rest of my mail stays exactly "
        "where it arrived.\n"
        "\n"
        "1. my_context, then list_donation_vendors for the club, every page. Tell me which club you have "
        "landed on.\n"
        "2. Catch up. Search my mail, sent mail included, for messages to or from each vendor's email "
        "address, from a few days before their last_contact on (all of them, if they have none); one "
        "search can hold many addresses joined with OR. record_donation_email each one that is about "
        "asking them for a donation: direction 'sent' or 'received', its date with its offset, who it "
        "was from and to, its subject, its text without the earlier messages quoted under it, and the "
        "mailbox's message_id. A message_id already on file is not recorded twice, so going over the "
        "same mail again is harmless. For a reply, add a summary of what they said and what the club has "
        "to do next, under 200 characters, and a status: interested, promised, not_interested, or "
        "unclear. Auto-replies and out-of-office messages are unclear.\n"
        "   - Leave everything else out, even from a vendor's own address. Plenty of vendors are shops I "
        "buy from, and their receipts, orders and newsletters aren't donation mail. Nor is anything this "
        "site forwarded to me, which comes from its relay address with the club's name in brackets at "
        "the start of the subject: a donation reply among those is on the record already.\n"
        "   - Somebody not on the list who is plainly answering one of our requests, from a colleague's "
        "address, say: ask me which vendor it belongs to.\n"
        "   - If they ask not to be contacted again, update_donation_vendor with status do_not_contact, "
        "and tell me.\n"
        "   - If they say a donation is on its way or was dropped off, ask me whether it arrived before "
        "you mark it received. Somebody has to have it in hand.\n"
        "3. Tell me where things stand, briefly: who replied and what they said, who is due a follow-up "
        "(list_donation_vendors with status 'due'), and who is new and has never been asked. Then ask me "
        "which of them to write to.\n"
        "4. For each one I pick, describe_donation_vendor, then write the email and save it as a draft "
        "in my mailbox, to their address, as a reply in the same thread when there is one. End every "
        "one with the email_footer exactly as it comes: the club's postal address and the vendor's "
        "unsubscribe link go on every donation email. Skip anyone it says can't be contacted. For a "
        "vendor who takes requests through a form on their own site, give me what_their_form_asks_for "
        "and the link instead, and once I've filled it in, record_donation_contact. Don't send them "
        "unless I tell you to. List the drafts with a link to each.\n"
        "5. Once they've gone, find each one in my sent mail and record_donation_email it as 'sent', "
        "with the sent copy's own message_id. Whatever I send later is recorded the next time we do "
        "this.\n"
        "\n"
        "How to write them. Use only what describe_donation_vendor gives you (club_donation_context, the "
        "club's mailing address, club_next_event, the context on the vendor and the messages so far) and "
        "what I tell you. Never invent a name, a phone number, a date or a value. Never say a donation is "
        "tax deductible, or that the club is a registered charity, unless the club's own details say so. "
        "Warm and direct, no emoji, signed off from the club.\n"
        "   - A first approach: who the club is, what the event is, one modest ask with no dollar figure, "
        "and what the business gets, which is its name in front of local hobbyists who buy what it "
        "sells. Under 250 words.\n"
        "   - A nudge, when our last email had no answer: refer back to it in the first sentence, don't "
        "repeat the pitch, and make it easy to say no. Under 120 words.\n"
        "   - An answer to them: reply to what they asked in the first sentence, and don't introduce the "
        "club or ask again. If they asked where or how to send something, put the club's mailing "
        "address in the body. If they said no, thank them and say the club won't ask again. Under 150 "
        "words.\n"
        "\n"
        "None of this counts against the club's daily donation-email limit, which is only for mail this "
        "site sends.",
    ),
    Prompt(
        "find_donation_vendors",
        "Find vendors to ask",
        "Search the web for businesses that might donate to a club's raffle or auction, and add the ones "
        "you pick to its donation list.",
        (
            _CLUB,
            Argument(
                "looking_for",
                "What kind of businesses, and where, e.g. 'fish stores and pet shops within 30 miles'.",
                False,
                unsaid="I haven't said, so ask me what kind of businesses and how far away",
            ),
        ),
        "Find businesses to ask for a donation to a raffle or auction run by {club}. What I'm looking "
        "for: {looking_for}.\n"
        "\n"
        "1. my_context, then list_donation_vendors for that club, every page, so you know who is already "
        "on the list. Tell me which club you have landed on and how many vendors it has. Unless I said "
        "where, search near its club_location.\n"
        "2. Search the web. For each business that fits, get from its own website or its own social "
        "media page: its name, the email address it gives for enquiries or donation requests, the "
        "person to ask if one is named, and whether it takes donation requests only through a form on "
        "its site. Chains usually do, and for them the form's address is what you need.\n"
        "3. Leave out anyone already on the list. Never guess an email address (info@ their domain is a "
        "guess unless they print it), and never take one from a directory or somebody else's list: the "
        "club only writes to addresses a business gave out itself.\n"
        "4. Show me a table: the name, why it fits, how it takes requests, the email address or the "
        "form's link, and where you found it. Wait for me to say which to add.\n"
        "5. add_donation_vendor for each one I pick, with the email address, or with contact_method "
        "'webform' and the contact_url; the contact's name if you found one; and as context, what they "
        "sell, why they fit, and the page you found them on. That note is what makes the email to them "
        "worth reading.\n"
        "\n"
        "Don't contact anyone. Adding a vendor sends nothing, and that is all this is.",
    ),
    Prompt(
        "digitize_documents",
        "Digitize old club papers",
        "Read a folder of scans or photos of old newsletters and breeder reports, and add each article to a club's library.",
        (
            Argument(
                "folder",
                "Where the pages are: a folder on this computer, or 'attached' for pictures attached to this chat.",
                True,
            ),
            _CLUB,
            Argument(
                "about",
                "What they are, e.g. 'our newsletters, 1975-1982'. Optional.",
                False,
                unsaid="I haven't said, so tell me what they look like",
            ),
        ),
        "I have scans or photos of old pages in {folder}. What they are: {about}. I want them in "
        "{club}'s library on this site, read by you, because you read old print better than the "
        "site's own model does.\n"
        "\n"
        "First call my_context and tell me which club you have landed on. add_document only files "
        "under a club whose settings I can edit; if it refuses, stop and tell me rather than adding "
        "them without the club. Ask me once who should be able to read them: everyone on the site "
        "(the default), only the club's members, or only me.\n"
        "\n"
        "1. List every picture and PDF in {folder}, in page order: by file name, or by the date taken "
        "if the names don't say. Tell me how many there are. If you can't open them, stop and say so.\n"
        "2. Look at every page once and plan before you transcribe anything. One document per article "
        "or report: not one per page, and not one per newsletter issue, because someone searching for "
        "spawning Apistogramma wants the article, and an issue covers a dozen subjects. Put an "
        "article's pages back in order if they are out of order. Show me the plan as a short list "
        "(title, which files, author, year) and wait for me to say go.\n"
        "   In the plan, mark what you would leave out, and why: blank pages and adverts; membership "
        "lists, or anything else with people's addresses or phone numbers, which never go in the "
        "library; and articles reprinted from a magazine or a book, which aren't the club's to share. "
        "A byline is fine.\n"
        "3. If there are more than about 30 pages, work in batches of about 10, and tell me where you "
        "are after each one, so a long folder doesn't run out of room halfway through an article.\n"
        "4. For each document in the plan:\n"
        "   a. Transcribe every word exactly as printed, as Markdown, in reading order: headings as "
        "headings, tables as tables, lists as lists. Don't correct spelling, update names or "
        "summarise. Write [illegible] for a word you can't make out; never fill in a guess. Put "
        "<!-- page 1 --> on a line of its own before the first page's text, <!-- page 2 --> before "
        "the second, and so on.\n"
        "   b. For each photograph or drawing, add a line starting 'Picture:' that says what it shows "
        "and names any fish or plant you can identify. Don't describe the page itself.\n"
        "   c. End with a section headed '## Library notes', which is yours and not part of the "
        "transcription: a 'Source:' line naming the files it came from and the newsletter issue if one "
        "is printed, and a 'Species:' line listing every species the article names by its current "
        "scientific name, giving the old one too where it has changed (Cichlasoma nigrofasciatum is now "
        "Amatitlania nigrofasciata). The library tags species from that line.\n"
        "   d. search_documents for the title and for one distinctive sentence. If the article is already "
        "there, read_document it. If what's there is worse than yours, for example garbled or [illegible] "
        "where you could read it, tell me, and only if I agree, add yours and delete_document the old "
        "one. Otherwise skip it.\n"
        "   e. add_document with the text, the title as printed, the author as printed, the club, who "
        "can read it, and up to four topics from the list add_document gives. Give the year only if the pages show it: a dated "
        "masthead counts, but the folder name and what I told you above don't. Leave out anything you "
        "aren't sure of. The library fills in a blank, but it can't correct a wrong answer.\n"
        "5. At the end, list what you added with links, what you left out and why, and any pages you "
        "couldn't read well enough, so I can rescan them.\n"
        "\n"
        "Don't change documents that are already in the library except as step 4d says. If you notice "
        "a problem with one, tell me.",
    ),
    Prompt(
        "tidy_the_library",
        "Tidy a club library",
        "Fill in missing titles, authors, years and topics across a club's library, and list what needs a person to look at it.",
        (_CLUB,),
        "Go through {club}'s library and tidy its catalogue.\n"
        "\n"
        "1. list_documents for that club. Keep calling it with the offset it gives you until you have "
        "all of them, and tell me how many there are.\n"
        "2. Start with any that readers reported problems with (open_reports), then any whose title is "
        "just a file name, then any missing an author, a year or topics. read_document each one and "
        "work out what's missing from the text itself: the printed title, the byline, a date in the "
        "masthead or the text.\n"
        "3. update_document with only what you are sure of. Don't guess a year from how old the typing "
        "looks, and don't replace a title or author somebody has already filled in unless it is plainly "
        "wrong; if so, tell me why.\n"
        "4. Don't try to fix the text itself. You can't see the scan it was read from, so you can't tell "
        "a misreading from what was printed. Instead, list documents whose text looks garbled: runs of "
        "[illegible], words that aren't words, sentences that stop halfway. Those are worth rescanning "
        "and adding again with digitize_documents.\n"
        "5. List likely duplicates, meaning the same article added twice, but don't delete either one. "
        "Tell me which and I'll decide.\n"
        "\n"
        "At the end, give me a table of each document you changed and what you changed, then the "
        "garbled ones, then the duplicates.",
    ),
    Prompt(
        "ask_the_library",
        "Ask the library",
        "Answer a question from a club's old articles and breeder reports, quoting what they actually say.",
        (_CLUB, Argument("question", "What you want to know, e.g. 'how did members hatch killifish eggs?'.", False)),
        "Answer this from {club}'s library: {question}\n"
        "\n"
        "1. search_documents with the question, and again with the words an article from the 1970s "
        "would have used: older scientific names, and the hobby terms of the time. Use the species "
        "filter if the question is about one species.\n"
        "2. read_document around the best passages, starting where each one starts, so you see what "
        "comes before and after it.\n"
        "3. Answer in a few sentences, citing the article, its author and its year for each point, with "
        "the document's link. Quote where the wording matters.\n"
        "\n"
        "These are decades-old club articles, so say plainly where their advice is out of date or where "
        "two of them disagree. Don't fill gaps from your own knowledge without saying that's what you "
        "are doing. If the library doesn't answer the question, say so.",
    ),
)

BY_NAME = {prompt.name: prompt for prompt in PROMPTS}

#: Listed only to accounts the library is on for (``UserData.library_enabled``).
LIBRARY_PROMPTS = frozenset({"digitize_documents", "tidy_the_library", "ask_the_library"})


def descriptors(user=None) -> list[dict[str, Any]]:
    """The ``prompts/list`` answer. ``user=None`` lists every prompt."""
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
        for prompt in prompt_list(user)
    ]


def prompt_list(user=None) -> tuple[Prompt, ...]:
    """Every prompt, unfiltered by permission -- filtering the menu would leak who can do what -- except
    the library's, which aren't listed for someone without it.
    """
    if user is None or can_use_library(user):
        return PROMPTS
    return tuple(prompt for prompt in PROMPTS if prompt.name not in LIBRARY_PROMPTS)


def render(name: str, arguments: dict[str, Any] | None) -> dict[str, Any] | None:
    """One prompt, filled in; ``None`` for an unknown name. A missing argument becomes a readable
    placeholder so the model has to ask, rather than blank."""
    prompt = BY_NAME.get(name)
    if prompt is None:
        return None
    given = {key: str(value) for key, value in (arguments or {}).items() if value not in (None, "")}
    filled = {
        argument.name: given.get(argument.name) or argument.unsaid or f"(ask me which {argument.name})"
        for argument in prompt.arguments
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
