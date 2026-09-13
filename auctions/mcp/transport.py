"""The HTTP end of the MCP server: one view, at ``/mcp/``. Nothing here knows what a tool is.

Stateless: a POST gets one JSON body back, no SSE stream and no session id.

===============================  ===========================================================
``POST`` a JSON-RPC request      ``200 application/json``, one JSON-RPC response
``POST`` a notification/response ``202``, empty body
``GET``                          ``405`` — no server-initiated stream is offered
``DELETE``                       ``405`` — there are no sessions to terminate
``Origin`` present and foreign   ``403`` — the DNS-rebinding rule
unknown ``MCP-Protocol-Version`` ``400``
no or bad credential             ``401`` + ``WWW-Authenticate``, never a tool error
good credential, feature off     ``403`` and no challenge — see ``auth.Refusal``
over the rate limit              ``429`` with ``Retry-After``
===============================  ===========================================================

A 401 (not a tool error) is required so the client's OAuth flow can start from ``WWW-Authenticate``.
Authentication failures are answered here and never reach :mod:`protocol`.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit

from django.http import HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from . import auth, protocol, tools

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
    """The Model Context Protocol endpoint. ``csrf_exempt``: the credential is a bearer token, and
    session cookies are refused outright, so there is no ambient authority to forge."""

    http_method_names = ["post", "get", "delete", "options"]

    def dispatch(self, request, *args, **kwargs):
        forbidden = self.check_origin(request)
        if forbidden:
            return forbidden
        return super().dispatch(request, *args, **kwargs)

    def check_origin(self, request):
        """Reject a cross-origin browser request outright (DNS-rebinding protection)."""
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

    def forbidden(self, message):
        """A credential we recognised and won't act on: 403, not 401, and no ``WWW-Authenticate``."""
        return _rpc_error(protocol.INVALID_REQUEST, message, status=403)

    def get(self, request, *args, **kwargs):
        return HttpResponse(status=405)

    def delete(self, request, *args, **kwargs):
        return HttpResponse(status=405)

    def post(self, request, *args, **kwargs):
        version, wrong_version = self.check_protocol_version(request)
        if wrong_version:
            return wrong_version

        credential = auth.authenticate(request)
        if isinstance(credential, auth.Refusal):
            return self.forbidden(credential.message)
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

        if isinstance(message, list):
            return _rpc_error(protocol.INVALID_REQUEST, "Batched requests are not supported.", status=400)

        # Resolvers run as this credential's user; set only on this endpoint, not via middleware.
        request.user = credential.user
        request.mcp_credential = credential
        request.assistant_surface = credential.label  # what the auction history says did this

        caller = protocol.Caller(
            request=request,
            writes=credential.writes,
            protocol_version=version,
            areas=tools.parse_areas(request.GET.get("tools", "")),  # ``?tools=club`` narrows it
        )
        answer = protocol.handle(message, caller)
        if answer is None:
            return HttpResponse(status=202)
        return _json(answer)
