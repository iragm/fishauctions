"""Where people get stuck: one row per rejected form submission.

The *failure* half of the usability campaign's three measurements (USABILITY.md). Reach is
``PageView``; adoption is ``AuctionHistory.changed_fields``. Neither can see a page somebody
reaches, tries and gives up on, which looks like success in every reach metric and leaves no trace
in a changelog, because nothing was saved.

Four columns: ``form_name`` (the form class, since one form is reached from several URLs);
``field_errors`` (which field and why, as Django's error **codes** -- never the message, never the
value, because a code groups); ``attempt`` (how many times this person has been bounced without
getting through); and ``resolved`` (whether they finished -- the column the table is for).

**Two kinds of failure, and the second is the common one here.** A rejection is only visible when a
validator refuses something, and this site is built so it usually doesn't: nearly every field is
optional and the rest are filled in on save. A form whose validator never complains still gets
abandoned, and nothing on the server sees it.

So ``kind="abandoned"`` rows come from the page: ``unsaved_changes.js`` knows which fields changed
(it has to, to draw the unsaved-changes bar) and beacons that on the way out. ``field_errors`` there
is the set of field names edited and not saved, and ``seconds_on_page`` is how long they spent.

Here rather than in ``models.py`` for the same reason as :mod:`auctions.moderation_models`.
:mod:`auctions.form_friction` holds the view mixin that writes these rows.

**Nothing a user typed is stored**: only field names, from the code, and error codes, from the
validators.
"""

from django.db import models

KIND_CHOICES = (
    # The server said no. Rare here by design, which is why it can't be the only thing recorded.
    ("rejected", "Submitted and rejected"),
    # Edited and left without saving: the common shape of giving up, and invisible to the server.
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
            # unresolved ones.
            models.Index(fields=["form_name", "resolved"]),
            models.Index(fields=["kind", "timestamp"]),
        ]

    def __str__(self):
        fields = ", ".join(self.field_errors) if self.field_errors else "no field"
        if self.kind == "abandoned":
            return f"{self.form_name} abandoned after editing {fields}"
        return f"{self.form_name} rejected on {fields} (attempt {self.attempt})"
