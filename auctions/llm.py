"""Provider abstraction for everything on this site that talks to a language model.

* ``LLMProvider``: the interface
* ``OpenAIProvider``: chat-completions over ``httpx``
* ``get_provider()``: factory reading ``settings.LLM_PROVIDER`` / ``LLM_MODEL``

``complete_json(system, messages)`` returns one JSON object, for data answers (species names,
donation emails, talk lists). ``complete(system, messages, tools)`` lets the model call a tool
from JSON Schemas built by :mod:`auctions.mcp.tools`, which the provider enforces; the palette uses
it. Both return an :class:`LLMResult` with token usage.

To add a provider, subclass ``LLMProvider``, implement ``complete_json``, ``complete`` and
``is_configured``, add it to ``_PROVIDERS``, and set ``LLM_PROVIDER``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

# Per call; the assist loop has its own overall budget.
DEFAULT_TIMEOUT_SECONDS = 10.0

# Includes a reasoning model's hidden tokens; running out gives an empty reply. Unused headroom is free.
DEFAULT_MAX_TOKENS = 2000

# "minimal" answers in ~1s on gpt-5-nano where the default takes 6-8s. LLM_REASONING_EFFORT sets
# low/medium/high, or "" to omit it.
DEFAULT_REASONING_EFFORT = "minimal"

# Keys older models or compatible servers may not know; dropped one at a time on rejection.
OPTIONAL_PARAMETERS = ("max_completion_tokens", "reasoning_effort")


class LLMError(Exception):
    """Any failure talking to the provider. Callers degrade gracefully."""


class UnsupportedParameter(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


@dataclass(frozen=True)
class ToolCall:
    """One tool call from the model, with parsed ``arguments``. Callers still validate before running it."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResult:
    """A completion and its cost: ``data`` from ``complete_json``, or ``text``/``tool_calls`` from ``complete``."""

    data: dict[str, Any] = field(default_factory=dict)
    #: A plain reply; empty whenever ``tool_calls`` isn't.
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    prompt_tokens: int = 0
    #: Prompt tokens served from the provider's cache, billed at a fraction.
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
        """True when this provider can make calls; otherwise assist is off."""
        return bool(self.api_key)

    def complete_json(
        self,
        system: str,
        messages: list[dict[str, str]],
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResult:
        """Send ``system`` + ``messages`` and return the parsed JSON reply. Raises :class:`LLMError`."""
        msg = "complete_json must be implemented by a subclass"
        raise NotImplementedError(msg)

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResult:
        """Send ``system`` + ``messages`` and let the model call one of ``tools`` or answer.

        ``tools`` are MCP-shaped descriptors. Assistant ``tool_calls`` and ``tool`` result turns are built
        with :func:`tool_call_message` and :func:`tool_result_message`. Raises :class:`LLMError`.
        """
        msg = "complete must be implemented by a subclass"
        raise NotImplementedError(msg)


class OpenAIProvider(LLMProvider):
    """OpenAI or any compatible endpoint (``LLM_BASE_URL``) via chat-completions, on ``httpx``."""

    name = "openai"
    default_base_url = "https://api.openai.com/v1"

    @property
    def _endpoint(self) -> str:
        return f"{(self.base_url or self.default_base_url).rstrip('/')}/chat/completions"

    def _payload(self, system: str, messages: list[dict[str, Any]], max_tokens: int) -> dict[str, Any]:
        """The half of the request body that is the same however we are asking."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            # gpt-5* renamed max_tokens; _send falls back if rejected.
            "max_completion_tokens": max_tokens,
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        return payload

    def _send(self, payload: dict[str, Any], max_tokens: int) -> dict[str, Any]:
        """POST the payload, dropping one optional parameter at a time if rejected."""
        if not self.is_configured():
            msg = "No API key configured for the OpenAI provider"
            raise LLMError(msg)
        for _attempt in range(len(OPTIONAL_PARAMETERS) + 1):
            try:
                return self._post(payload)
            except UnsupportedParameter as rejected:
                logger.info("%s does not accept %s; retrying without it", self.model, rejected.name)
                payload.pop(rejected.name, None)
                if rejected.name == "max_completion_tokens":
                    payload["max_tokens"] = max_tokens
        msg = "Language model rejected every form of the request"
        raise LLMError(msg)

    def complete_json(
        self,
        system: str,
        messages: list[dict[str, str]],
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResult:
        payload = self._payload(system, messages, max_tokens)
        payload["response_format"] = {"type": "json_object"}
        return self._parse(self._send(payload, max_tokens))

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResult:
        payload = self._payload(system, messages, max_tokens)
        if tools:
            payload["tools"] = [as_openai_tool(tool) for tool in tools]
            # "auto": answering in words is legitimate.
            payload["tool_choice"] = "auto"
        return self._parse_tools(self._send(payload, max_tokens))

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST and return the response body; raises :class:`UnsupportedParameter` for a rejected optional key."""
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
        """The JSON object and token usage from a chat-completions response."""
        try:
            choice = body["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            msg = "Language model response was missing its content"
            raise LLMError(msg) from error
        if not (content or "").strip():
            # A reasoning model that exhausts its budget returns an empty 200 with finish_reason
            # "length"; fail with the reason rather than parsing it as {}.
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
        return self._with_usage(body, data=data)

    def _parse_tools(self, body: dict[str, Any]) -> LLMResult:
        """The tool calls or plain reply from a chat-completions response."""
        try:
            choice = body["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as error:
            msg = "Language model response was missing its content"
            raise LLMError(msg) from error
        calls = []
        for raw in message.get("tool_calls") or []:
            function = (raw or {}).get("function") or {}
            name = function.get("name")
            if not name:
                continue
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except ValueError:
                # Keep the call with empty arguments; the resolver will ask for what's missing.
                logger.warning("Unparseable tool arguments from %s for %s", self.model, name)
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            calls.append(ToolCall(id=str(raw.get("id") or name), name=str(name), arguments=arguments))
        text = (message.get("content") or "").strip()
        if not calls and not text:
            # Same empty-reply trap as ``_parse``.
            if choice.get("finish_reason") == "length":
                msg = "Language model used its whole completion budget before answering"
                raise LLMError(msg)
            msg = "Language model returned an empty reply"
            raise LLMError(msg)
        return self._with_usage(body, text=text, tool_calls=calls)

    def _with_usage(self, body: dict[str, Any], **fields: Any) -> LLMResult:
        """Attach the model name and token counts to a result. Shared by both parsers."""
        usage = body.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        return LLMResult(
            model=str(body.get("model") or self.model),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            cached_prompt_tokens=int(details.get("cached_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            **fields,
        )


def as_openai_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """One MCP tool descriptor as an OpenAI function definition, so ``auctions/mcp/tools.py`` stays the single catalogue."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
        },
    }


def tool_call_message(calls: list[ToolCall]) -> dict[str, Any]:
    """The assistant turn recording tool calls; it must precede their results."""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments, default=str)},
            }
            for call in calls
        ],
    }


def tool_result_message(call: ToolCall, content: str) -> dict[str, Any]:
    """The turn carrying what one tool returned."""
    return {"role": "tool", "tool_call_id": call.id, "content": content}


_PROVIDERS: dict[str, type[LLMProvider]] = {
    OpenAIProvider.name: OpenAIProvider,
}

# Set by tests to use a fake provider.
_provider_override: LLMProvider | None = None


def set_provider_override(provider: LLMProvider | None) -> None:
    """Install or clear (``None``) a provider override for tests."""
    global _provider_override
    _provider_override = provider


def get_provider() -> LLMProvider:
    """Build the configured provider from settings. It may be unconfigured; check ``is_configured()``."""
    if _provider_override is not None:
        return _provider_override
    name = (getattr(settings, "LLM_PROVIDER", "") or OpenAIProvider.name).lower()
    provider_class = _PROVIDERS.get(name, OpenAIProvider)
    effort = getattr(settings, "LLM_REASONING_EFFORT", None)
    return provider_class(
        model=getattr(settings, "LLM_MODEL", "") or "gpt-5-nano",
        api_key=getattr(settings, "OPENAI_API_KEY", "") or "",
        base_url=getattr(settings, "LLM_BASE_URL", "") or "",
        # Unset means the default; empty means don't send it.
        reasoning_effort=DEFAULT_REASONING_EFFORT if effort is None else effort,
    )


def assist_enabled() -> bool:
    """True when natural-language assist is offered at all."""
    try:
        return get_provider().is_configured()
    except Exception:
        logger.exception("Could not build the LLM provider")
        return False
