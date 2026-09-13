"""Where people get stuck: one row per rejected form submission.

This is the *failure* half of the usability campaign's three measurements (USABILITY.md).  Reach --
did anybody open this page -- is ``PageView``.  Adoption -- did anybody ever change this setting --
is ``AuctionHistory.changed_fields``.  Neither can see the case this table exists for: **a page
somebody reaches, tries, and gives up on**, which looks exactly like success in every reach metric
and leaves no trace at all in a changelog, because nothing was saved.

Four questions, four columns:

``form_name``
    Which form. The form class, not the URL, because one form is reached from several URLs and one
    URL serves several forms.
``field_errors``
    Which field, and *why*, as Django's error **codes** -- ``required``, ``invalid``,
    ``max_value`` -- never the message and never the value. A code groups: "of 400 bounces on this
    form, 380 were ``required`` on one field" is a label problem with an obvious fix, and it is not
    a sentence anybody could have read off the messages.
``attempt``
    How many times this person has been bounced off this form without getting through. The first
    bounce is a typo; the fourth is a form nobody can fill in.
``resolved``
    Whether they eventually finished. This is the column the whole table is for: an unresolved run
    of four attempts is somebody who left.

**Two kinds of failure, and the second one is the common one here.**  A rejected submission is
only visible when a validator actually refuses something, and this site is deliberately built so
that it usually does not: nearly every field is optional, and most of the rest are filled in on
save (auction dates, fees, lot numbers).  A form whose validator never complains still gets
abandoned -- somebody opens the auction settings, changes three things, cannot work out the
fourth, and closes the tab.  Nothing on the server sees that, which would have made this table a
record of the rare case and blind to the ordinary one.

So ``kind="abandoned"`` rows come from the page: ``unsaved_changes.js`` knows which fields have
actually changed (it has to, to draw the unsaved-changes bar) and beacons that on the way out.
``field_errors`` on one of those rows is the set of field names the person had edited and did not
save, and ``seconds_on_page`` is how long they spent before giving up.  Still no values -- see
below.

It lives here rather than in ``models.py`` for the same reason
:mod:`auctions.moderation_models` does -- that file is at the ceiling ``auctions/module_map.py``
holds it to, and the ratchet only comes down.  :mod:`auctions.form_friction` holds the view mixin
that writes these rows.

**Nothing a user typed is stored.**  Not the submitted values, not the rendered error messages --
only field names, which come from the code, and error codes, which come from the validators.  A
table of everything that failed validation across the site would otherwise be a table of
mistyped passwords and half-finished addresses.
"""

from django.db import models

KIND_CHOICES = (
    # The server said no. Rare on this site by design -- most fields are optional and most of the
    # rest are filled in on save -- which is exactly why it cannot be the only thing recorded.
    ("rejected", "Submitted and rejected"),
    # Edited and left without saving. The common shape of giving up on a form whose validator never
    # got a chance to complain, and invisible to anything watching the server.
    ("abandoned", "Edited and left without saving"),
)


class FormFailure(models.Model):
    """One submission the server rejected, or one form somebody edited and walked away from."""

    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default="rejected", db_index=True)
    form_name = models.CharField(max_length=100, db_index=True)
    form_name.help_text = "The form class that rejected the submission"
    url = models.CharField(max_length=600, blank=True, default="")
    url.help_text = "Site-relative path, the same shape as PageView.url"
    user = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL)
    session_id = models.CharField(max_length=100, blank=True, default="", db_index=True)
    session_id.help_text = "Who to join attempts up by when there is no user, as in PageView"
    field_errors = models.JSONField(default=dict, blank=True)
    field_errors.help_text = "{field name: [error code, ...]} -- codes only, never messages or values"
    attempt = models.PositiveSmallIntegerField(default=1)
    attempt.help_text = "How many consecutive rejections this person has had on this form"
    seconds_on_page = models.PositiveIntegerField(null=True, blank=True)
    seconds_on_page.help_text = "Abandonments only: how long they spent before leaving"
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    resolved = models.BooleanField(default=False)
    resolved.help_text = "Set when the same person later submits the same form successfully"
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            # The two queries the report makes: this form's bounces over a window, and the
            # unresolved ones. Both filter on form_name first.
            models.Index(fields=["form_name", "resolved"]),
            models.Index(fields=["kind", "timestamp"]),
        ]

    def __str__(self):
        fields = ", ".join(self.field_errors) if self.field_errors else "no field"
        if self.kind == "abandoned":
            return f"{self.form_name} abandoned after editing {fields}"
        return f"{self.form_name} rejected on {fields} (attempt {self.attempt})"
