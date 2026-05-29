"""
parser.py - Turn a raw STS2 history .save file into a clean Python dict.

Confirmed schema (from real save file, STS2 v0.99.1, schema_version 8):

Top-level keys:
  acts, ascension, build_id, game_mode, killed_by_encounter, killed_by_event,
  map_point_history, modifiers, platform_type, players, run_time,
  schema_version, seed, start_time, was_abandoned, win

players[] structure (one entry per player, supports co-op):
  character, deck[], id (Steam ID), max_potion_slot_count, potions[], relics[]

deck[] entry:
  id, floor_added_to_deck, current_upgrade_level (optional),
  enchantment: { id, amount } (optional)

relics[] entry:
  id, floor_added_to_deck, props (optional)

floor is DERIVED by summing map_point_history entries across all acts.
gold/hp are derived from the final entry in map_point_history player_stats.
"""

import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Top-level field extraction
# ---------------------------------------------------------------------------

def _derive_floor(data: dict) -> Optional[int]:
    """
    Count total map points traversed across all acts.
    map_point_history is a list of acts, each act is a list of map points.
    """
    history = data.get("map_point_history")
    if not history or not isinstance(history, list):
        return None
    return sum(len(act) for act in history if isinstance(act, list))


def _derive_final_stats_for_player(data: dict, steam_id: int) -> dict:
    """
    Walk map_point_history in reverse to find the last player_stats entry
    for a given Steam ID. This gives us final gold and final HP.

    We walk in reverse because the last map point they participated in
    contains their end-of-run state.
    """
    history = data.get("map_point_history", [])

    # Flatten all map points, reversed
    all_points = []
    for act in history:
        all_points.extend(act)
    all_points.reverse()

    for point in all_points:
        for stats in point.get("player_stats", []):
            if stats.get("player_id") == steam_id:
                return {
                    "final_gold":    stats.get("current_gold"),
                    "final_hp":      stats.get("current_hp"),
                    "final_max_hp":  stats.get("max_hp"),
                }

    return {}


# ---------------------------------------------------------------------------
# Per-player extraction
# ---------------------------------------------------------------------------

def _parse_card(raw: dict) -> dict:
    """
    Parse a single card entry from players[].deck[].

    STS2 card format:
      {
        "id": "CARD.SETUP_STRIKE",
        "floor_added_to_deck": 1,
        "current_upgrade_level": 1,       # optional, absent = 0
        "enchantment": {                   # optional
          "id": "ENCHANTMENT.SOWN",
          "amount": 1
        }
      }
    """
    enchantment = raw.get("enchantment") or {}
    return {
        "id":             raw.get("id", "UNKNOWN"),
        "upgrade_level":  raw.get("current_upgrade_level", 0),
        "floor_acquired": raw.get("floor_added_to_deck"),
        "enchantment_id": enchantment.get("id"),
        "enchantment_amt": enchantment.get("amount"),
    }


def _parse_relic(raw: dict) -> dict:
    """
    Parse a single relic entry from players[].relics[].

    STS2 relic format:
      {
        "id": "RELIC.BURNING_BLOOD",
        "floor_added_to_deck": 1,
        "props": { ... }   # optional, internal state (e.g. Nunchaku attack count)
      }
    """
    return {
        "id":             raw.get("id", "UNKNOWN"),
        "floor_acquired": raw.get("floor_added_to_deck"),
    }


def _parse_players(data: dict) -> list[dict]:
    """
    Extract all players from the run.
    Returns a list with one dict per player, preserving order (player_index).
    """
    players_raw = data.get("players", [])
    players = []

    for i, p in enumerate(players_raw):
        steam_id = p.get("id")

        # Get final gold/hp from the last map_point_history entry for this player
        final_stats = _derive_final_stats_for_player(data, steam_id)

        players.append({
            "player_index":  i,
            "steam_id":      str(steam_id) if steam_id else None,
            # Strip the "CHARACTER." prefix for cleaner storage
            "character":     _strip_prefix(p.get("character"), "CHARACTER."),
            "cards":         [_parse_card(c) for c in p.get("deck", [])],
            "relics":        [_parse_relic(r) for r in p.get("relics", [])],
            **final_stats,
        })

    return players


def _strip_prefix(value: Optional[str], prefix: str) -> Optional[str]:
    """Remove a known prefix from an ID string. 'CHARACTER.IRONCLAD' → 'IRONCLAD'"""
    if value and value.startswith(prefix):
        return value[len(prefix):]
    return value


# ---------------------------------------------------------------------------
# Main parse entry point
# ---------------------------------------------------------------------------

def parse_run_file(path: Path) -> Optional[tuple[dict, str]]:
    """
    Parse a single STS2 history .save file.

    Returns (parsed_dict, raw_json) or None on hard failure.
    The raw_json is stored separately so we can always re-parse later
    if the schema evolves.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        log.error("Could not read %s: %s", path, e)
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("Could not parse JSON in %s: %s", path.name, e)
        return None

    # ---- Derive run_id from filename or start_time ----------------------
    # Filenames are Unix timestamps (e.g. "1779917139") — use as-is.
    run_id = path.stem

    # ---- Top-level fields -----------------------------------------------
    parsed = {
        "run_id":               run_id,
        "file_path":            str(path),
        "ascension":            data.get("ascension"),
        "floor":                _derive_floor(data),
        "victory":              int(bool(data.get("win", False))),
        "seed":                 data.get("seed"),
        "run_time":             data.get("run_time"),
        "start_time":           data.get("start_time"),
        "was_abandoned":        int(bool(data.get("was_abandoned", False))),
        "killed_by_encounter":  _strip_prefix(data.get("killed_by_encounter"), "ENCOUNTER."),
        "killed_by_event":      _strip_prefix(data.get("killed_by_event"), "EVENT."),
        "game_mode":            data.get("game_mode"),
        "build_id":             data.get("build_id"),
        "schema_version":       data.get("schema_version"),
        "players":              _parse_players(data),
    }

    # Derive is_multiplayer from player count
    parsed["is_multiplayer"] = int(len(parsed["players"]) > 1)

    # Log any top-level keys we're not using — useful during early access
    # as the schema evolves
    known_keys = {
        "acts", "ascension", "build_id", "current_act_index", "events_seen",
        "extra_fields", "game_mode", "killed_by_encounter", "killed_by_event",
        "map_drawings", "map_point_history", "modifiers", "odds", "platform_type",
        "players", "pre_finished_room", "rng", "run_time", "save_time",
        "schema_version", "seed", "shared_relic_grab_bag", "start_time",
        "visited_map_coords", "was_abandoned", "win", "win_time",
    }
    unknown = set(data.keys()) - known_keys
    if unknown:
        log.debug("Run %s: new top-level keys (update parser if needed): %s",
                  run_id, sorted(unknown))

    return parsed, raw