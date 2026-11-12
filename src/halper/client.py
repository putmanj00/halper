"""Claude Messages API tool-use loop.

Sends a request, runs any requested tools, returns the results, and repeats until the
model stops asking for tools or the turn limit is reached. The system prompt and tool
definitions carry `cache_control` so repeated turns read them from the prompt cache.
The Anthropic client is injected, so tests can pass a fake.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anthropic

from .config import Settings, load_settings
from .guardrail import Decision, Verdict, evaluate

Handler = Callable[[dict[str, Any]], str]
Approver = Callable[[str, dict[str, Any], Verdict], bool]

RETRYABLE_STATUS = {429, 503, 529}  # rate limited, unavailable, overloaded
CACHE = {"type": "ephemeral"}


@dataclass
class Usage:
    """Token totals across a run, including prompt-cache writes and reads."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def add(self, u: Any) -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + (getattr(u, name, 0) or 0))

    @property
    def total_input_tokens(self) -> int:
        """All input tokens: uncached + cache writes + cache reads."""
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens


@dataclass
class RunResult:
    text: str
    stop_reason: str  # "end_turn", "max_turns", or the API's other stop reasons
    turns: int
    usage: Usage
    messages: list[dict[str, Any]] = field(default_factory=list)


def _retry_delay(exc: anthropic.APIError, attempt: int) -> float:
    response = getattr(exc, "response", None)  # only status errors carry a response
    header = response.headers.get("retry-after") if response is not None else None
    if header and header.replace(".", "", 1).isdigit():
        return min(float(header), 60.0)
    return min(2.0**attempt, 60.0) + random.uniform(0, 1)


class ToolLoop:
    def __init__(
        self,
        client: Any | None = None,
        settings: Settings | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings or load_settings()
        # The SDK also retries by default; disable it so retries happen in one place.
        self.client = client or anthropic.Anthropic(max_retries=0)
        self._sleep = sleep

    def _create(self, **request: Any) -> Any:
        for attempt in range(self.settings.max_retries + 1):
            try:
                return self.client.messages.create(**request)
            except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
                # Connection errors and timeouts are retried too: the SDK's own retries are off.
                status = getattr(exc, "status_code", None)
                retryable = status is None or status in RETRYABLE_STATUS
                if not retryable or attempt >= self.settings.max_retries:
                    raise
                self._sleep(_retry_delay(exc, attempt))
        raise AssertionError("unreachable")

    def _run_tool(
        self,
        block: Any,
        handlers: dict[str, Handler],
        guard: bool,
        approve: Approver | None,
    ) -> dict[str, Any]:
        def result(content: str, is_error: bool = False) -> dict[str, Any]:
            out: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": content,
            }
            if is_error:
                out["is_error"] = True
            return out

        if guard:
            verdict = evaluate(block.name, block.input)
            allowed = verdict.decision is Decision.ALLOW or (
                verdict.decision is Decision.ASK
                and approve is not None
                and approve(block.name, block.input, verdict)
            )
            if not allowed:
                return result(
                    f"Blocked by policy ({verdict.decision.value}): {verdict.reason}", True
                )
        handler = handlers.get(block.name)
        if handler is None:
            return result(f"Unknown tool: {block.name}", True)
        try:
            return result(str(handler(block.input)))
        except Exception as exc:  # report to the model instead of crashing the loop
            return result(f"{type(exc).__name__}: {exc}", True)

    def run(
        self,
        prompt: str,
        *,
        system: str,
        tools: list[dict[str, Any]] | None = None,
        handlers: dict[str, Handler] | None = None,
        guard: bool = True,
        approve: Approver | None = None,
        model: str | None = None,
    ) -> RunResult:
        """Run the loop. With `guard` on, every tool call passes through the guardrail first:
        allow runs, ask runs only if `approve` returns True, deny never runs."""
        handlers = handlers or {}
        request: dict[str, Any] = {
            "model": model or self.settings.model,
            "max_tokens": self.settings.max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": CACHE}],
        }
        if tools:
            # Marking the last tool caches the whole list; copy so callers' dicts are untouched.
            request["tools"] = [*tools[:-1], {**tools[-1], "cache_control": CACHE}]

        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        usage = Usage()
        response: Any = None
        for turn in range(1, self.settings.max_turns + 1):
            response = self._create(**request, messages=messages)
            usage.add(response.usage)
            if response.stop_reason != "tool_use":
                return RunResult(_text(response), response.stop_reason, turn, usage, messages)
            if turn == self.settings.max_turns:
                break
            messages.append({"role": "assistant", "content": response.content})
            results = [
                self._run_tool(b, handlers, guard, approve)
                for b in response.content
                if b.type == "tool_use"
            ]
            messages.append({"role": "user", "content": results})
        return RunResult(_text(response), "max_turns", self.settings.max_turns, usage, messages)


def _text(response: Any) -> str:
    return "".join(b.text for b in response.content if b.type == "text")
