"""Reports about content, copyright notices, and the strikes that come out of them.

These would sit in :mod:`auctions.models` if there were room; that file is at the ceiling
``auctions/module_map.py`` holds it to. They are self-contained: nothing else reads them, and they
name the rest of the app only as ``"auctions.Lot"`` strings, so ``models.py`` can import this
without a cycle.

They are three objects with three different clocks:

* :class:`ContentReport` is the "something is wrong with this" queue behind the report button. App
  Store Review 1.2 requires an app with user-generated content to offer one.
* :class:`CopyrightNotice` is a notice under 17 U.S.C. 512(c)(3)(A): it carries the six statutory
  elements, starts an expeditious-removal obligation, and on a counter-notice starts the ten-to-
  fourteen business day window in 512(g)(2)(C). A free-text message can't stand in for that.
* :class:`CopyrightStrike` is what makes 512(i) true. The safe harbour is conditioned on adopting
  **and reasonably implementing** a repeat-infringer policy: Cox had a written policy it didn't
  follow and lost it; a one-man site with no written procedure kept it in *Ventura Content v.
  Motherless* because it acted. What separates them is a record, so the record is a table.

:mod:`auctions.dmca` holds the policy and the agent published at ``/dmca/``.
"""

from django.conf import settings
from django.db import models


class ContentReport(models.Model):
    """Somebody reporting a lot for something other than a copyright claim.

    ``lot`` is ``SET_NULL`` and ``material`` records the URL as it was: reports are usually resolved by
    the lot ceasing to exist, and a queue that deletes its own record of what it did is no record.
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

    Every field up to ``accurate`` is one of the six things 512(c)(3)(A) requires. They are stored
    because the notice is the evidence for the removal, the strike and any counter-notice -- and because
    512(f) only helps if we still have what was actually sent.

    A notice arriving by email (the agent's address is the one that counts) is entered by hand, which is
    why ``received_on`` is separate from ``createdon``: the clock started when the agent received it.
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

    #: 512(c)(3)(A)(iii): what here is said to infringe, and where.
    material = models.TextField(max_length=5000)

    #: 512(c)(3)(A)(v) and (vi): both statements, required for a complete notice (``is_complete``).
    good_faith = models.BooleanField(default=False)
    accurate = models.BooleanField(default=False)

    #: 512(c)(3)(A)(i): the signature. A typed full legal name is an electronic signature.
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

    #: The counter-notice, if one comes. 512(g)(3) wants a signature, identification of what was
    #: removed and where, a statement under penalty of perjury, and consent to federal jurisdiction.
    counter_notice_on = models.DateTimeField(null=True, blank=True)
    counter_notice_signature = models.CharField(max_length=100, blank=True)
    counter_notice_contact = models.TextField(max_length=500, blank=True)
    counter_notice_text = models.TextField(max_length=5000, blank=True)
    counter_notice_consents_to_jurisdiction = models.BooleanField(default=False)
    #: 512(g)(2)(C): 10 to 14 business days after the counter-notice is forwarded, unless the
    #: complainant says they have filed suit.
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

        An incomplete notice doesn't start the removal obligation, and 512(c)(3)(B) says it isn't knowledge
        of infringement. It is still worth acting on; this only says which kind arrived.
        """
        return bool(self.signature and self.work and self.material and self.name and self.email and self.address) and (
            self.good_faith and self.accurate
        )


class CopyrightStrike(models.Model):
    """One strike against an account, and who decided it was one.

    Counted by :func:`auctions.dmca.strike_count`. Withdrawn strikes stay with the reason attached: a
    strike lifted because its notice was withdrawn is part of the account's story, and deleting it would
    leave a silence when the policy has to be shown to have run.
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
