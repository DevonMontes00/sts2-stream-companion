"""
chat_commands.py - Viewer-facing Twitch chat commands.

Commands expose live run state (from ActiveRunWatcher) and historical
stats (from the database). Registered as a twitchio Cog so they stay
decoupled from the posting logic in bot.py.

Supported commands:
  !run      Current run status (character, floor, act, counts)
  !deck     Full card list for the active run
  !relics   Full relic list for the active run
  !stats    Win/loss record for the current character
  !wr       Win rate, optionally for a named character: !wr ironclad
"""

import logging
from typing import TYPE_CHECKING, Optional

import database
from twitchio.ext import commands as tw_commands

if TYPE_CHECKING:
    from active_run import ActiveRunWatcher

log = logging.getLogger(__name__)

_MAX_LEN = 490  # Twitch hard cap is 500; leave headroom for safety


def _fmt(raw_id: str) -> str:
    for prefix in ("RELIC.", "CARD.", "ENCOUNTER.", "CHARACTER.", "ENCHANTMENT."):
        if raw_id.startswith(prefix):
            raw_id = raw_id[len(prefix):]
            break
    return raw_id.replace("_", " ").title()


def _fit_list(items: list[str], prefix: str) -> str:
    """Append items to prefix until the Twitch length cap is reached."""
    result = prefix
    for i, item in enumerate(items):
        sep = ", " if i > 0 else ""
        chunk = sep + item
        if len(result) + len(chunk) + 14 > _MAX_LEN:
            result += f" ... +{len(items) - i} more"
            break
        result += chunk
    return result


class RunCommandsCog(tw_commands.Cog):

    def __init__(self, bot, active_watcher: "ActiveRunWatcher"):
        self._watcher = active_watcher

    @property
    def _state(self):
        return self._watcher.current_state

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @tw_commands.command(name="run")
    async def cmd_run(self, ctx: tw_commands.Context) -> None:
        state = self._state
        if state is None or state.character is None:
            await ctx.send("No active run detected.")
            return

        char        = _fmt(state.character)
        asc         = f"A{state.ascension}" if state.ascension else "A0"
        relic_count = sum(len(v) for v in state.relics.values())
        card_count  = sum(len(v) for v in state.cards.values())

        await ctx.send(
            f"Current run: {char} {asc} | "
            f"Floor {state.floor} (Act {state.act}) | "
            f"{relic_count} relics | {card_count} cards"
        )

    @tw_commands.command(name="deck")
    async def cmd_deck(self, ctx: tw_commands.Context) -> None:
        state = self._state
        if state is None or not state.cards:
            await ctx.send("No active run detected.")
            return

        cards = []
        for card_list in state.cards.values():
            for c in card_list:
                if c.upgrade_level == 1:
                    suffix = "+"
                elif c.upgrade_level > 1:
                    suffix = f"+{c.upgrade_level}"
                else:
                    suffix = ""
                cards.append(_fmt(c.card_id) + suffix)

        cards.sort()
        await ctx.send(_fit_list(cards, f"Deck ({len(cards)}): "))

    @tw_commands.command(name="relics")
    async def cmd_relics(self, ctx: tw_commands.Context) -> None:
        state = self._state
        if state is None or not state.relics:
            await ctx.send("No active run detected.")
            return

        relics = sorted(_fmt(r) for rset in state.relics.values() for r in rset)
        await ctx.send(_fit_list(relics, f"Relics ({len(relics)}): "))

    @tw_commands.command(name="stats")
    async def cmd_stats(self, ctx: tw_commands.Context) -> None:
        state = self._state
        await self._send_char_stats(ctx, state.character if state else None)

    @tw_commands.command(name="wr")
    async def cmd_wr(self, ctx: tw_commands.Context) -> None:
        parts = ctx.message.content.strip().split(maxsplit=1)
        if len(parts) > 1:
            character = parts[1].upper().replace(" ", "_")
        else:
            state = self._state
            character = state.character if state else None
        await self._send_char_stats(ctx, character)

    # ------------------------------------------------------------------

    async def _send_char_stats(self, ctx: tw_commands.Context, character: Optional[str]) -> None:
        if not character:
            await ctx.send("No active run — try: !wr <character>")
            return

        stats = database.character_stats(character)
        if not stats or stats["total_runs"] == 0:
            await ctx.send(f"No recorded runs for {_fmt(character)}.")
            return

        wins   = stats["wins"]
        total  = stats["total_runs"]
        losses = total - wins
        wr     = stats["winrate"]
        best   = stats.get("best_floor", "?")
        asc    = stats.get("highest_ascension", "?")

        await ctx.send(
            f"{_fmt(character)}: {wins}W {losses}L | {wr}% winrate | "
            f"Best floor: {best} | Highest A: {asc}"
        )
