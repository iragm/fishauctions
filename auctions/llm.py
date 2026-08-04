"""Provider abstraction for the command palette's natural-language assist.

One small seam so the model behind the palette can be swapped without touching any
calling code:

  * ``LLMProvider``    -- the interface: ``complete_json(system, messages)`` -> ``LLMResult``
  * ``OpenAIProvider`` -- chat-completions over ``httpx`` (already a dependency), JSON mode
  * ``get_provider()`` -- factory reading ``settings.LLM_PROVIDER`` / ``LLM_MODEL``

Everything here speaks JSON in both directions: the caller supplies a system prompt and a
list of ``{"role", "content"}`` messages, and gets a parsed ``dict`` back plus token usage
for :class:`auctions.models.LLMUsage`. Nothing in this module knows about auctions -- the
prompt building and the (untrusted!) validation of whatever the model returns live in
``palette_assist.py``.

Adding a provider: subclass ``LLMProvider``, implement ``complete_json`` and
``is_configured``, and add it to ``_PROVIDERS``. Set ``LLM_PROVIDER`` in ``.env`` to pick it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

# Per-request wall clock for a single provider call. The assist loop can make several of
# these, and it has its own overall budget, so keep this tight enough that one slow call
# doesn't eat the whole budget.
DEFAULT_TIMEOUT_SECONDS = 10.0

# Hard cap on completion size. Actions are small JSON objects, but on a reasoning model this
# budget covers the hidden reasoning tokens too, and running out of it produces an *empty*
# reply rather than a short one. Only tokens actually generated are billed, so headroom here
# is free and running short is not.
DEFAULT_MAX_TOKENS = 2000

# How hard a reasoning model should think before answering. This is a one-line command with a
# person waiting on it, and the whole job is picking one entry out of a list that is already in
# the prompt -- measured on gpt-5-nano, "minimal" answers correctly in ~1s where the default
# spends 6-8s and several hundred tokens reasoning its way to the same JSON. Set
# LLM_REASONING_EFFORT to "low"/"medium"/"high" to trade that latency back for accuracy, or to
# "" to leave the parameter off entirely.
DEFAULT_REASONING_EFFORT = "minimal"

# Payload keys that not every model generation or OpenAI-compatible server understands. We send
# the modern shape and step back a key at a time when the endpoint says it doesn't know one, so
# LLM_MODEL and LLM_BASE_URL stay free to point at something older.
OPTIONAL_PARAMETERS = ("max_completion_tokens", "reasoning_effort")


class LLMError(Exception):
    """Any failure talking to the provider: transport, HTTP status, or unparseable output.

    Callers are expected to catch this and degrade gracefully -- the palette falls back to
    ordinary search rather than showing the user a traceback.
    """


class UnsupportedParameter(Exception):
    """The endpoint rejected one of :data:`OPTIONAL_PARAMETERS`. Internal to this module."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


