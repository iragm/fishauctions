"""Who a log line was written for: a short request id and user pk, or the Celery task, on every record.

Without it two errors a second apart can't be told apart as one request or two, and a WARNING from
deep in a model can't be tied to the page that caused it. :class:`RequestContextMiddleware` (outermost,
next to ``SlowRequestMiddleware``) and the Celery hooks in :func:`connect_celery` set a context
variable; :class:`ContextFilter` puts it on each record as ``%(context)s``, which ``settings.LOGGING``'s
verbose format prints as ``[r=1a2b3c4d u=42]`` or ``[t=endauctions]``, and nothing outside one.

``auctions.mcp.admin`` strips the token before grouping records, so it never splits one kind of error
into many. A pk, never a username or an email: the logs are read by agents that can write to a
public repository.
"""

from __future__ import annotations

import contextvars
import logging
import uuid

from asgiref.sync import iscoroutinefunction, markcoroutinefunction

_context: contextvars.ContextVar[str] = contextvars.ContextVar("log_context", default="")


def current() -> str:
    """The ``[...]`` token for the record being written now, or ``""``."""
    return _context.get()


class ContextFilter(logging.Filter):
    """Adds ``record.context``. Never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        token = _context.get()
        record.context = f"[{token}] " if token else ""
        return True


def _request_token(request) -> str:
    request_id = uuid.uuid4().hex[:8]
    request.log_request_id = request_id
    return f"r={request_id}"


def set_user(request) -> None:
    """Add the signed-in user's pk once authentication has run. Safe to call on any request."""
    user = getattr(request, "user", None)
    request_id = getattr(request, "log_request_id", "")
    if request_id and user is not None and getattr(user, "is_authenticated", False):
        _context.set(f"r={request_id} u={user.pk}")


class RequestContextMiddleware:
    """Gives every request a short id for its log lines, and the user's pk once it is known.

    Outermost, so a line SlowRequestMiddleware writes carries it too. The user is read lazily on the
    way out (``AuthenticationMiddleware`` has run by then) and by :func:`set_user`, which
    ``UserContextMiddleware`` calls straight after authentication.
    """

    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        if iscoroutinefunction(self.get_response):
            markcoroutinefunction(self)

    def __call__(self, request):
        if iscoroutinefunction(self):
            return self.__acall__(request)
        token = _context.set(_request_token(request))
        try:
            return self.get_response(request)
        finally:
            _context.reset(token)

    async def __acall__(self, request):
        token = _context.set(_request_token(request))
        try:
            return await self.get_response(request)
        finally:
            _context.reset(token)


class UserContextMiddleware:
    """Straight after ``AuthenticationMiddleware``: adds the user's pk to this request's log lines."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        set_user(request)
        return self.get_response(request)


def _task_started(sender=None, **kwargs) -> None:
    name = getattr(sender, "name", "") or ""
    _context.set(f"t={name.rsplit('.', 1)[-1]}" if name else "")


def _task_finished(sender=None, **kwargs) -> None:
    _context.set("")


def connect_celery() -> None:
    """Tag a worker's records with the task that wrote them. Called from ``AuctionsConfig.ready``."""
    from celery.signals import task_postrun, task_prerun

    task_prerun.connect(_task_started, weak=False, dispatch_uid="log_context_task_prerun")
    task_postrun.connect(_task_finished, weak=False, dispatch_uid="log_context_task_postrun")
