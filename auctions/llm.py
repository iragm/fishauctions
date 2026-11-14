"""Provider abstraction for everything on this site that talks to a language model.

* ``LLMProvider``: the interface
* ``OpenAIProvider``: chat-completions over ``httpx``
* ``AnthropicProvider``: Claude through the ``anthropic`` SDK, built for Haiku
* ``get_provider()``: factory reading ``settings.LLM_PROVIDER`` / ``LLM_MODEL``
* ``get_embedding_provider()``: the one with embeddings, whichever answers the chat

Callers write messages in the chat-completions shape whatever the provider (``tool_call_message``,
``tool_result_message``, ``image_url`` parts); a provider with another wire format translates.

``complete_json(system, messages)`` returns one JSON object, for data answers (species names,
donation emails, talk lists). ``complete(system, messages, tools)`` lets the model call a tool
from JSON Schemas built by :mod:`auctions.mcp.tools`, which the provider enforces; the palette uses
it, and the library sends it pictures to read. Both return an :class:`LLMResult` with token usage.
``embed(texts, model)`` returns vectors for the library's search; a provider without it raises
:class:`LLMError` and the library falls back to keyword search.

To add a provider, subclass ``LLMProvider``, implement ``complete_json``, ``complete`` and
``is_configured`` (and ``embed`` if it has embeddings), name its key in ``api_key_setting`` and its
``default_model``, add it to ``_PROVIDERS``, and set ``LLM_PROVIDER``.
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
OPTIONAL_PARAMETERS = ("max_completion_tokens", "reasoning_effort", "tool_choice", "dimensions")


class LLMError(Exception):
    """Any failure talking to the provider. Callers degrade gracefully."""


class RateLimited(LLMError):
    """The provider is refusing for now, not failing. Its own ``Retry-After`` is on ``retry_after``.

    Worth its own class because the two want opposite handling: an outage should stop us calling for
    a while, and this should make everyone wait a little and then work.
    """

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


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


@dataclass
class EmbeddingResult:
    """One vector per input text, in order, and what they cost."""

    vectors: list[list[float]]
    model: str = ""
    prompt_tokens: int = 0


class LLMProvider:
    """Base class for chat providers that can be asked to return a JSON object."""

    name = "base"
    #: The setting holding this provider's key, and the model used when ``LLM_MODEL`` is unset.
    api_key_setting = ""
    default_model = ""

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
        tool_choice: str = "",
    ) -> LLMResult:
        """Send ``system`` + ``messages`` and let the model call one of ``tools`` or answer.

        ``tools`` are MCP-shaped descriptors. Assistant ``tool_calls`` and ``tool`` result turns are built
        with :func:`tool_call_message` and :func:`tool_result_message`. ``tool_choice`` of ``"required"``
        forbids a plain reply. Raises :class:`LLMError`.
        """
        msg = "complete must be implemented by a subclass"
        raise NotImplementedError(msg)

    def embed(self, texts: list[str], model: str, dimensions: int = 0) -> EmbeddingResult:
        """Vectors for ``texts``, shortened to ``dimensions`` where the model allows. Raises :class:`LLMError`."""
        msg = f"The {self.name} provider has no embeddings"
        raise LLMError(msg)


class OpenAIProvider(LLMProvider):
    """OpenAI or any compatible endpoint (``LLM_BASE_URL``) via chat-completions, on ``httpx``."""

    name = "openai"
    api_key_setting = "OPENAI_API_KEY"
    default_model = "gpt-5-nano"
    default_base_url = "https://api.openai.com/v1"

    def _endpoint(self, path: str = "chat/completions") -> str:
        return f"{(self.base_url or self.default_base_url).rstrip('/')}/{path}"

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

    def _send(self, payload: dict[str, Any], max_tokens: int, path: str = "chat/completions") -> dict[str, Any]:
        """POST the payload, dropping one optional parameter at a time if rejected."""
        if not self.is_configured():
            msg = "No API key configured for the OpenAI provider"
            raise LLMError(msg)
        for _attempt in range(len(OPTIONAL_PARAMETERS) + 1):
            try:
                return self._post(payload, path)
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
        tool_choice: str = "",
    ) -> LLMResult:
        payload = self._payload(system, messages, max_tokens)
        if tools:
            payload["tools"] = [as_openai_tool(tool) for tool in tools]
            # The palette sends "required": every outcome it can show the user is a tool, so a bare
            # paragraph is the one reply it cannot render. An endpoint that rejects the key falls
            # back to "auto" through ``OPTIONAL_PARAMETERS``.
            payload["tool_choice"] = tool_choice or "auto"
        return self._parse_tools(self._send(payload, max_tokens))

    def embed(self, texts: list[str], model: str, dimensions: int = 0) -> EmbeddingResult:
        payload: dict[str, Any] = {"model": model, "input": list(texts)}
        if dimensions:
            # Only the text-embedding-3 family takes it; ``_send`` drops it for one that doesn't.
            payload["dimensions"] = dimensions
        body = self._send(payload, 0, path="embeddings")
        try:
            rows = sorted(body["data"], key=lambda row: row["index"])
            vectors = [list(row["embedding"]) for row in rows]
        except (KeyError, TypeError) as error:
            msg = "Embedding response was missing its vectors"
            raise LLMError(msg) from error
        if len(vectors) != len(texts):
            msg = f"Asked for {len(texts)} embeddings and got {len(vectors)}"
            raise LLMError(msg)
        usage = body.get("usage") or {}
        return EmbeddingResult(
            vectors=vectors, model=str(body.get("model") or model), prompt_tokens=int(usage.get("prompt_tokens") or 0)
        )

    def _post(self, payload: dict[str, Any], path: str = "chat/completions") -> dict[str, Any]:
        """POST and return the response body; raises :class:`UnsupportedParameter` for a rejected optional key."""
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(self._endpoint(path), headers=headers, json=payload)
        except httpx.HTTPError as error:
            msg = f"Could not reach the language model: {error}"
            raise LLMError(msg) from error
        if response.status_code == 400:
            for name in OPTIONAL_PARAMETERS:
                if name in payload and name in response.text:
                    raise UnsupportedParameter(name)
        if response.status_code == 429:
            # Their own number if they sent one; OpenAI's is often well under a second.
            try:
                retry_after = float(response.headers.get("retry-after", "") or 0)
            except ValueError:
                retry_after = 0.0
            logger.info("LLM provider is rate limiting us: %s", response.text[:200])
            msg = "Language model is rate limiting this site"
            raise RateLimited(msg, retry_after=retry_after)
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


#: What ``LLM_REASONING_EFFORT`` may say to Claude. OpenAI's "minimal" has no namesake there.
ANTHROPIC_EFFORTS = ("low", "medium", "high", "xhigh", "max")

MIN_ANTHROPIC_MAX_TOKENS = 4096

_JSON_ONLY = "\n\nReply with one JSON object and nothing else: no prose around it, no code fence."


class AnthropicProvider(LLMProvider):
    """Claude through the official ``anthropic`` SDK. Built for Haiku, the model in the palette's price range.

    Three things differ from chat-completions, all handled here so no caller knows which provider it has:

    * **Messages.** Tool calls are ``tool_use`` blocks in an assistant turn and their results are
      ``tool_result`` blocks in a user turn; a picture is a base64 ``image`` block. :meth:`_messages`
      translates the chat-completions shape every caller writes.
    * **Thinking.** Claude thinks before it answers and a tool-use turn has to go back with its thinking
      blocks exactly as sent, which the shared ``tool_call_message`` knows nothing about. The provider
      keeps each turn's own blocks by tool-call id (:attr:`_turns`) and replays them. One provider
      object lives as long as one request's loop, which is as long as they are needed.
    * **JSON.** There is no ``json_object`` mode without a schema, and no prefill, so ``complete_json``
      asks in the system prompt and reads the object out of the reply.

    The system prompt carries the cache breakpoint: tools render before it, so one breakpoint covers
    the tool list and the page catalog, which are the same bytes for everybody in a permission tier.
    """

    name = "anthropic"
    api_key_setting = "ANTHROPIC_API_KEY"
    default_model = "claude-haiku-5-5"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._turns: dict[str, list[Any]] = {}
        #: An ``httpx2`` client for the SDK to use instead of its own. Tests answer through it.
        self.http_client: Any = None

    def _client(self):
        import anthropic

        if not self.is_configured():
            msg = "No API key configured for the Anthropic provider"
            raise LLMError(msg)
        options: dict[str, Any] = {"api_key": self.api_key, "timeout": self.timeout, "max_retries": 0}
        if self.base_url:
            options["base_url"] = self.base_url
        if self.http_client is not None:
            options["http_client"] = self.http_client
        # No retries of the SDK's own: the palette has one clock for the whole request and waits out
        # a rate limit itself, with the wait on screen.
        return anthropic.Anthropic(**options)

    def _effort(self) -> str:
        effort = (self.reasoning_effort or "").lower()
        if effort in ANTHROPIC_EFFORTS:
            return effort
        # "minimal" (the default, tuned for gpt-5-nano) and anything unknown: as little as there is.
        return "low" if effort else ""

    @staticmethod
    def _part(part: Any) -> dict[str, Any] | None:
        """One chat-completions content part as a Claude content block."""
        if isinstance(part, str):
            return {"type": "text", "text": part} if part else None
        if not isinstance(part, dict):
            return None
        if part.get("type") == "text":
            return {"type": "text", "text": part["text"]} if part.get("text") else None
        if part.get("type") == "image_url":
            url = str((part.get("image_url") or {}).get("url") or "")
            if url.startswith("data:") and ";base64," in url:
                media_type, data = url[len("data:") :].split(";base64,", 1)
                return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
            return {"type": "image", "source": {"type": "url", "url": url}} if url else None
        return part

    def _blocks(self, content: Any) -> list[dict[str, Any]]:
        parts = content if isinstance(content, list) else [content]
        return [block for block in (self._part(part) for part in parts) if block]

    def _messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """``messages`` in Claude's shape. Consecutive tool results share one user turn, as required."""
        built: list[dict[str, Any]] = []

        def add(role: str, blocks: list[Any]) -> None:
            if not blocks:
                return
            if built and built[-1]["role"] == role and role == "user":
                built[-1]["content"].extend(blocks)
            else:
                built.append({"role": role, "content": list(blocks)})

        for message in messages:
            role = message.get("role")
            if role == "tool":
                add(
                    "user",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.get("tool_call_id"),
                            "content": str(message.get("content") or ""),
                        }
                    ],
                )
            elif role == "assistant" and message.get("tool_calls"):
                add("assistant", self._tool_turn(message["tool_calls"]))
            elif role == "assistant":
                add("assistant", self._blocks(message.get("content")))
            else:
                add("user", self._blocks(message.get("content")))
        return built

    def _tool_turn(self, calls: list[dict[str, Any]]) -> list[Any]:
        """An assistant turn of tool calls: the model's own blocks when this provider saw them made.

        The palette answers only the first of several calls, and every ``tool_use`` sent needs a
        result, so calls the caller left out are left out here too.
        """
        wanted = [str(call.get("id")) for call in calls]
        kept = self._turns.get(wanted[0]) if wanted else None
        if kept:
            return [block for block in kept if getattr(block, "type", "") != "tool_use" or block.id in wanted]
        turn = []
        for call in calls:
            function = call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except ValueError:
                arguments = {}
            turn.append(
                {
                    "type": "tool_use",
                    "id": str(call.get("id")),
                    "name": function.get("name"),
                    "input": arguments if isinstance(arguments, dict) else {},
                }
            )
        return turn

    def _create(self, system: str, messages: list[dict[str, Any]], max_tokens: int, **extra: Any):
        import anthropic

        request: dict[str, Any] = {
            "model": self.model,
            # A ceiling, not a cost, and it has to hold the thinking as well as the answer: the
            # callers' numbers were sized for a model that was told not to think.
            "max_tokens": max(max_tokens, MIN_ANTHROPIC_MAX_TOKENS),
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": self._messages(messages),
            **extra,
        }
        effort = self._effort()
        if effort:
            request["output_config"] = {"effort": effort}
        client = self._client()
        try:
            try:
                response = client.messages.create(**request)
            except anthropic.BadRequestError as error:
                # Claude's larger models refuse a forced tool call. Asking again without forcing one
                # leaves the palette's own "that reply was not a tool" round to deal with any prose.
                if "tool_choice" not in request or "tool_choice" not in str(error):
                    raise
                logger.info("%s does not accept a forced tool call; retrying without it", self.model)
                request["tool_choice"] = {"type": "auto"}
                response = client.messages.create(**request)
        except anthropic.RateLimitError as error:
            try:
                retry_after = float(error.response.headers.get("retry-after", "") or 0)
            except ValueError:
                retry_after = 0.0
            logger.info("Anthropic is rate limiting us: %s", str(error)[:200])
            msg = "Language model is rate limiting this site"
            raise RateLimited(msg, retry_after=retry_after) from error
        except anthropic.APIStatusError as error:
            logger.warning("Anthropic returned %s: %s", error.status_code, str(error)[:500])
            msg = f"Language model returned HTTP {error.status_code}"
            raise LLMError(msg) from error
        except anthropic.APIConnectionError as error:
            msg = f"Could not reach the language model: {error}"
            raise LLMError(msg) from error
        if response.stop_reason == "refusal":
            msg = "Language model declined to answer"
            raise LLMError(msg)
        return response

    def _result(self, response, **fields: Any) -> LLMResult:
        usage = response.usage
        read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        written = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        return LLMResult(
            model=str(response.model or self.model),
            # ``input_tokens`` is only what was neither read from the cache nor written to it.
            prompt_tokens=int(usage.input_tokens or 0) + read + written,
            cached_prompt_tokens=read,
            completion_tokens=int(usage.output_tokens or 0),
            **fields,
        )

    @staticmethod
    def _text(response) -> str:
        return "".join(block.text for block in response.content if block.type == "text").strip()

    def _empty(self, response) -> LLMError:
        if response.stop_reason == "max_tokens":
            return LLMError("Language model used its whole completion budget before answering")
        return LLMError("Language model returned an empty reply")

    def complete_json(
        self,
        system: str,
        messages: list[dict[str, str]],
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResult:
        response = self._create(system + _JSON_ONLY, messages, max_tokens)
        text = self._text(response)
        if not text:
            raise self._empty(response)
        # Told not to, it still fences now and then; the object is what is between the outer braces.
        start, end = text.find("{"), text.rfind("}")
        try:
            data = json.loads(text[start : end + 1]) if start != -1 and end > start else None
        except ValueError as error:
            msg = "Language model did not return valid JSON"
            raise LLMError(msg) from error
        if not isinstance(data, dict):
            msg = "Language model returned JSON that was not an object"
            raise LLMError(msg)
        return self._result(response, data=data)

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        tool_choice: str = "",
    ) -> LLMResult:
        extra: dict[str, Any] = {}
        if tools:
            extra["tools"] = [as_anthropic_tool(tool) for tool in tools]
            # "any" is "required": it must call one. Haiku then skips thinking, which is the speed
            # the palette's single call wants.
            # One call at a time when one is required: the palette runs only the first.
            extra["tool_choice"] = (
                {"type": "any", "disable_parallel_tool_use": True} if tool_choice == "required" else {"type": "auto"}
            )
        response = self._create(system, messages, max_tokens, **extra)
        calls = [
            ToolCall(
                id=str(block.id), name=str(block.name), arguments=block.input if isinstance(block.input, dict) else {}
            )
            for block in response.content
            if block.type == "tool_use"
        ]
        text = self._text(response)
        if not calls and not text:
            raise self._empty(response)
        if calls:
            self._turns[calls[0].id] = list(response.content)
        return self._result(response, text=text, tool_calls=calls)