@dataclass
class LLMResult:
    """A single completion: the parsed JSON object plus what it cost."""

    data: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    prompt_tokens: int = 0
    #: The part of ``prompt_tokens`` the provider served from its own cache, billed at a fraction
    #: of the normal input rate. Worth recording because the palette's system prompt is a large,
    #: byte-identical prefix on every call, so this is most of it.
    cached_prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMProvider:
    """Base class for chat providers that can be asked to return a JSON object."""

    name = "base"

    def __init__(
        self,
        model: str = "",
        api_key: str = "",
        base_url: str = "",
        timeout: float | None = None,
        reasoning_effort: str = "",
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout
        self.reasoning_effort = reasoning_effort

    def is_configured(self) -> bool:
        """True when this provider has everything it needs to make a call.

        The palette treats a provider that isn't configured as "assist is turned off" and
        behaves exactly as it did before this feature existed.
        """
        return bool(self.api_key)

    def complete_json(
        self,
        system: str,
        messages: list[dict[str, str]],
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResult:
        """Send ``system`` + ``messages`` and return the parsed JSON object the model replied with.

        Raises :class:`LLMError` on any transport, status, or JSON-parsing failure.
        """
        msg = "complete_json must be implemented by a subclass"
        raise NotImplementedError(msg)


class OpenAIProvider(LLMProvider):
    """OpenAI (or any OpenAI-compatible endpoint) via the chat-completions REST API.

    Built directly on ``httpx`` rather than the ``openai`` SDK so no new dependency is needed.
    ``response_format={"type": "json_object"}`` makes the model return a single JSON object,
    which is still validated by the caller -- never trusted.

    Point ``LLM_BASE_URL`` at any compatible server (a local model, a proxy, OpenRouter, ...)
    to use this same class with something other than OpenAI.
    """

    name = "openai"
    default_base_url = "https://api.openai.com/v1"

    @property
    def _endpoint(self) -> str:
        return f"{(self.base_url or self.default_base_url).rstrip('/')}/chat/completions"

    def complete_json(
        self,
        system: str,
        messages: list[dict[str, str]],
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResult:
        if not self.is_configured():
            msg = "No API key configured for the OpenAI provider"
            raise LLMError(msg)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "response_format": {"type": "json_object"},
            # Newer models (gpt-5*) renamed this parameter; send the new name and fall back
            # to the old one if the endpoint rejects it, so both generations work unchanged.
            "max_completion_tokens": max_tokens,
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        # One attempt per optional parameter we might have to give up on, plus the real one.
        for _attempt in range(len(OPTIONAL_PARAMETERS) + 1):
            try:
                return self._parse(self._post(payload))
            except UnsupportedParameter as rejected:
                logger.info("%s does not accept %s; retrying without it", self.model, rejected.name)
                payload.pop(rejected.name, None)
                if rejected.name == "max_completion_tokens":
                    payload["max_tokens"] = max_tokens
        msg = "Language model rejected every form of the request"
        raise LLMError(msg)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST the payload and return the parsed response body.

        Raises :class:`UnsupportedParameter` when the endpoint rejected one of the optional keys,
        so the caller can drop it and try the older shape.
        """
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(self._endpoint, headers=headers, json=payload)
        except httpx.HTTPError as error:
            msg = f"Could not reach the language model: {error}"
            raise LLMError(msg) from error
        if response.status_code == 400:
            for name in OPTIONAL_PARAMETERS:
                if name in payload and name in response.text:
                    raise UnsupportedParameter(name)
        if response.status_code != 200:
            logger.warning("LLM provider returned %s: %s", response.status_code, response.text[:500])
            msg = f"Language model returned HTTP {response.status_code}"
            raise LLMError(msg)
        try:
            return response.json()
        except ValueError as error:
            msg = "Language model returned a non-JSON body"
            raise LLMError(msg) from error

    def _parse(self, body: dict[str, Any]) -> LLMResult:
        """Pull the JSON object and token usage out of a chat-completions response body."""
        try:
            choice = body["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            msg = "Language model response was missing its content"
            raise LLMError(msg) from error
        if not (content or "").strip():
            # A reasoning model that spends its entire completion budget thinking replies with an
            # empty string and finish_reason "length" -- a 200, with nothing in it. Parsing that as
            # `{}` made it look like the model had answered off-contract, so the assist loop
            # "corrected" it and asked again, burning another several seconds per round on the
            # identical empty answer. Fail here instead, with the reason.
            if choice.get("finish_reason") == "length":
                msg = "Language model used its whole completion budget before answering"
                raise LLMError(msg)
            msg = "Language model returned an empty reply"
            raise LLMError(msg)
        try:
            data = json.loads(content or "{}")
        except ValueError as error:
            msg = "Language model did not return valid JSON"
            raise LLMError(msg) from error
        if not isinstance(data, dict):
            msg = "Language model returned JSON that was not an object"
            raise LLMError(msg)
        usage = body.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        return LLMResult(
            data=data,
            model=str(body.get("model") or self.model),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            cached_prompt_tokens=int(details.get("cached_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )


_PROVIDERS: dict[str, type[LLMProvider]] = {
    OpenAIProvider.name: OpenAIProvider,
}

# Set by tests (see ``auctions/test_palette_assist.py``) to run the whole assist stack against a
# fake provider with no network access. Production code never touches this.
_provider_override: LLMProvider | None = None


def set_provider_override(provider: LLMProvider | None) -> None:
    """Install (or clear, with ``None``) a provider used by ``get_provider()``. For tests."""
    global _provider_override  # noqa: PLW0603
    _provider_override = provider


def get_provider() -> LLMProvider:
    """Build the configured provider from settings.

    Swapping models is a one-line ``.env`` change (``LLM_MODEL``); swapping vendors is
    ``LLM_PROVIDER`` plus a class in ``_PROVIDERS``. The returned provider may not be
    configured -- callers should check ``is_configured()`` (or use ``assist_enabled()``).
    """
    if _provider_override is not None:
        return _provider_override
    name = (getattr(settings, "LLM_PROVIDER", "") or OpenAIProvider.name).lower()
    provider_class = _PROVIDERS.get(name, OpenAIProvider)
    effort = getattr(settings, "LLM_REASONING_EFFORT", None)
    return provider_class(
        model=getattr(settings, "LLM_MODEL", "") or "gpt-5-nano",
        api_key=getattr(settings, "OPENAI_API_KEY", "") or "",
        base_url=getattr(settings, "LLM_BASE_URL", "") or "",
        # An unset setting takes the default; an explicitly empty one means "don't send it".
        reasoning_effort=DEFAULT_REASONING_EFFORT if effort is None else effort,
    )


def assist_enabled() -> bool:
    """True when natural-language assist should be offered at all.

    Without this, the palette must behave exactly as it did before the feature existed:
    no mic buttons, no assist calls, Enter falls through to ordinary search.
    """
    try:
        return get_provider().is_configured()
    except Exception:
        logger.exception("Could not build the LLM provider")
        return False
