"""The identification block every email this site sends has to carry.

US bulk commercial email (CAN-SPAM) needs a physical postal address and a working opt-out. Canada's
CASL is stricter and covers most of the traffic here: it wants the sender named, with a postal
address and a way to reach them, in *every* commercial message, and it exempts less than CAN-SPAM
does -- a "your lot didn't sell, relist it" nudge is a commercial message even though the recipient
asked for the account it belongs to.

So the footer goes on every templated email rather than on the ones somebody classified as
marketing, and the opt-out line appears wherever the send site put an ``unsubscribe`` token in the
context. That is the whole rule: :func:`auctions.templatetags.email_tags.email_footer` reads it off
the context, so a template carries the footer and a send site decides whether it offers the opt-out.

:data:`UNSET_MAILING_ADDRESS` is what ``settings.MAILING_ADDRESS`` says when nobody filled it in.
Printing that string in a footer is worse than printing nothing -- it reads as a bug to a member and
as an evasion to a regulator -- so :func:`mailing_address` returns an empty string instead, and the
admin setup checklist is what tells the owner to fix it.
"""

from django.conf import settings

#: settings.py's default for MAILING_ADDRESS. Also the placeholder ``.env.example`` ships.
UNSET_MAILING_ADDRESS = "No address configured"

#: The placeholder in `.env.example`, which a new deployment copies and may never edit. The admin
#: setup checklist prints the same line with different capitalization, so both are matched caselessly.
EXAMPLE_MAILING_ADDRESS = "123 your street, Anytown, USA"

_PLACEHOLDERS = frozenset({UNSET_MAILING_ADDRESS.casefold(), EXAMPLE_MAILING_ADDRESS.casefold()})


def mailing_address():
    """``settings.MAILING_ADDRESS`` if it is a real address, else an empty string."""
    address = (getattr(settings, "MAILING_ADDRESS", "") or "").strip()
    if address.casefold() in _PLACEHOLDERS:
        return ""
    return address
