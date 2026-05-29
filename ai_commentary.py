"""
ai_commentary.py - AI-generated Twitch commentary via the Anthropic API.

Design:
  - Each call runs in a ThreadPoolExecutor so it never blocks the watcher.
  - A per-call timeout controls how long we wait before falling back to the
    static message. If the API is slow or down, the stream never notices.
  - The system prompt is deliberately tight: one message, max 200 chars,
    specific tone, no filler. Vague prompts produce generic output.

Threading model:
  Watchdog thread → triggers.py → generate() → submits to pool → waits timeout
  If response arrives in time  → return AI message
  If timeout / error           → return fallback string silently
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Optional

import anthropic

log = logging.getLogger(__name__)

# One persistent client, one small thread pool.
# Two workers is enough — we never fire more than one AI call at a time,
# but a second worker handles the rare case of overlapping events.
_client: Optional[anthropic.Anthropic] = None
_pool   = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai-commentary")

# How long to wait for the API before using the fallback (seconds).
# Anthropic responses are usually under 2s; 4s gives comfortable headroom.
DEFAULT_TIMEOUT = 4.0


def _get_client() -> Optional[anthropic.Anthropic]:
    """Lazy-init the client. Returns None if no API key is configured."""
    global _client
    if _client is None:
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            return None
        _client = anthropic.Anthropic(api_key=key)
    return _client


# ---------------------------------------------------------------------------
# System prompt — loaded once, shared across all calls.
# Keeping it focused produces much tighter output than a long prompt.
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a dry, self-aware Twitch chat bot for a Slay the Spire 2 streamer.

Rules:
- Respond with EXACTLY ONE message for Twitch chat.
- Maximum 200 characters.
- No hashtags. No asterisks. No markdown.
- Be specific about any stats or numbers provided — vague comments are worthless.
- Tone: knowledgeable, occasionally sarcastic, always brief. Think "friend who knows the game well and isn't afraid to call out bad decisions."
- Do NOT start with "I" or repeat the stat back verbatim.
- Output only the message text. Nothing else."""


def generate(prompt: str, fallback: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """
    Generate an AI commentary message.

    Args:
        prompt:   The user-facing context prompt (what just happened + stats).
        fallback: The static message to return if AI is unavailable or slow.
        timeout:  Seconds to wait before falling back.

    Returns:
        Either the AI-generated string or the fallback.
    """
    client = _get_client()
    if client is None:
        log.debug("No ANTHROPIC_API_KEY configured — using static fallback.")
        return fallback

    def _call() -> str:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=100,   # ~200 chars is well under 100 tokens
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()

    future = _pool.submit(_call)
    try:
        result = future.result(timeout=timeout)
        log.debug("AI message: %s", result)
        return result
    except FuturesTimeout:
        log.debug("AI timed out after %.1fs — using fallback.", timeout)
        future.cancel()
        return fallback
    except Exception as e:
        log.debug("AI call failed (%s) — using fallback.", e)
        return fallback