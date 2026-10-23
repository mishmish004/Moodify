"""
How Moodify talks to the LLM: one layout for every call, one way to call, retry and log.

Layout (phase-plan.md, "Caching", layer 3). OpenAI caches the longest prompt prefix it has seen
before, for prompts of 1,024 tokens or more, so every call is assembled the same way:
  1. static: the system prompt (rules, schema, worked examples), identical for everyone
  2. per user: taste summary, known songs, later memory; changes at most daily
  3. per request: the prompt itself
Nothing per request may come before something per user, and nothing per user before the static part.
"""
import time
import unicodedata
from typing import Callable, Optional, Type, TypeVar

from openai import APIConnectionError, InternalServerError, OpenAI, OpenAIError
from pydantic import BaseModel

from src.core.config import get_settings
from src.core.logger import MoodifyLogger
from src.jobs import Trace

TIMEOUT = 120.0  # per attempt; the first real run (2026-10-23) timed out at 45 s on a long prompt
ATTEMPTS = 2  # a timeout, 5xx, dropped connection or unusable answer gets one more try; other 4xx don't

M = TypeVar("M", bound=BaseModel)


ZWJ = "\u200d"  # joins emoji like 👩‍💻; dropping it splits them


def clean_text(text: str, limit: int) -> str:
    """User-typed text for a prompt, capped at `limit`. Line breaks stay (lists and bullet points are
    common in detailed prompts); other control characters become spaces; invisible format characters
    (zero-width spaces, direction overrides) go, except the emoji joiner; spaces collapse per line."""
    lines = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        kept = []
        for ch in line:
            category = unicodedata.category(ch)
            if category == "Cc":
                kept.append(" ")
            elif not category.startswith("C") or ch == ZWJ:
                kept.append(ch)
        if line := " ".join("".join(kept).split()):
            lines.append(line)
    return "\n".join(lines)[:limit]


def layout(static: str, per_user: str, per_request: str) -> list[dict]:
    """The messages for one call, cache-friendly order (see the module docstring)."""
    user = f"{per_user}\n\n{per_request}" if per_user else per_request
    return [{"role": "system", "content": static}, {"role": "user", "content": user}]


def ask(
    call: str, model: str, messages: list[dict], schema: Type[M], trace: Trace, cache_key: str = "",
    usable: Callable[[M], bool] = lambda parsed: True, label: Optional[str] = None,
) -> Optional[M]:
    """One structured-output call with retries. Never raises: returns None when nothing usable came back.

    Each attempt is logged to the build's trace with its token counts. `usable` decides whether a
    parsed answer is good enough or worth one more try (e.g. an empty song list). `cache_key` keeps
    a user's calls of one kind on the same cache (OpenAI's prompt_cache_key).
    """
    log = MoodifyLogger.get_logger(call)
    label = label or call
    # max_retries=0: retries happen here, so each attempt is logged and traced
    client = OpenAI(api_key=get_settings().OPENAI_API_KEY.get_secret_value(), timeout=TIMEOUT, max_retries=0)
    extra = {"prompt_cache_key": cache_key} if cache_key else {}
    for attempt in range(1, ATTEMPTS + 1):
        started = time.monotonic()
        event = {"call": call, "model": model, "attempt": attempt}
        try:
            completion = client.chat.completions.parse(model=model, messages=messages, response_format=schema, **extra)
            parsed = completion.choices[0].message.parsed
        except (OpenAIError, ValueError, LookupError) as exc:
            # ValueError covers bad JSON and pydantic validation; LookupError an empty choices list.
            # Timeouts, dropped connections, 5xx and bad output may pass on a second try; 401/403/429 won't
            retry = isinstance(exc, (APIConnectionError, InternalServerError, ValueError, LookupError))
            log.error("%s call failed (attempt %d): %s: %s", label, attempt, type(exc).__name__, exc)
            trace.event("llm", **event, ok=False, error=type(exc).__name__, seconds=_since(started))
            if not retry:
                return None
            continue
        usage = _usage(completion)
        if parsed is None:
            log.error("model refused or returned nothing parsable for the %s call", label)
            trace.event("llm", **event, ok=False, error="refusal", seconds=_since(started), **usage)
            return None  # a refusal would only repeat itself
        ok = usable(parsed)
        trace.event("llm", **event, ok=ok, seconds=_since(started), **usage, **_size(parsed))
        if ok:
            return parsed
        log.error("%s call gave an unusable answer (attempt %d)", label, attempt)
    return None


def _since(started: float) -> float:
    return round(time.monotonic() - started, 2)


def _usage(completion) -> dict:
    """Token counts for the job log, cached ones included."""
    usage = getattr(completion, "usage", None)
    if usage is None:
        return {}
    details = getattr(usage, "prompt_tokens_details", None)
    return {
        "input_tokens": usage.prompt_tokens,
        "cached_tokens": getattr(details, "cached_tokens", None) or 0,
        "output_tokens": usage.completion_tokens,
    }


def _size(parsed: BaseModel) -> dict:
    """A one-number summary of the answer for the log: how many songs or sections came back."""
    for name in ("songs", "sections"):
        if isinstance(getattr(parsed, name, None), list):
            return {name: len(getattr(parsed, name))}
    return {}
