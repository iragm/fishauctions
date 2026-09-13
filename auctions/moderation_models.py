"""Reports about content, copyright notices, and the strikes that come out of them.

These three models would sit in :mod:`auctions.models` with the other eighty if there were room.
That file is at the ceiling ``auctions/module_map.py`` holds it to and the ratchet there only ever
comes down, so they live here instead. They are a self-contained corner in any case: nothing else
on the site reads them, and they name the rest of the app only as ``"auctions.Lot"``-style strings,
which is what lets ``models.py`` import this from the top of its own import block without a cycle.

The three are separate because they are three different objects with three different clocks:

* :class:`ContentReport` is the general "something is wrong with this" queue behind the report
  button on a lot. App Store Review Guideline 1.2 requires an app carrying user-generated content
  to offer "a mechanism to report offensive content and timely responses to concerns", and a site
  hosting photographs uploaded by strangers wants one regardless.
* :class:`CopyrightNotice` is a notice under 17 U.S.C. 512(c)(3)(A), which is not a support ticket:
  it carries the six statutory elements, it starts an expeditious-removal obligation, and if a
  counter-notice arrives it starts the ten-to-fourteen business day window 512(g)(2)(C) puts before
  the material goes back up. A free-text message cannot stand in for any of that, which is why the
  form that writes these rows is its own form and not the contact form.
* :class:`CopyrightStrike` is the record that makes 512(i) true. The safe harbour is conditioned on
  adopting **and reasonably implementing** a repeat-infringer policy, and the case law is consistent
  about which half sites lose on. Cox had a written thirteen-strike policy it did not follow and
  lost the safe harbour; a one-man site with no written procedure at all kept it in *Ventura
  Content v. Motherless* because it actually did terminate repeat infringers. What separates those
  two is a record of decisions, so the record is a table rather than a habit.

:mod:`auctions.dmca` holds the policy these are counted against and the designated agent published
at ``/dmca/``.
"""

from django.conf import settings
from django.db import models


class ContentReport(models.Model):
    """Somebody reporting a lot for something other than a copyright claim.

    ``lot`` is ``SET_NULL`` rather than ``CASCADE`` and ``material`` records the URL as it was at
    the time: the usual way a report is resolved is by the offending lot ceasing to exist, and a
    moderation queue that deletes its own record of what it did the moment the thing is dealt with
    is no record at all.
    """

    REASONS = (
        ("OFFENSIVE", "Offensive, abusive, or harassing"),
        ("PROHIBITED", "An animal or item that shouldn't be sold here"),
        ("MISLEADING", "Misleading or dishonest listing"),
        ("SPAM", "Spam or a scam"),
        ("OTHER", "Something else"),
    )
    STATUSES = (
        ("OPEN", "Open"),
        ("ACTIONED", "Actioned"),
        ("DISMISSED", "Dismissed"),
    )

    lot = models.ForeignKey("auctions.Lot", null=True, blank=True, on_delete=models.SET_NULL)
    material = models.CharField(max_length=500, blank=True)
    material.help_text = "Where the reported content was, recorded when the report came in"
    reason = models.CharField(max_length=20, choices=REASONS)
    details = models.TextField(max_length=5000, blank=True)
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="content_reports_made",
    )
    reporter_email = models.EmailField(max_length=254, blank=True)
    reporter_email.help_text = "Filled in when the report came from somebody who wasn't signed in"
    status = models.CharField(max_length=10, choices=STATUSES, default="OPEN")
    admin_notes = models.TextField(blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="content_reports_resolved",
    )
    resolved_on = models.DateTimeField(null=True, blank=True)
    createdon = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-createdon"]

    def __str__(self):
        return f"{self.get_reason_display()} report on {self.material or 'deleted content'}"


