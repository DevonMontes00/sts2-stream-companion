"""
bot.py - Twitch IRC bot using twitchio.

The core design challenge: twitchio is async (runs in an asyncio event loop),
but our watchdog file callbacks are sync (run in a background thread).

Solution: asyncio.run_coroutine_threadsafe()
  This is the standard way to schedule async work from a sync thread.
  It takes a coroutine and an event loop, and returns a Future you can
  optionally wait on. We fire-and-forget here since message delivery
  doesn't need to block the file watcher.

Threading model:
  Thread 1 (main):    watchdog observers, file parsing, DB writes
  Thread 2 (bot):     asyncio event loop running twitchio
  Bridge:             bot.post(msg) → run_coroutine_threadsafe → channel.send()
"""

import asyncio
import logging
import threading
import time
from typing import Optional

import twitchio
from twitchio.ext import commands

import config

log = logging.getLogger(__name__)


class STS2Bot(commands.Bot):

    def __init__(self):
        super().__init__(
            token=config.TWITCH_TOKEN,
            nick=config.TWITCH_BOT_NICK,
            prefix="!",
            initial_channels=[config.TWITCH_CHANNEL],
        )
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._connected = threading.Event()  # signals when bot is ready to post
        self._last_post_time: float = 0

    # ------------------------------------------------------------------
    # twitchio lifecycle events
    # ------------------------------------------------------------------

    async def event_ready(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._connected.set()
        log.info("Bot connected as %s → #%s", self.nick, config.TWITCH_CHANNEL)

    async def event_error(self, error: Exception, data: str = None) -> None:
        log.error("Twitch bot error: %s", error)

    # ------------------------------------------------------------------
    # Public API — safe to call from any thread
    # ------------------------------------------------------------------

    def post(self, message: str) -> None:
        """
        Post a message to Twitch chat. Thread-safe.

        Blocks for up to 10s waiting for the bot to connect on first call.
        Subsequent calls return immediately (the loop is already running).
        Respects MESSAGE_COOLDOWN to avoid spamming chat.
        """
        if not self._connected.wait(timeout=10):
            log.warning("Bot not connected after 10s — dropping message: %s", message)
            return

        now = time.monotonic()
        elapsed = now - self._last_post_time
        if elapsed < config.MESSAGE_COOLDOWN:
            time.sleep(config.MESSAGE_COOLDOWN - elapsed)
        self._last_post_time = time.monotonic()

        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._send(message), self._loop
            )

    async def _send(self, message: str) -> None:
        """Inner async send — only called from the bot's own event loop."""
        channel = self.get_channel(config.TWITCH_CHANNEL)
        if channel:
            await channel.send(message)
            log.debug("Posted to #%s: %s", config.TWITCH_CHANNEL, message)
        else:
            log.warning("Channel %s not found — message dropped.", config.TWITCH_CHANNEL)


def start_bot_thread() -> STS2Bot:
    """
    Create a bot instance and run it in a dedicated daemon thread.

    Returns the bot immediately — it connects in the background.
    Use bot.post() once the connection is established (it will block
    briefly on the first call if the bot hasn't connected yet).

    Why a daemon thread?
      Daemon threads are killed automatically when the main thread exits,
      so the user doesn't need to handle bot cleanup separately.
    """
    bot = STS2Bot()

    def _run():
        # twitchio's run() creates and manages its own asyncio event loop
        bot.run()

    thread = threading.Thread(target=_run, name="twitch-bot", daemon=True)
    thread.start()
    log.info("Bot thread started — connecting to Twitch...")
    return bot