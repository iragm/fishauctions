"""Proving to a plugin directory that this host is ours.

OpenAI's plugin submission portal issues one token per plugin and fetches it from
``/.well-known/openai-apps-challenge`` on the MCP host or a parent host of it. Its rules, not ours:
the body is **only** that token -- not JSON, not a list, not two tokens -- and the path answers
without a redirect and without a sign-in, the same three constraints the app-association files in
``auctions/app_links.py`` are written to.

Blank ``OPENAI_APPS_CHALLENGE_TOKEN`` is a 404 rather than an empty 200, so a deployment that was
never given a token can't verify as "this host claims a plugin whose token is nothing".
"""

from django.conf import settings
from django.http import Http404, HttpResponse


def openai_apps_challenge(request):
    """GET /.well-known/openai-apps-challenge -- domain verification for the plugin directory.

    Not cached: the portal re-checks on every submission and a token replaced between versions has
    to be the one served the same minute.
    """
    token = settings.OPENAI_APPS_CHALLENGE_TOKEN
    if not token:
        raise Http404
    response = HttpResponse(token, content_type="text/plain; charset=utf-8")
    response["Cache-Control"] = "no-store"
    return response
