"""The view mixin that writes :class:`auctions.friction_models.FormFailure` rows.

Add ``FormFrictionMixin`` **first** in a form view's bases and it records every rejected submission
and marks the run resolved when the person gets through::

    class AuctionUpdate(FormFrictionMixin, LoginRequiredMixin, AuctionViewMixin, UpdateView):

First, because it overrides ``form_valid``/``form_invalid`` and calls ``super()``: behind Django's
own ``FormMixin`` it would never be reached. ``test_form_friction.FormFrictionWiringTests`` checks
the ordering on every view that uses it.

Three things it deliberately doesn't do: **swallow anything** (every write is inside ``try``, since
an instrument that can 500 a form is worse than none, and this runs on the error path); **store what
anybody typed** (field names and error codes only); and **count a fresh GET as an attempt** (the
counter is in the session and only ``form_invalid`` moves it).

That counter is what makes ``attempt`` mean "in a row": ``form_valid`` marks this person's open
failures on this form resolved and clears the key, so a run ends with a success or the session.
"""

from __future__ import annotations

import logging

from django.core import signing
from django.utils import timezone

logger = logging.getLogger(__name__)

SESSION_KEY = "form_friction_attempts"
# Namespace for the token the page posts back when somebody leaves a form unsaved. Signed, so the
# endpoint's vocabulary is exactly the forms this server rendered for this person: the beacon is
# unauthenticated by necessity and would otherwise accept any form name.
ABANDON_SALT = "auctions.form_friction.abandon"
ABANDON_TOKEN_MAX_AGE = 60 * 60 * 12
# Field names posted with an abandonment. A form has 43 fields; past this it is not a person.
MAX_ABANDONED_FIELDS = 60
# One abandonment row per form per session: somebody who opens a page five times and closes it five
# times is one story, and this endpoint takes no authentication.
ABANDON_SESSION_KEY = "form_friction_abandoned"
# Past this the run says nothing new -- somebody is holding the enter key, or a script is.
MAX_ATTEMPTS_RECORDED = 10


def error_codes(form) -> dict[str, list[str]]:
    """``{field name: [error code, ...]}`` for a rejected form.

    Codes rather than messages, because a code is what groups, and ``as_data()`` is the only place
    Django keeps them: a rendered message carries whatever the validator interpolated, which can be the
    value somebody typed.
    """
    codes: dict[str, list[str]] = {}
    try:
        for field_name, errors in form.errors.as_data().items():
            codes[field_name] = sorted({(error.code or "invalid") for error in errors})
    except Exception:
        logger.exception("could not read error codes from %s", type(form).__name__)
    return codes


def _who(request, create_session=False):
    """``(user, session_id)``: whichever identifies this person.

    A signed-out visitor is joined up by session key, as ``PageView`` does it, and a visitor on their
    first request has no key yet -- so recording a failure forces one. Without that, every anonymous
    first bounce is stored under ``session_id=""``, can never be resolved, and one anonymous person's
    success would resolve every other anonymous person's failures at once.
    """
    user = getattr(request, "user", None)
    user = user if (user is not None and user.is_authenticated) else None
    session = getattr(request, "session", None)
    if user is None and session is not None and create_session and not session.session_key:
        session.save()
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

        base.html renders it into a marker element whenever it is present, so adding the mixin is the whole
        of instrumenting a view.
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
        user, session_id = _who(request, create_session=True)
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
            # Got it right first time, which this table has nothing to say about.
            return 0
        del attempts[form_name]
        session[SESSION_KEY] = attempts
        user, session_id = _who(request)
        if user is None and not session_id:
            # Nothing identifies this person, so there is no run to close. Resolving on an empty
            # session id would mark every anonymous failure on the site resolved.
            return 0
        rows = FormFailure.objects.filter(form_name=form_name, resolved=False)
        rows = rows.filter(user=user) if user is not None else rows.filter(session_id=session_id, user__isnull=True)
        return rows.update(resolved=True, resolved_at=timezone.now())
