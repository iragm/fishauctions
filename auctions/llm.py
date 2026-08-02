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

# Hard cap on completion size. Actions are small JSON objects; anything bigger is a runaway.
DEFAULT_MAX_TOKENS = 800


class LLMError(Exception):
    """Any failure talking to the provider: transport, HTTP status, or unparseable output.

    Callers are expected to catch this and degrade gracefully -- the palette falls back to
    ordinary search rather than showing the user a traceback.
    """


@dataclass
class LLMResult:
    """A single completion: the parsed JSON object plus what it cost."""

    data: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMProvider:
    """Base class for chat providers that can be asked to return a JSON object."""

    name = "base"

    def __init__(self, model: str = "", api_key: str = "", base_url: str = "", timeout: float | None = None) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout

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
        data = self._post(payload)
        if data is None:
            payload.pop("max_completion_tokens")
            payload["max_tokens"] = max_tokens
            data = self._post(payload, retry_on_param_error=False)
        return self._parse(data)

    def _post(self, payload: dict[str, Any], retry_on_param_error: bool = True) -> dict[str, Any] | None:
        """POST the payload. Returns ``None`` (only when ``retry_on_param_error``) if the
        endpoint rejected an unsupported parameter, so the caller can retry with the older name."""
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(self._endpoint, headers=headers, json=payload)
        except httpx.HTTPError as error:
            msg = f"Could not reach the language model: {error}"
            raise LLMError(msg) from error
        if response.status_code == 400 and retry_on_param_error and "max_completion_tokens" in response.text:
            return None
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
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            msg = "Language model response was missing its content"
            raise LLMError(msg) from error
        try:
            data = json.loads(content or "{}")
        except ValueError as error:
            msg = "Language model did not return valid JSON"
            raise LLMError(msg) from error
        if not isinstance(data, dict):
            msg = "Language model returned JSON that was not an object"
            raise LLMError(msg)
        usage = body.get("usage") or {}
        return LLMResult(
            data=data,
            model=str(body.get("model") or self.model),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
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
    return provider_class(
        model=getattr(settings, "LLM_MODEL", "") or "gpt-5-nano",
        api_key=getattr(settings, "OPENAI_API_KEY", "") or "",
        base_url=getattr(settings, "LLM_BASE_URL", "") or "",
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
