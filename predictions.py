"""
predictions.py - Twitch Predictions API integration.

Lifecycle:
  create(boss_name)  → open a prediction when entering a boss room
  resolve(won)       → close it with the correct outcome
  cancel()           → discard it (new run started, app restart safety)

All methods are async and must run inside the bot's event loop.
Thread-safe wrappers are provided on STS2Bot.
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)

_HELIX = "https://api.twitch.tv/helix"
_TITLE_MAX   = 45
_OUTCOME_MAX = 25


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


class PredictionManager:

    def __init__(self, client_id: str, broadcaster_id: str, token: str):
        self._client_id      = client_id
        self._broadcaster_id = broadcaster_id
        self._token          = token

        self._prediction_id: Optional[str] = None
        self._win_id:         Optional[str] = None
        self._lose_id:        Optional[str] = None
        self._boss_name:      Optional[str] = None

    @property
    def active(self) -> bool:
        return self._prediction_id is not None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict:
        return {
            "Client-Id":    self._client_id,
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def _patch(self, payload: dict) -> bool:
        import aiohttp
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.patch(
                    f"{_HELIX}/predictions",
                    headers=self._headers(),
                    json=payload,
                ) as r:
                    if r.status == 200:
                        return True
                    log.warning("Prediction PATCH failed %s: %s", r.status, await r.text())
                    return False
        except Exception as exc:
            log.error("Prediction PATCH error: %s", exc)
            return False

    def _clear(self) -> None:
        self._prediction_id = None
        self._win_id        = None
        self._lose_id       = None
        self._boss_name     = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create(self, boss_name: str) -> bool:
        """Open a new prediction. Cancels any previously open one first."""
        import aiohttp

        if self._prediction_id:
            await self.cancel()

        title = _truncate(f"Do we beat {boss_name}?", _TITLE_MAX)
        payload = {
            "broadcaster_id":    self._broadcaster_id,
            "title":             title,
            "outcomes": [
                {"title": _truncate("We win! PogChamp",      _OUTCOME_MAX)},
                {"title": _truncate("We die here FeelsBadMan", _OUTCOME_MAX)},
            ],
            "prediction_window": 120,
        }

        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    f"{_HELIX}/predictions",
                    headers=self._headers(),
                    json=payload,
                ) as r:
                    if r.status == 200:
                        data     = (await r.json())["data"][0]
                        outcomes = data["outcomes"]
                        self._prediction_id = data["id"]
                        self._boss_name     = boss_name
                        self._win_id        = outcomes[0]["id"]
                        self._lose_id       = outcomes[1]["id"]
                        log.info("Prediction created: %s (id=%s)", boss_name, self._prediction_id)
                        return True
                    log.warning("Prediction create failed %s: %s", r.status, await r.text())
                    return False
        except Exception as exc:
            log.error("Prediction create error: %s", exc)
            return False

    async def resolve(self, won: bool) -> bool:
        """Resolve the active prediction. No-op if none is open."""
        if not self._prediction_id:
            return False

        ok = await self._patch({
            "broadcaster_id":    self._broadcaster_id,
            "id":                self._prediction_id,
            "status":            "RESOLVED",
            "winning_outcome_id": self._win_id if won else self._lose_id,
        })
        if ok:
            log.info("Prediction resolved (%s): %s", "WIN" if won else "LOSS", self._boss_name)
            self._clear()
        return ok

    async def cancel(self) -> bool:
        """Cancel the active prediction. No-op if none is open."""
        if not self._prediction_id:
            return False

        ok = await self._patch({
            "broadcaster_id": self._broadcaster_id,
            "id":             self._prediction_id,
            "status":         "CANCELED",
        })
        if ok:
            log.info("Prediction canceled: %s", self._boss_name)
            self._clear()
        return ok
