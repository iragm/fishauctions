"""The HTTP end of the MCP server: one view, at ``/mcp/``.

Everything here is a rule the transport specification attaches to a status code, and nothing here
knows what a tool is. That is the whole point of the split -- see :mod:`auctions.mcp`.

**Stateless.** The spec lets a server answer a POSTed request with a single
``Content-Type: application/json`` body instead of opening an SSE stream, and lets it refuse the
GET stream with a ``405``. Both are taken: there is nothing this server wants to say to a client
that the client didn't ask for, so a session id would be bookkeeping with nothing in it. The
consequence worth knowing is that a long tool call holds a request open, which is what the rate
limit and the resolvers' own bounded queries are for.

===============================  ===========================================================
``POST`` a JSON-RPC request      ``200 application/json``, one JSON-RPC response
``POST`` a notification/response ``202``, empty body
``GET``                          ``405`` — no server-initiated stream is offered
``DELETE``                       ``405`` — there are no sessions to terminate
``Origin`` present and foreign   ``403`` — the DNS-rebinding rule
unknown ``MCP-Protocol-Version`` ``400``
no or bad credential             ``401`` + ``WWW-Authenticate``, never a tool error
over the rate limit              ``429`` with ``Retry-After``
===============================  ===========================================================

The 401 matters more than it looks: a client that gets a *tool error* saying "please log in" has
no way to start an OAuth flow, because the flow begins with the ``WWW-Authenticate`` header on a
401. Authentication failures are answered at this layer and never reach :mod:`protocol`.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit

from django.http import HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from . import auth, protocol

logger = logging.getLogger(__name__)

#: Seconds a client is told to wait after a 429. One rate-limit window.
RETRY_AFTER_SECONDS = 3600


def _json(payload, status=200):
    response = JsonResponse(payload, status=status)
    response["Cache-Control"] = "private, no-store"
    return response


def _rpc_error(code, message, status=200):
    """A JSON-RPC error with no id, for a failure that happened before we had one."""
    return _json(protocol.error(None, code, message), status=status)


@method_decorator(csrf_exempt, name="dispatch")
class MCPEndpointView(View):
    """The Model Context Protocol endpoint.

    ``csrf_exempt`` because the credential is a bearer token in a header, which a cross-site form
    post cannot set. :func:`auctions.mcp.auth.authenticate` refuses session cookies outright, so
    there is no ambient authority here for a forged request to borrow.
    """

    http_method_names = ["post", "get", "delete", "options"]

    def dispatch(self, request, *args, **kwargs):
        forbidden = self.check_origin(request)
        if forbidden:
            return forbidden
        return super().dispatch(request, *args, **kwargs)

    def check_origin(self, request):
        """Reject a cross-origin browser request outright (DNS-rebinding protection).

        A real MCP client sends no ``Origin`` at all. One that does is a browser, and the only
        browser that has any business here is one on this site.
        """
        origin = request.META.get("HTTP_ORIGIN")
        if not origin:
            return None
        if urlsplit(origin).netloc == request.get_host():
            return None
        return _rpc_error(protocol.INVALID_REQUEST, "Cross-origin requests are not accepted here.", status=403)

    def check_protocol_version(self, request):
        """``400`` on a version we don't speak. An absent header means the oldest we support."""
        version = request.META.get("HTTP_MCP_PROTOCOL_VERSION")
        if version is None:
            return protocol.ASSUMED_PROTOCOL_VERSION, None
        if version not in protocol.SUPPORTED_PROTOCOL_VERSIONS:
            return None, _rpc_error(
                protocol.INVALID_REQUEST,
                f"This server does not speak MCP {version}. "
                f"Supported: {', '.join(protocol.SUPPORTED_PROTOCOL_VERSIONS)}.",
                status=400,
            )
        return version, None

    def unauthorized(self, request, message="Authentication is required."):
        response = _rpc_error(protocol.INVALID_REQUEST, message, status=401)
        response["WWW-Authenticate"] = auth.challenge(request)
        return response

    def get(self, request, *args, **kwargs):
        # The spec's own way of saying "I have nothing to push you".
        return HttpResponse(status=405)

    def delete(self, request, *args, **kwargs):
        # No sessions are issued, so there are none to terminate.
        return HttpResponse(status=405)

    def post(self, request, *args, **kwargs):
        version, wrong_version = self.check_protocol_version(request)
        if wrong_version:
            return wrong_version

        credential = auth.authenticate(request)
        if credential is None:
            return self.unauthorized(request)
        if not auth.within_rate_limit(credential):
            response = _rpc_error(protocol.INTERNAL_ERROR, "Too many requests. Try again later.", status=429)
            response["Retry-After"] = str(RETRY_AFTER_SECONDS)
            return response

        try:
            message = json.loads((request.body or b"").decode("utf-8") or "null")
        except (ValueError, UnicodeDecodeError):
            return _rpc_error(protocol.PARSE_ERROR, "Request body was not valid JSON.", status=400)

        # A batch is a JSON array. Removed from the spec in 2025-06-18 and never sent by Claude;
        # say so rather than half-implementing it.
        if isinstance(message, list):
            return _rpc_error(protocol.INVALID_REQUEST, "Batched requests are not supported.", status=400)

        # The resolvers run as this user. Setting it here rather than in a middleware keeps the
        # substitution local to this endpoint: nothing else on the site sees a request whose user
        # came from a bearer token.
        request.user = credential.user
        request.mcp_credential = credential

        caller = protocol.Caller(request=request, writes=credential.writes, protocol_version=version)
        answer = protocol.handle(message, caller)
        if answer is None:
            # A notification or a client response: accepted, nothing to say back.
            return HttpResponse(status=202)
        return _json(answer)
