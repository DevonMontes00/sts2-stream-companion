"""
ai_commentary.py - AI-generated Twitch commentary via the Anthropic API.

Design:
  - Each call runs in a ThreadPoolExecutor so it never blocks the watcher.
  - A per-call timeout controls how long we wait before falling back to the
    static message. If the API is slow or down, the stream never notices.
  - The system prompt is deliberately tight: one message, max 200 chars,
    specific tone, no filler. Vague prompts produce generic output.
  - A RunJournal tracks what happened this run and what the bot said, so
    each AI call has narrative context rather than treating events in isolation.

Threading model:
  Watchdog thread → triggers.py → generate() → submits to pool → waits timeout
  If response arrives in time  → return AI message
  If timeout / error           → return fallback string silently
  Journal reads happen inside the pool thread; writes happen on the watchdog
  thread. A lock guards all journal access.
"""

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from typing import Optional

import anthropic

log = logging.getLogger(__name__)

_client: Optional[anthropic.Anthropic] = None
_pool   = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai-commentary")

DEFAULT_TIMEOUT = 4.0
_JOURNAL_MAX_ENTRIES = 8  # how many past events to include in context


def _get_client() -> Optional[anthropic.Anthropic]:
    global _client
    if _client is None:
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            log.warning("ANTHROPIC_API_KEY not set — AI commentary disabled, using static fallback.")
            return None
        _client = anthropic.Anthropic(api_key=key)
    return _client


# ---------------------------------------------------------------------------
# Run journal — tracks narrative context across a single run
# ---------------------------------------------------------------------------

@dataclass
class _JournalEntry:
    event:   str  # short label, e.g. "relic: Burning Blood"
    comment: str  # what the bot actually said


class RunJournal:
    """
    Accumulates key events and bot comments for the current run.

    Thread-safe: all access is guarded by a lock because generate() reads the
    journal from a pool thread while triggers.py writes from the watchdog thread.
    """

    def __init__(self) -> None:
        self._lock:       threading.Lock         = threading.Lock()
        self._character:  Optional[str]          = None
        self._ascension:  Optional[int]          = None
        self._start_time: Optional[float]        = None
        self._entries:    list[_JournalEntry]    = []

    def reset(self, character: str, ascension: int) -> None:
        with self._lock:
            self._character  = character
            self._ascension  = ascension
            self._start_time = time.monotonic()
            self._entries    = []
        log.debug("RunJournal reset for %s A%s", character, ascension)

    def add(self, event: str, comment: str) -> None:
        with self._lock:
            self._entries.append(_JournalEntry(event, comment))
            if len(self._entries) > _JOURNAL_MAX_ENTRIES:
                self._entries = self._entries[-_JOURNAL_MAX_ENTRIES:]

    def get_context(self) -> str:
        """Return a compact, prompt-ready summary of the current run so far."""
        with self._lock:
            if not self._character:
                return ""

            elapsed_min = int((time.monotonic() - self._start_time) / 60) if self._start_time else 0
            header = f"Run so far ({self._character} A{self._ascension}, {elapsed_min}min in):"

            if not self._entries:
                return header

            lines = [header]
            for entry in self._entries:
                lines.append(f'- [{entry.event}] "{entry.comment}"')
            return "\n".join(lines)


_journal = RunJournal()


def reset_run(character: str, ascension: int) -> None:
    """Call this at the start of each new run to clear stale context."""
    _journal.reset(character, ascension)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a dry, self-aware Twitch chat bot for a Slay the Spire 2 streamer.

Rules:
- Respond with EXACTLY ONE message for Twitch chat.
- Maximum 200 characters.
- No hashtags. No asterisks. No markdown.
- Be specific about any stats or numbers provided — vague comments are worthless.
- Tone: knowledgeable, occasionally sarcastic, always brief. Think "friend who knows the game well and isn't afraid to call out bad decisions."
- If run context is provided, use it to stay consistent with your previous comments and avoid repeating yourself.
- Do NOT start with "I" or repeat the stat back verbatim.
- Output only the message text. Nothing else."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate(prompt: str, fallback: str, event_label: str = "", timeout: float = DEFAULT_TIMEOUT) -> str:
    """
    Generate an AI commentary message.

    Args:
        prompt:      The event context (what just happened + relevant stats).
        fallback:    Static message to return if AI is unavailable or slow.
        event_label: Short label recorded in the run journal, e.g. "relic: Burning Blood".
                     Pass an empty string to skip journal recording (e.g. run_end).
        timeout:     Seconds to wait before falling back.

    Returns:
        Either the AI-generated string or the fallback.
    """
    client = _get_client()
    if client is None:
        return fallback

    context    = _journal.get_context()
    full_prompt = f"{context}\n\n{prompt}" if context else prompt

    def _call() -> str:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=100,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": full_prompt}],
        )
        return response.content[0].text.strip()

    future = _pool.submit(_call)
    result = fallback
    try:
        result = future.result(timeout=timeout)
        log.debug("AI message: %s", result)
    except FuturesTimeout:
        log.warning("AI timed out after %.1fs — using fallback.", timeout)
        future.cancel()
    except Exception as e:
        log.warning("AI call failed (%s) — using fallback.", e)

    if event_label:
        _journal.add(event_label, result)

    return result