class CopyrightNotice(models.Model):
    """A DMCA takedown notice, and whatever happened to it.

    Every field up to ``accurate`` is one of the six things 512(c)(3)(A) requires a notice to
    contain. They are stored rather than only emailed because the notice is the evidence for
    everything that follows it -- the removal, the strike, and the decision about a counter-notice
    -- and because a notice that turns out to be false is the other party's problem under 512(f)
    only if we still have what they actually sent.

    A notice that arrives by email instead (the designated agent's address is the address that
    counts, not this form) is entered here by hand. ``received_on`` is separate from ``createdon``
    for exactly that case: the clock started when the agent received it, not when it was typed in.
    """

    STATUSES = (
        ("RECEIVED", "Received - not yet acted on"),
        ("REMOVED", "Material removed"),
        ("INVALID", "Rejected - not a valid notice"),
        ("COUNTERED", "Counter-notice received"),
        ("RESTORED", "Material restored after counter-notice"),
        ("WITHDRAWN", "Withdrawn by the sender"),
    )

    #: 512(c)(3)(A)(iv) -- who is complaining, and how to reach them.
    name = models.CharField(max_length=100)
    email = models.EmailField(max_length=254)
    phone = models.CharField(max_length=50, blank=True)
    address = models.TextField(max_length=500)
    on_behalf_of = models.CharField(max_length=200, blank=True)
    on_behalf_of.help_text = "The copyright owner, if the sender is an agent acting for them"

    #: 512(c)(3)(A)(ii) -- what work is said to be infringed.
    work = models.TextField(max_length=5000)

    #: 512(c)(3)(A)(iii) -- what on this site is said to infringe it, and where.
    material = models.TextField(max_length=5000)

    #: 512(c)(3)(A)(v) and (vi) -- the two statements. Both are required for a notice to be
    #: complete, which is what ``is_complete`` is for.
    good_faith = models.BooleanField(default=False)
    accurate = models.BooleanField(default=False)

    #: 512(c)(3)(A)(i) -- the signature. A typed full legal name is an electronic signature.
    signature = models.CharField(max_length=100, blank=True)

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="copyright_notices_sent",
    )
    lot = models.ForeignKey("auctions.Lot", null=True, blank=True, on_delete=models.SET_NULL)
    lot.help_text = "The lot the material was on, when it could be worked out from the URLs given"

    status = models.CharField(max_length=10, choices=STATUSES, default="RECEIVED")
    received_on = models.DateTimeField(null=True, blank=True)
    received_on.help_text = "When the designated agent received it. Leave blank for notices sent through the form."
    actioned_on = models.DateTimeField(null=True, blank=True)
    admin_notes = models.TextField(blank=True)

    #: The counter-notice, if one comes back. 512(g)(3) wants a signature, identification of what
    #: was removed and where it was, a statement under penalty of perjury that it came down by
    #: mistake, and consent to the jurisdiction of a federal district court.
    counter_notice_on = models.DateTimeField(null=True, blank=True)
    counter_notice_signature = models.CharField(max_length=100, blank=True)
    counter_notice_contact = models.TextField(max_length=500, blank=True)
    counter_notice_text = models.TextField(max_length=5000, blank=True)
    counter_notice_consents_to_jurisdiction = models.BooleanField(default=False)
    #: 512(g)(2)(C): not less than 10 nor more than 14 business days after the counter-notice is
    #: forwarded, unless the complainant says they have filed suit.
    restore_no_earlier_than = models.DateTimeField(null=True, blank=True)
    restore_no_later_than = models.DateTimeField(null=True, blank=True)
    restored_on = models.DateTimeField(null=True, blank=True)

    createdon = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-createdon"]

    def __str__(self):
        return f"Copyright notice from {self.name} ({self.get_status_display()})"

    @property
    def is_complete(self):
        """Whether this has all six things 512(c)(3)(A) asks for.

        An incomplete notice does not start the removal obligation, and 512(c)(3)(B) says a notice
        missing pieces is not to be treated as knowledge of infringement. It is still worth reading
        and usually worth acting on -- this only says which kind of thing arrived.
        """
        return bool(self.signature and self.work and self.material and self.name and self.email and self.address) and (
            self.good_faith and self.accurate
        )


class CopyrightStrike(models.Model):
    """One strike against an account, and who decided it was one.

    Counted by :func:`auctions.dmca.strike_count`. Withdrawn strikes stay on the table with the
    reason attached rather than being deleted -- a strike that was lifted because the notice behind
    it was withdrawn is part of the story of the account, and deleting it would leave the same
    silence a site cannot afford when it has to show what its policy actually did.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="copyright_strikes",
    )
    notice = models.ForeignKey(CopyrightNotice, null=True, blank=True, on_delete=models.SET_NULL)
    reason = models.TextField(max_length=2000, blank=True)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="copyright_strikes_issued",
    )
    withdrawn = models.BooleanField(default=False)
    withdrawn.help_text = "A withdrawn strike stays on the record but stops counting towards termination"
    withdrawn_reason = models.TextField(max_length=2000, blank=True)
    createdon = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-createdon"]

    def __str__(self):
        return f"Copyright strike against {self.user} on {self.createdon:%Y-%m-%d}"
