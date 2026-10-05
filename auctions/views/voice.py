"""Voice on set lot winners: reading what was heard, opening the browser's OpenAI session, and the log.

The set-winners page owns the form and the microphone; these answer it. :mod:`auctions.voice_interpreter`
does the reading, and :mod:`auctions.voice` holds the grammar and the OpenAI session settings.
"""

import hashlib
import json
import logging

import httpx
from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.http import JsonResponse
from django.views.generic import TemplateView, View

from auctions import voice, voice_interpreter
from auctions.mobile.services import voice as voice_service
from auctions.models import VoiceCommandLog, VoiceGrammar
from auctions.views.base import AuctionViewMixin
from auctions.views.selling import queue_next_to_record

logger = logging.getLogger(__name__)

# Only the views: this module shares its name with auctions.voice, which it imports.
__all__ = ["VoiceCloudSessionView", "VoiceCommandLogView", "VoiceInterpretView"]

#: How much of a window is read, newest last. A window this long means nothing has closed for minutes.
MAX_SEGMENTS = 40
MAX_SEGMENT_CHARS = 1000

#: OpenAI sessions a person may open in an hour. The page reconnects when a session ends, so this is
#: what stops a page stuck in a reconnect loop from listening on the site's key all night.
CLOUD_SESSIONS_PER_HOUR = 30

#: Rows on the log page, newest first.
LOG_PAGE_ROWS = 300


class VoiceCommandLogView(LoginRequiredMixin, AuctionViewMixin, TemplateView):
    """What voice heard on set lot winners, what it filled in, and what people changed it to.

    GET is the log page. POST ``id`` with ``corrected_to`` records the operator changing a field voice
    filled; the row's id came back with the command from :class:`VoiceInterpretView`. Fire-and-forget:
    ``{"id": <pk or null>}``, never an error that interrupts a sale.
    """

    template_name = "auctions/voice_log.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        context["rows"] = VoiceCommandLog.objects.filter(auction=self.auction).select_related("user")[:LOG_PAGE_ROWS]
        return context

    def post(self, request, *args, **kwargs):
        log_id = request.POST.get("id")
        try:
            log_id = int(log_id) if log_id else None
        except (TypeError, ValueError):
            log_id = None
        if not log_id:
            return JsonResponse({"id": None})
        result_id = voice.log_command(
            request.user,
            self.auction,
            log_id=log_id,
            slot=request.POST.get("slot", ""),
            corrected_to=request.POST.get("corrected_to", ""),
        )
        return JsonResponse({"id": result_id})


class VoiceInterpretView(LoginRequiredMixin, AuctionViewMixin, View):
    """Read what the set-winners page heard as a sale (:func:`voice_interpreter.interpret`).

    POST JSON ``{"heard": [...], "lot", "winner", "price", "previous"}``: the transcripts since the lot
    on the block came up, what the form holds, and the sale voice recorded moments ago, if any. Answers
    the reading, with each command that changes the form logged and its row's ``log_id`` attached.
    """

    def post(self, request, *args, **kwargs):
        try:
            body = json.loads(request.body or b"{}")
        except (ValueError, UnicodeDecodeError):
            return JsonResponse({"error": "Expected JSON"}, status=400)
        if not isinstance(body, dict):
            return JsonResponse({"error": "Expected JSON"}, status=400)
        heard = [str(segment)[:MAX_SEGMENT_CHARS] for segment in body.get("heard") or [] if isinstance(segment, str)]
        form = {name: str(body.get(name) or "")[:40] for name in ("lot", "winner", "price")}
        previous = body.get("previous") if isinstance(body.get("previous"), dict) else None
        grammar = VoiceGrammar.load()
        if grammar and not grammar.enabled:
            return JsonResponse(voice_interpreter.Reading().as_dict())
        reading = voice_interpreter.interpret(
            heard[-MAX_SEGMENTS:],
            voice_interpreter.Vocabulary.from_payload(voice_service.build_vocabulary(self.auction)),
            voice_interpreter.Grammar.from_model(grammar),
            lot=form["lot"],
            previous=previous,
            next_lot=self.next_lot,
        )
        result = reading.as_dict()
        self.log(result, form, closed=reading.carry is not None)
        return JsonResponse(result)

    def next_lot(self, lot_number):
        """The lot number after ``lot_number`` in this auction's lot queue, or None."""
        lots = self.auction.lots_qs
        if self.auction.use_seller_dash_lot_numbering:
            lot = lots.filter(custom_lot_number=lot_number).first()
        else:
            lot = lots.filter(lot_number_int=int(lot_number)).first() if str(lot_number).isdigit() else None
        following = queue_next_to_record(self.auction, after_lot=lot) if lot else None
        return following.lot_number_display if following else None

    def log(self, result, form, closed):
        """A row for each field this reading changes and each sale it closes; one for each miss."""
        fields = {"lot": "lot", "bidder": "winner", "price": "price"}
        for command in result["commands"]:
            slot = command["slot"]
            if slot in fields:
                if command["value"] == form[fields[slot]]:
                    continue
                chosen = command["value"]
            elif slot == "sold":
                if not closed:
                    continue
                chosen = "save"
            else:
                chosen = {"unsold": "end_unsold"}.get(slot, slot)
            command["log_id"] = voice.log_command(
                self.request.user,
                self.auction,
                slot=slot,
                heard=command["heard"],
                chosen=chosen,
                confidence=command["confidence"],
            )
        for missed in result["missed"]:
            voice.log_unmatched(
                self.request.user,
                self.auction,
                heard=f"lot {missed['lot']}: {missed['heard']}",
                session_key=self.request.session.session_key or "",
            )


class VoiceCloudSessionView(LoginRequiredMixin, AuctionViewMixin, View):
    """A short-lived OpenAI key for the page to stream its microphone to, with the session settings.

    The site's own key never leaves the server; the one returned opens one transcription session.
    POST only. 404 when listening through OpenAI is off (``VoiceGrammar.cloud_model``), 429 past
    :data:`CLOUD_SESSIONS_PER_HOUR`, 502 when OpenAI can't be reached.
    """

    def post(self, request, *args, **kwargs):
        model = voice.cloud_model(VoiceGrammar.load())
        if not model:
            return JsonResponse({"error": "Listening in the browser is turned off"}, status=404)
        key = f"voice-cloud:{request.user.pk}"
        cache.add(key, 0, 3600)
        try:
            opened = cache.incr(key)
        except ValueError:
            opened = 1
        if opened > CLOUD_SESSIONS_PER_HOUR:
            return JsonResponse({"error": "Too many listening sessions this hour"}, status=429)
        session, update = voice.cloud_session(model, voice_service.build_vocabulary(self.auction))
        try:
            response = httpx.post(
                voice.CLOUD_SESSION_URL,
                json=session,
                headers={
                    "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                    # Lets OpenAI tell one abusive account from the whole site.
                    "OpenAI-Safety-Identifier": hashlib.sha256(f"user-{request.user.pk}".encode()).hexdigest(),
                },
                timeout=10,
            )
            response.raise_for_status()
            secret = response.json()["value"]
        except (httpx.HTTPError, ValueError, KeyError):
            logger.exception("OpenAI transcription session for %s", self.auction.slug)
            return JsonResponse({"error": "Couldn't reach OpenAI"}, status=502)
        return JsonResponse({"key": secret, "model": model, "commit": model == voice.CLOUD_LIVE, "update": update})