def as_anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """One MCP tool descriptor as a Claude tool definition: the same three fields under other names."""
    return {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "input_schema": tool.get("inputSchema") or {"type": "object", "properties": {}},
    }


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
    AnthropicProvider.name: AnthropicProvider,
}

# Set by tests to use a fake provider.
_provider_override: LLMProvider | None = None


def set_provider_override(provider: LLMProvider | None) -> None:
    """Install or clear (``None``) a provider override for tests."""
    global _provider_override
    _provider_override = provider


def get_provider(model: str = "", timeout: float | None = None) -> LLMProvider:
    """Build the configured provider from settings. It may be unconfigured; check ``is_configured()``.

    ``model`` and ``timeout`` override ``LLM_MODEL`` and the per-call default for one caller (the
    library's vision reads are slower than a palette round). The test override ignores both.
    """
    if _provider_override is not None:
        return _provider_override
    name = (getattr(settings, "LLM_PROVIDER", "") or OpenAIProvider.name).lower()
    return _build(_PROVIDERS.get(name, OpenAIProvider), model, timeout)


def _build(provider_class: type[LLMProvider], model: str = "", timeout: float | None = None) -> LLMProvider:
    effort = getattr(settings, "LLM_REASONING_EFFORT", None)
    return provider_class(
        model=model or getattr(settings, "LLM_MODEL", "") or provider_class.default_model,
        api_key=getattr(settings, provider_class.api_key_setting, "") or "",
        base_url=getattr(settings, "LLM_BASE_URL", "") or "",
        timeout=timeout,
        # Unset means the default; empty means don't send it.
        reasoning_effort=DEFAULT_REASONING_EFFORT if effort is None else effort,
    )


def get_embedding_provider(timeout: float | None = None) -> LLMProvider:
    """The provider the library's vectors come from: the configured one, or OpenAI when that one has
    no embeddings (Claude) and there is an OpenAI key. Changing where vectors come from is a re-index,
    so switching the chat model must not do it by accident.
    """
    provider = get_provider(timeout=timeout)
    if _provider_override is None and type(provider).embed is LLMProvider.embed:
        fallback = _build(OpenAIProvider, timeout=timeout)
        if fallback.is_configured():
            return fallback
    return provider


def assist_enabled() -> bool:
    """True when natural-language assist is offered at all."""
    try:
        return get_provider().is_configured()
    except Exception:
        logger.exception("Could not build the LLM provider")
        return False
