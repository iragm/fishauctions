"""The command palette and the assistant surface behind it.

The palette's own views -- ask, execute, cancel, report -- plus ``/ai/``, which is the page that
lists a user's API keys and what is signed in through OAuth. The catalogue of what the palette can
actually do is in :mod:`auctions.palette_actions`, not here.
"""

import json
import logging
from datetime import timedelta
from urllib.parse import urlencode

from asgiref.sync import sync_to_async
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import (
    Count,
    Q,
    Sum,
)
from django.db.models.base import Model as Model
from django.http import (
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView, View

from auctions.models import (
    AssistantSkillRequest,
    CommandPaletteSearch,
    LLMUsage,
    UserAPIKey,
)

from .base import AdminOnlyViewMixin

logger = logging.getLogger(__name__)


class UserAPIKeyView(LoginRequiredMixin, TemplateView):
    """How to connect an AI agent to this site, and the keys for doing it.

    Two ways in, and the page leads with the one most people want. **Signing in** is the whole
    story for Claude, for Grok's custom connectors and for ChatGPT's developer-mode apps: they run
    a real OAuth flow against this site and there is no key to copy anywhere. A **key** is for the things that can't do
    that — a script, a cron job, a connector an administrator adds for a whole organisation with
    a fixed header.

    Either way the credential can never do more than its owner can: the tools re-check the owner's
    real permissions on every call, and ``allow_writes`` (and the OAuth ``write`` scope) is a
    ceiling on top of that rather than a grant.

    A key's secret is shown **once**, on the redirect after creating it, and is never stored — only
    a salted hash of it is. Same shape as the club API key pages
    (:class:`ClubAPIKeyCreateView`), and for the same reason: a key you can go back and read is a
    key that is written down somewhere it can be read from.

    Open to **everyone signed in**. It used to be gated on ``UserData.use_llm_search``, the
    per-user flag that opens the natural-language command palette, on the reasoning that the two
    are one beta reached two ways. They are not the same feature: the palette spends this site's
    own language-model budget on every keystroke, which is what that flag is for, while an agent
    connecting over MCP brings its own model and costs this site nothing beyond the queries a web
    page would make. It can also do nothing its owner could not do by clicking, because the tools
    re-check the owner's real permissions on every call. See :mod:`auctions.mcp.auth`.

    Deliberately *not* gated on a language model being configured site-wide (``llm.assist_enabled``)
    either, for the same reason: this works perfectly well on an install with no API key of its own.
    """

    template_name = "user_api_keys.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "api_keys"
        context["keys"] = UserAPIKey.objects.filter(user=self.request.user).order_by("-created_at")
        context["new_raw_key"] = self.request.session.pop("new_user_api_key", None)
        context["mcp_url"] = self.request.build_absolute_uri(reverse("mcp"))
        context["connected_apps"] = self.connected_apps()
        return context

    def connected_apps(self):
        """Assistants this person has signed in to the site from, and whether they're still live.

        Signing in is the way almost everybody connects, and until this list existed the page
        described a connection it could not show and offered no way to end. "Revoke your key" is
        no help to somebody who never made one; the honest answer to "how do I disconnect Claude?"
        was to go into the Django admin, which no ordinary user can open.

        Grouped by application rather than by token because a token is an hour long and refreshes
        itself -- a list of tokens would be a list of the same connection over and over.
        """
        from auctions.mcp import auth as mcp_auth

        if not mcp_auth.oauth_enabled():
            return []
        from oauth2_provider.models import get_access_token_model

        rows = {}
        tokens = (
            get_access_token_model()
            .objects.filter(user=self.request.user)
            .select_related("application")
            .order_by("-created")
        )
        for token in tokens:
            application = token.application
            if application is None:
                continue
            row = rows.setdefault(
                application.pk,
                {
                    "pk": application.pk,
                    "name": application.name or "An AI agent",
                    "connected": token.created,
                    "live": False,
                    "writes": False,
                },
            )
            row["connected"] = max(row["connected"], token.created)
            if not token.is_expired():
                row["live"] = True
                row["writes"] = row["writes"] or mcp_auth.SCOPE_WRITE in (token.scope or "").split()
        return sorted(rows.values(), key=lambda row: row["connected"], reverse=True)

    def disconnect_app(self, request, application_pk):
        """End every session this person has with one application. Returns True if there was one.

        Deletes the refresh tokens as well as the access tokens, and the outstanding grants: an
        access token alone lives an hour, so revoking only those disconnects somebody for less time
        than it takes them to read this page.
        """
        from auctions.mcp import auth as mcp_auth

        if not mcp_auth.oauth_enabled():
            return False
        from oauth2_provider.models import get_access_token_model, get_grant_model, get_refresh_token_model

        try:
            application_pk = int(application_pk)
        except (TypeError, ValueError):
            return False
        removed = 0
        for model in (get_refresh_token_model(), get_access_token_model(), get_grant_model()):
            deleted, _ = model.objects.filter(user=request.user, application_id=application_pk).delete()
            removed += deleted
        return bool(removed)

    def post(self, request, *args, **kwargs):
        disconnect = request.POST.get("disconnect")
        if disconnect:
            if self.disconnect_app(request, disconnect):
                messages.info(request, "Disconnected. It will ask you to sign in again if you reconnect it.")
            else:
                messages.info(request, "That was already disconnected.")
            return redirect(reverse("user_api_keys"))
        revoke = request.POST.get("revoke", "").strip()
        if revoke:
            # Revoked rather than deleted: the row is what says a key existed and when it was last
            # used, which is exactly what somebody wants to see after revoking one in a hurry.
            # ``isdigit`` because a hand-written POST with a non-numeric pk raises rather than 404s.
            UserAPIKey.objects.filter(pk=revoke if revoke.isdigit() else 0, user=request.user).update(is_active=False)
            messages.info(request, "That key has been revoked and will stop working immediately.")
            return redirect(reverse("user_api_keys"))
        name = request.POST.get("name", "").strip()
        if not name:
            messages.error(request, "Give the key a name so you can tell it apart later.")
            return redirect(reverse("user_api_keys"))
        raw_key, prefix, key_hash = UserAPIKey.generate()
        # An expiry is the one control that limits the damage of a key nobody remembers issuing,
        # and the model has always had the column -- it just had no way in from the page, so every
        # key ever made was immortal. Blank still means "never", because a cron job that stops
        # working in ninety days with no warning is its own kind of failure.
        expiry_days = {"30": 30, "90": 90, "365": 365}.get(request.POST.get("expires_in", ""))
        expires_at = timezone.now() + timedelta(days=expiry_days) if expiry_days else None
        UserAPIKey.objects.create(
            user=request.user,
            name=name[:100],
            prefix=prefix,
            key_hash=key_hash,
            allow_writes=request.POST.get("allow_writes") == "on",
            expires_at=expires_at,
        )
        # Carried in the session rather than rendered straight away so a refresh of the page it
        # lands on doesn't put the secret back on screen.
        request.session["new_user_api_key"] = raw_key
        return redirect(reverse("user_api_keys"))


class CommandPaletteView(View):
    """JSON results for the command palette.

    GET ?q= returns search groups; an empty/absent query returns the default items.
    Login is required (applied in urls.py); the response is never cached.
    """

    def get(self, request, *args, **kwargs):
        from auctions import command_palette

        groups = command_palette.search(request, request.GET.get("q", ""))
        response = JsonResponse({"groups": groups})
        response["Cache-Control"] = "private, no-store"
        response["Pragma"] = "no-cache"
        return response


class CommandPaletteLogView(View):
    """Upsert the user's current command-palette search row.

    Accepts form-encoded POST data (works with both fetch and navigator.sendBeacon):
    id, search, result, result_type, result_url, result_object_id. Returns {"id": <pk>}.
    """

    def post(self, request, *args, **kwargs):
        from auctions import command_palette

        def _int(value):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        search_id = command_palette.log_search(
            request.user,
            search_id=_int(request.POST.get("id")),
            search=request.POST.get("search", ""),
            result=request.POST.get("result"),
            result_type=request.POST.get("result_type", ""),
            result_url=request.POST.get("result_url", ""),
            result_object_id=_int(request.POST.get("result_object_id")),
        )
        return JsonResponse({"id": search_id})


class CommandPaletteAssistBase(View):
    """Shared plumbing for the two natural-language endpoints: JSON body parsing and throttling.

    Both endpoints are login-only (applied in ``urls.py``, like the other palette routes) and both
    are throttled before any work happens, so a throttled request can never reach the model.
    """

    def load_json(self, request):
        """Parse the request body as JSON. Returns ``{}`` for anything unparseable."""
        try:
            data = json.loads((request.body or b"").decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def throttled_response(self, request):
        """A 429 with a message the palette renders, or ``None`` when the user is under the limit."""
        from auctions import palette_assist

        message = palette_assist.check_cooldown(request.user)
        if message:
            return JsonResponse({"kind": "error", "message": message}, status=429)
        return None


#: Returned by :func:`next_or_done` instead of raising ``StopIteration``, which cannot cross the
#: sync/async boundary (it is swallowed by the coroutine machinery and never reaches the caller).
STREAM_DONE = object()


def next_or_done(iterator):
    """One item from a sync iterator, or :data:`STREAM_DONE` when it's exhausted."""
    return next(iterator, STREAM_DONE)


class CommandPaletteAssistView(CommandPaletteAssistBase):
    """Turn a natural-language command palette query into results, a navigation, or an action.

    POST JSON: ``{"q": "...", "context": [...], "path": "/where/the/user/is/"}``.

    Streams **newline-delimited JSON**: zero or more ``{"kind": "progress"}`` objects while the
    assist loop works, then exactly one final response (results / navigate / countdown / clarify /
    done / error). One object per line, so the client can render each as it lands and doesn't need
    an incremental JSON parser.

    NDJSON over ``fetch`` rather than server-sent events because this is a POST with a body and a
    CSRF token; ``EventSource`` is GET-only. A client that can't read a stream still gets a valid
    body it can parse line by line at the end, so the streaming is an enhancement, not a
    requirement.

    The body has to be an **async** generator. Handed a sync one, Django's ASGI handler consumes
    the whole thing with ``sync_to_async(list)`` before writing a single byte
    (``django/http/response.py``, ``StreamingHttpResponse.__aiter__``), which silently turns this
    endpoint back into a slow plain-JSON one: no progress reaches the browser and the answer lands
    all at once twenty seconds later. ``assist_stream`` itself stays sync -- it is full of ORM
    calls -- so each event is pulled through ``sync_to_async``.

    Nothing that changes the database happens here -- confirm-tier actions come back as a countdown
    and are run by the execute endpoint.
    """

    def post(self, request, *args, **kwargs):
        from auctions import palette_assist

        throttled = self.throttled_response(request)
        if throttled:
            return throttled
        data = self.load_json(request)
        query = data.get("q", "")
        context = data.get("context")
        path = data.get("path", "")

        if not data.get("stream", True):
            return JsonResponse(palette_assist.assist(request, query, context, path))

        events = palette_assist.assist_stream(request, query, context, path)

        async def lines():
            while True:
                try:
                    event = await sync_to_async(next_or_done)(events)
                except Exception:
                    # A traceback must not reach the user as a half-written stream, and by this
                    # point the status line is long gone, so the only thing left is to end with a
                    # usable final object.
                    logger.exception("Command palette assist stream failed")
                    yield json.dumps({"kind": "error", "message": "Something went wrong working that out."}) + "\n"
                    return
                if event is STREAM_DONE:
                    return
                yield json.dumps(event, default=str) + "\n"

        response = StreamingHttpResponse(lines(), content_type="application/x-ndjson")
        response["Cache-Control"] = "private, no-store"
        # Without this nginx buffers the whole response and the streaming does nothing at all.
        response["X-Accel-Buffering"] = "no"
        return response


class CommandPaletteExecuteView(CommandPaletteAssistBase):
    """Run a confirm-tier palette action once the client's countdown has elapsed.

    POST JSON: ``{"action": "...", "params": {...}}``. The countdown is client-side UX only --
    this re-runs the action's own resolver, so permissions and validation are checked here
    independently of whatever the assist call decided a moment ago.
    """

    def post(self, request, *args, **kwargs):
        from auctions import palette_assist

        throttled = self.throttled_response(request)
        if throttled:
            return throttled
        data = self.load_json(request)
        response = palette_assist.execute(request, data.get("action", ""), data.get("params"), data.get("path", ""))
        return JsonResponse(response)


class CommandPaletteCancelView(View):
    """Record that the user cancelled a confirm-tier action's countdown.

    POST JSON (or a ``sendBeacon`` body): ``{"usage_id": <int>}``. Nothing happened and nothing is
    undone -- the action never ran -- so this only writes down that we got it wrong, which is the
    one thing an abandoned command otherwise leaves no trace of.
    """

    def post(self, request, *args, **kwargs):
        from auctions import palette_assist, palette_routes

        try:
            data = json.loads((request.body or b"").decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            data = {}
        data = data or {}
        # The page is resolved here for the same reason the execute endpoint resolves it: working
        # out which auction an action was about is how the trust window is keyed.
        request.palette_page = palette_routes.page_context_from_path(request.user, data.get("path") or "")
        recorded = palette_assist.mark_cancelled(
            request.user,
            data.get("usage_id"),
            request=request,
            action_name=str(data.get("action") or "")[:50],
            params=data.get("params"),
        )
        return JsonResponse({"recorded": recorded})


class CommandPaletteReportView(View):
    """Record that the user told us a palette command didn't work.

    POST JSON: ``{"usage_id": <int>}``. The twin of the cancel endpoint, for the other half of
    getting it wrong: cancel means "you understood me and picked the wrong thing", this means "you
    didn't understand me at all". Nothing is emailed — it flags the row so the analytics page can
    sort the failures somebody actually minded to the top.
    """

    def post(self, request, *args, **kwargs):
        from auctions import palette_assist

        try:
            data = json.loads((request.body or b"").decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            data = {}
        recorded = palette_assist.mark_reported(request.user, (data or {}).get("usage_id"))
        return JsonResponse({"recorded": recorded})


class AssistantSkillRequestsView(AdminOnlyViewMixin, TemplateView):
    """What agents tried to do here and could not, grouped by what they asked for.

    The sibling of :class:`CommandPaletteAnalyticsView`'s bounce list and of
    :class:`SpeciesGapsView`, and it exists for the same reason both of those do: the interesting
    thing about a catalogue of fifty tools is not the fifty, it is the repeated request for the
    fifty-first. Every tool on the MCP endpoint was added because somebody said out loud that it
    was missing, and until this page that saying-out-loud had to reach the site owner by accident.

    Grouped by skill name and ordered by how many **different people** asked, because five clubs
    asking for the same thing is the number that decides whether it gets built and five requests
    from one enthusiastic agent is not. The rows underneath each group are what makes it readable:
    the same missing tool is described differently by every caller, and the description is the part
    that says what to build.

    Everything in a request was written by a language model acting for a member of this site. It is
    displayed and never executed, and the template escapes it like any other user text.
    """

    template_name = "assistant_skill_requests.html"

    #: Enough to work through in a sitting. Anything asked for once is not yet a pattern.
    LIMIT = 200

    def post(self, request, *args, **kwargs):
        """Move one request between the four states. The only thing this page writes."""
        row = get_object_or_404(AssistantSkillRequest, pk=request.POST.get("pk"))
        status = request.POST.get("status", "")
        if status in dict(AssistantSkillRequest.STATUS_CHOICES):
            row.status = status
            row.notes = request.POST.get("notes", row.notes)[:2000]
            row.save(update_fields=["status", "notes", "updatedon"])
            messages.success(request, f"“{row.skill}” is now {row.get_status_display().lower()}.")
        return redirect(self.back_to(request))

    @staticmethod
    def back_to(request):
        """Where the form returns to: this page, on the tab it was posted from.

        Built out of ``reverse()`` and one validated word rather than out of ``HTTP_REFERER``.
        The referrer is a header the browser sends and anybody can set, so redirecting to it is an
        open redirect however superuser-only the page is — and it was never the better answer here
        anyway, because the only place this form has to return to is itself.
        """
        wanted = request.POST.get("filter", "")
        url = reverse("assistant_skill_requests")
        for status, _label in AssistantSkillRequest.STATUS_CHOICES:
            if status == wanted:
                # The value that goes into the URL is the model's own constant, not the string
                # that was posted. Comparing the two and then interpolating the *posted* one is
                # the same URL and a worse one: nothing that reads this can see that the check
                # happened, static analysis included, and the next person to add a status here
                # would have to notice that the guard is load-bearing.
                return f"{url}?{urlencode({'status': status})}"
        return url

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        wanted = self.request.GET.get("status", AssistantSkillRequest.STATUS_NEW)
        rows = AssistantSkillRequest.objects.select_related("user")
        if wanted in dict(AssistantSkillRequest.STATUS_CHOICES):
            rows = rows.filter(status=wanted)
        groups: dict[str, dict] = {}
        for row in rows[: self.LIMIT]:
            key = row.skill.strip().lower()
            group = groups.setdefault(key, {"skill": row.skill, "rows": [], "people": set()})
            group["rows"].append(row)
            group["people"].add(row.user_id)
        ordered = sorted(groups.values(), key=lambda group: (-len(group["people"]), -len(group["rows"])))
        for group in ordered:
            group["people_count"] = len(group["people"])
        context["groups"] = ordered
        context["status"] = wanted
        # (value, label, count) rather than the choices plus a dict: a Django template cannot look
        # a value up in a dict by a variable key, and the workaround is always a custom filter.
        context["statuses"] = [
            (value, label, AssistantSkillRequest.objects.filter(status=value).count())
            for value, label in AssistantSkillRequest.STATUS_CHOICES
        ]
        return context


class CommandPaletteAnalyticsView(AdminOnlyViewMixin, TemplateView):
    """Admin overview of what people search for in the command palette.

    Surfaces the most common searches and, especially, the top 'bounce' searches
    (queries that returned nothing) so we can add them as synonyms or new shortcuts.
    """

    template_name = "command_palette_analytics.html"

    def get_context_data(self, **kwargs):
        from auctions import palette_assist

        context = super().get_context_data(**kwargs)
        base = CommandPaletteSearch.objects.exclude(search="")

        def top(qs):
            return list(
                qs.values("search")
                .annotate(count=Count("id"), clicks=Count("id", filter=Q(result="clicked")))
                .order_by("-count")[:20]
            )

        context["top_searches"] = top(base)
        context["top_bounces"] = top(base.filter(result="bounce"))
        context["total_searches"] = base.count()
        context["total_bounces"] = base.filter(result="bounce").count()
        # Natural-language assist: what it's being used for and what it's costing.
        usage = LLMUsage.objects.all()
        totals = usage.aggregate(
            calls=Count("id"),
            prompt=Sum("prompt_tokens"),
            cached=Sum("cached_prompt_tokens"),
            completion=Sum("completion_tokens"),
            total=Sum("total_tokens"),
        )
        context["llm_calls"] = totals["calls"] or 0
        context["llm_prompt_tokens"] = totals["prompt"] or 0
        context["llm_cached_prompt_tokens"] = totals["cached"] or 0
        context["llm_completion_tokens"] = totals["completion"] or 0
        context["llm_total_tokens"] = totals["total"] or 0
        # The number that actually drives the bill. The system prompt is the same on every call, so
        # most of the prompt total is a cache hit charged at a fraction of the normal input rate --
        # reading the raw prompt total as the cost overstates it several times over.
        context["llm_uncached_prompt_tokens"] = context["llm_prompt_tokens"] - context["llm_cached_prompt_tokens"]
        context["llm_cached_percent"] = (
            round(100 * context["llm_cached_prompt_tokens"] / context["llm_prompt_tokens"])
            if context["llm_prompt_tokens"]
            else 0
        )
        # Rounds per request: the multiplier on everything above, and the thing worth tuning.
        context["llm_rounds_per_query"] = (
            round(context["llm_calls"] / usage.values("query").distinct().count(), 2)
            if usage.values("query").distinct().count()
            else 0
        )
        context["llm_failures"] = usage.filter(success=False).count()
        context["llm_by_action"] = list(
            usage.exclude(action="")
            .values("action")
            .annotate(count=Count("id"), tokens=Sum("total_tokens"))
            .order_by("-count")[:10]
        )
        # The queries the assistant couldn't answer, most repeated first. This is the closest thing
        # to a feature backlog the palette has: a phrase that keeps showing up here is somebody
        # asking, over and over, for a skill that doesn't exist yet.
        context["llm_gave_up"] = list(
            usage.filter(response_kind__in=palette_assist.FAILURE_KINDS)
            .exclude(query="")
            .values("query", "response_kind")
            .annotate(count=Count("id"), reports=Count("id", filter=Q(reported=True)))
            .order_by("-reports", "-count")[:15]
        )
        # The failures somebody minded enough to press a button about. Everything else on this page
        # is inferred from behaviour; this is the only list where a person deliberately said "that
        # didn't work", which makes it short, high-signal and the first thing worth reading.
        reported = usage.filter(reported=True).exclude(query="")
        context["llm_reported"] = reported.count()
        context["llm_reported_queries"] = list(
            reported.values("query", "action", "response_kind").annotate(count=Count("id")).order_by("-count")[:15]
        )
        # Commands the user stopped during the countdown. Everything above records the assistant
        # failing; this records it succeeding at the wrong thing, which nothing else catches -- the
        # action never ran, so there is no error and no history entry. A query that keeps showing up
        # here was understood confidently and understood wrongly.
        cancelled = usage.filter(cancelled=True)
        context["llm_cancelled"] = cancelled.count()
        context["llm_cancelled_percent"] = (
            round(100 * context["llm_cancelled"] / usage.filter(response_kind=palette_assist.KIND_COUNTDOWN).count())
            if usage.filter(response_kind=palette_assist.KIND_COUNTDOWN).exists()
            else 0
        )
        context["llm_cancelled_queries"] = list(
            cancelled.exclude(query="").values("query", "action").annotate(count=Count("id")).order_by("-count")[:15]
        )
        return context
