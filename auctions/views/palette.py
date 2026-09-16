"""The command palette's views (ask, execute, cancel, report) and ``/ai/``, the API keys and OAuth
connections page. The action catalogue is :mod:`auctions.palette_actions`.
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

    Signing in (OAuth) is how Claude, Grok and ChatGPT connect; keys are for scripts and fixed-header
    connectors. Either way tools re-check the owner's permissions on every call, and ``allow_writes``
    or the ``write`` scope is only a ceiling. See :mod:`auctions.mcp.auth`.

    A key's secret is shown once and only a salted hash is stored, like :class:`ClubAPIKeyCreateView`.
    Open to everyone signed in, with or without ``use_llm_search`` or a site LLM key.
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
        """Applications this person has signed in from, grouped by application rather than token."""
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
        """End every session this person has with one application: access tokens, refresh tokens and
        grants. Returns True if there was one.
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
            # Revoked, not deleted, so last use stays visible. ``isdigit``: a non-numeric pk would raise.
            UserAPIKey.objects.filter(pk=revoke if revoke.isdigit() else 0, user=request.user).update(is_active=False)
            messages.info(request, "That key has been revoked and will stop working immediately.")
            return redirect(reverse("user_api_keys"))
        name = request.POST.get("name", "").strip()
        if not name:
            messages.error(request, "Give the key a name so you can tell it apart later.")
            return redirect(reverse("user_api_keys"))
        raw_key, prefix, key_hash = UserAPIKey.generate()
        # Blank means never expires.
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
        # In the session, so refreshing doesn't show the secret again.
        request.session["new_user_api_key"] = raw_key
        return redirect(reverse("user_api_keys"))


class CommandPaletteView(View):
    """JSON results for the command palette: GET ?q=, or the default items with no query. Never cached."""

    def get(self, request, *args, **kwargs):
        from auctions import command_palette

        groups = command_palette.search(request, request.GET.get("q", ""))
        response = JsonResponse({"groups": groups})
        response["Cache-Control"] = "private, no-store"
        response["Pragma"] = "no-cache"
        return response


class CommandPaletteLogView(View):
    """Upsert the user's current palette search row from form-encoded POST (fetch or sendBeacon).

    Fields: id, search, result, result_type, result_url, result_object_id. Returns {"id": <pk>}.
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
    """Shared JSON parsing and throttling for the natural-language endpoints, throttled before any work."""

    def load_json(self, request):
        """Parse the request body as JSON. Returns ``{}`` for anything unparseable."""
        try:
            data = json.loads((request.body or b"").decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def throttled_response(self, request):
        """A 429 with a message, or ``None`` when under the limit."""
        from auctions import palette_assist

        message = palette_assist.check_cooldown(request.user)
        if message:
            return JsonResponse({"kind": "error", "message": message}, status=429)
        return None


#: Returned instead of raising ``StopIteration``, which can't cross the sync/async boundary.
STREAM_DONE = object()


def next_or_done(iterator):
    """One item from a sync iterator, or :data:`STREAM_DONE` when it's exhausted."""
    return next(iterator, STREAM_DONE)


class CommandPaletteAssistView(CommandPaletteAssistBase):
    """Turn a natural-language palette query into results, a navigation, or an action.

    POST JSON ``{"q": "...", "context": [...], "path": "..."}``. Streams NDJSON: progress objects,
    then one final response. NDJSON over fetch because this is a POST with a CSRF token.

    The body must be an async generator, or ASGI buffers the whole stream. ``assist_stream`` is sync,
    so each event goes through ``sync_to_async``. Nothing is written here; confirm-tier actions come
    back as a countdown for the execute endpoint.
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
                    # The status line is sent, so end with a usable final object.
                    logger.exception("Command palette assist stream failed")
                    yield json.dumps({"kind": "error", "message": "Something went wrong working that out."}) + "\n"
                    return
                if event is STREAM_DONE:
                    return
                yield json.dumps(event, default=str) + "\n"

        response = StreamingHttpResponse(lines(), content_type="application/x-ndjson")
        response["Cache-Control"] = "private, no-store"
        # Without this nginx buffers the whole response.
        response["X-Accel-Buffering"] = "no"
        return response


class CommandPaletteExecuteView(CommandPaletteAssistBase):
    """Run a confirm-tier action once the countdown has elapsed.

    POST JSON ``{"action": "...", "params": {...}}``. Re-runs the resolver, so permissions are checked
    again here.
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
    """Record that the user cancelled a confirm-tier countdown. POST JSON or sendBeacon ``{"usage_id": <int>}``."""

    def post(self, request, *args, **kwargs):
        from auctions import palette_assist, palette_routes

        try:
            data = json.loads((request.body or b"").decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            data = {}
        data = data or {}
        # The page's auction keys the trust window.
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
    """Record that the user reported a palette command didn't work. POST JSON ``{"usage_id": <int>}``."""

    def post(self, request, *args, **kwargs):
        from auctions import palette_assist

        try:
            data = json.loads((request.body or b"").decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            data = {}
        recorded = palette_assist.mark_reported(request.user, (data or {}).get("usage_id"))
        return JsonResponse({"recorded": recorded})


class AssistantSkillRequestsView(AdminOnlyViewMixin, TemplateView):
    """What agents asked for and couldn't do, grouped by skill name and ordered by distinct requesters.

    Request text was written by a language model; it's displayed escaped and never executed.
    """

    template_name = "assistant_skill_requests.html"

    #: Rows per page.
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
        """This page on the posted tab, built from ``reverse()``; never ``HTTP_REFERER`` (an open redirect)."""
        wanted = request.POST.get("filter", "")
        url = reverse("assistant_skill_requests")
        for status, _label in AssistantSkillRequest.STATUS_CHOICES:
            if status == wanted:
                # Interpolate the model constant, not the posted string.
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
        # Tuples, since templates can't index a dict by a variable key.
        context["statuses"] = [
            (value, label, AssistantSkillRequest.objects.filter(status=value).count())
            for value, label in AssistantSkillRequest.STATUS_CHOICES
        ]
        return context


class CommandPaletteAnalyticsView(AdminOnlyViewMixin, TemplateView):
    """Admin overview of palette searches, especially bounces, to add as synonyms or shortcuts."""

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
        # Cached prompt tokens bill at a fraction of the input rate.
        context["llm_uncached_prompt_tokens"] = context["llm_prompt_tokens"] - context["llm_cached_prompt_tokens"]
        context["llm_cached_percent"] = (
            round(100 * context["llm_cached_prompt_tokens"] / context["llm_prompt_tokens"])
            if context["llm_prompt_tokens"]
            else 0
        )
        # Rounds per request multiply everything above.
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
        # Queries the assistant couldn't answer, most repeated first.
        context["llm_gave_up"] = list(
            usage.filter(response_kind__in=palette_assist.FAILURE_KINDS)
            .exclude(query="")
            .values("query", "response_kind")
            .annotate(count=Count("id"), reports=Count("id", filter=Q(reported=True)))
            .order_by("-reports", "-count")[:15]
        )
        # Failures a user reported.
        reported = usage.filter(reported=True).exclude(query="")
        context["llm_reported"] = reported.count()
        context["llm_reported_queries"] = list(
            reported.values("query", "action", "response_kind").annotate(count=Count("id")).order_by("-count")[:15]
        )
        # Countdowns the user stopped: understood confidently and wrongly.
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
