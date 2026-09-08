"""The view mixin that writes :class:`auctions.friction_models.FormFailure` rows.

Add ``FormFrictionMixin`` **first** in a form view's bases and it records every rejected
submission, and marks the run resolved when the person finally gets through:

    class AuctionUpdate(FormFrictionMixin, LoginRequiredMixin, AuctionViewMixin, UpdateView):

First, because it works by overriding ``form_valid``/``form_invalid`` and calling ``super()`` --
behind Django's own ``FormMixin`` in the MRO it would never be reached.
``test_form_friction.FormFrictionWiringTests`` checks the ordering on every view that uses it, so
a view that adds it in the wrong place fails the build rather than silently measuring nothing.

Three things this deliberately does not do:

* **It does not swallow anything.** Every write is inside ``try``: an instrument that can 500 a
  form is worse than no instrument, and this one runs on the error path, which is where the
  unusual state already is.
* **It does not store what anybody typed.** Field names and Django error codes only. See the
  module docstring on ``friction_models``.
* **It does not count a fresh GET as an attempt.** The counter lives in the session, keyed by form,
  and only ``form_invalid`` moves it.

The session counter is what makes ``attempt`` mean "in a row" rather than "ever", and it is also
what closes a run out: ``form_valid`` marks this person's open failures on this form resolved and
clears the key, so a run is bounded by either a success or the end of the session.
"""

from __future__ import annotations

import logging

from django.core import signing
from django.utils import timezone

logger = logging.getLogger(__name__)

SESSION_KEY = "form_friction_attempts"
# Namespace for the token the page posts back when somebody leaves a form unsaved. It is signed so
# the endpoint's vocabulary is exactly "forms this server actually rendered for this person": the
# beacon is unauthenticated by necessity (it fires as the page goes away), and without this it
# would accept any form name anybody cared to invent.
ABANDON_SALT = "auctions.form_friction.abandon"
ABANDON_TOKEN_MAX_AGE = 60 * 60 * 12
# Field names posted with an abandonment. A form has 43 fields; anything past this is not a person.
MAX_ABANDONED_FIELDS = 60
# One abandonment row per form per session. Somebody who opens the settings page five times and
# closes it five times is one story, not five, and this endpoint takes no authentication.
ABANDON_SESSION_KEY = "form_friction_abandoned"
# Past this, the run is not telling us anything new -- somebody is holding the enter key, or a
# script is. Rows stop being written; the counter stops climbing.
MAX_ATTEMPTS_RECORDED = 10


def error_codes(form) -> dict[str, list[str]]:
    """``{field name: [error code, ...]}`` for a rejected form.

    Codes rather than messages: a code is what groups. ``as_data()`` is the only place Django keeps
    them -- ``form.errors`` has already rendered the messages, and a message carries whatever the
    validator interpolated into it, which can be the value somebody typed.
    """
    codes: dict[str, list[str]] = {}
    try:
        for field_name, errors in form.errors.as_data().items():
            codes[field_name] = sorted({(error.code or "invalid") for error in errors})
    except Exception:
        logger.exception("could not read error codes from %s", type(form).__name__)
    return codes


def _who(request):
    """``(user, session_id)``: whichever of the two identifies this person.

    A signed-out visitor is joined up by session key, the same way ``PageView`` does it. The
    session is only forced into existence if it already has one -- a rejected form is not a reason
    to start writing a session row for a crawler.
    """
    user = getattr(request, "user", None)
    user = user if (user is not None and user.is_authenticated) else None
    session = getattr(request, "session", None)
    session_id = getattr(session, "session_key", "") or "" if session is not None else ""
    return user, session_id[:100]


def abandon_token(form_name: str) -> str:
    """The signed name of a form, for the page to hand back if it is left unsaved."""
    return signing.dumps({"form": (form_name or "")[:100]}, salt=ABANDON_SALT)


def read_abandon_token(token: str) -> str:
    """The form name inside a token, or "" if it is missing, forged or stale."""
    try:
        return (signing.loads(token, salt=ABANDON_SALT, max_age=ABANDON_TOKEN_MAX_AGE) or {}).get("form", "")[:100]
    except Exception:
        return ""


class FormFrictionMixin:
    """Record rejected submissions, abandonments, and whether the person eventually got through."""

    def get_context_data(self, **kwargs):
        """Hand the page a signed form name, which is what lets it report being abandoned.

        base.html renders this into a marker element whenever it is present, so adding the mixin to
        a view is the whole of instrumenting it -- there is no template to remember to edit.
        """
        context = super().get_context_data(**kwargs)
        try:
            form = context.get("form")
            if form is not None:
                context["friction_token"] = abandon_token(self.friction_form_name(form))
        except Exception:
            logger.exception("could not build a friction token for %s", type(self).__name__)
        return context

    def form_invalid(self, form):
        try:
            self._record_form_failure(form)
        except Exception:
            logger.exception("form friction instrument failed on %s", type(form).__name__)
        return super().form_invalid(form)

    def form_valid(self, form):
        try:
            self._resolve_form_failures(form)
        except Exception:
            logger.exception("form friction instrument failed on %s", type(form).__name__)
        return super().form_valid(form)

    def friction_form_name(self, form) -> str:
        """What to file these rows under. Overridable for a view serving several forms."""
        return type(form).__name__[:100]

    def _record_form_failure(self, form):
        from auctions.friction_models import FormFailure

        request = self.request
        form_name = self.friction_form_name(form)
        session = getattr(request, "session", None)
        attempts = (session.get(SESSION_KEY) or {}) if session is not None else {}
        attempt = int(attempts.get(form_name, 0)) + 1
        if session is not None:
            attempts[form_name] = min(attempt, MAX_ATTEMPTS_RECORDED + 1)
            session[SESSION_KEY] = attempts
        if attempt > MAX_ATTEMPTS_RECORDED:
            return None
        user, session_id = _who(request)
        return FormFailure.objects.create(
            form_name=form_name,
            url=(request.path or "")[:600],
            user=user,
            session_id=session_id,
            field_errors=error_codes(form),
            attempt=attempt,
        )

    def _resolve_form_failures(self, form):
        from auctions.friction_models import FormFailure

        request = self.request
        form_name = self.friction_form_name(form)
        session = getattr(request, "session", None)
        attempts = (session.get(SESSION_KEY) or {}) if session is not None else {}
        if not attempts.get(form_name):
            # Got it right first time, which is the case this table has nothing to say about.
            return 0
        del attempts[form_name]
        session[SESSION_KEY] = attempts
        user, session_id = _who(request)
        rows = FormFailure.objects.filter(form_name=form_name, resolved=False)
        rows = rows.filter(user=user) if user is not None else rows.filter(session_id=session_id, user__isnull=True)
        return rows.update(resolved=True, resolved_at=timezone.now())
