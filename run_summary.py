"""
run_summary.py - Extract aggregate stats from a completed run's raw JSON.

We parse this on-demand from raw_json rather than storing it in the DB,
since it's only needed at run-end for the overlay. No schema changes needed.

All stats are derived from map_point_history, which has per-room player_stats
entries. For co-op we attribute stats to player 0 for personal metrics
(damage, gold, rest choices) and count rooms once for map metrics (elites, bosses).
"""

import json
from typing import Optional


def parse(raw_json: str) -> Optional[dict]:
    try:
        data = json.loads(raw_json)
    except Exception:
        return None

    history   = data.get("map_point_history", [])
    players   = data.get("players", [])
    is_multi  = len(players) > 1

    # ---- Map-level counters (count rooms once regardless of player count) ---
    elites_fought  = 0
    bosses_fought  = 0
    monsters_fought = 0

    # ---- Player 0 personal stats (or summed for co-op display) ----
    total_damage    = 0
    total_gold_earned = 0
    total_gold_spent  = 0
    rest_heals      = 0
    rest_smiths      = 0
    potions_used    = 0
    cards_skipped   = 0
    cards_picked    = 0
    lowest_hp       = None   # closest to death moment

    for act in history:
        for point in act:
            map_type  = point.get("map_point_type", "")
            rooms     = point.get("rooms", [])
            all_stats = point.get("player_stats", [])

            # Room type counters — count once per room
            for room in rooms:
                rt = room.get("room_type", "")
                if rt == "elite":
                    elites_fought += 1
                elif rt == "boss":
                    bosses_fought += 1
                elif rt == "monster":
                    monsters_fought += 1

            # Player stats — use player 0 for personal metrics
            p0 = all_stats[0] if all_stats else {}

            total_damage      += p0.get("damage_taken",  0)
            total_gold_earned += p0.get("gold_gained",   0)
            total_gold_spent  += p0.get("gold_spent",    0)
            potions_used      += len(p0.get("potion_used", []))

            # Rest site choices
            for choice in p0.get("rest_site_choices", []):
                if choice == "HEAL":
                    rest_heals += 1
                elif choice == "SMITH":
                    rest_smiths += 1

            # Card pick rate
            for choice in p0.get("card_choices", []):
                if choice.get("was_picked"):
                    cards_picked += 1
                else:
                    cards_skipped += 1

            # Track lowest HP (closest-to-death moment)
            hp = p0.get("current_hp")
            if hp is not None:
                if lowest_hp is None or hp < lowest_hp:
                    lowest_hp = hp

    # ---- Run time formatting ----
    run_time = data.get("run_time", 0)
    hours    = run_time // 3600
    minutes  = (run_time % 3600) // 60
    seconds  = run_time % 60
    if hours > 0:
        time_str = f"{hours}h {minutes}m"
    elif minutes > 0:
        time_str = f"{minutes}m {seconds}s"
    else:
        time_str = f"{seconds}s"

    # ---- Final deck / relic counts ----
    deck_size   = len(players[0].get("deck",   [])) if players else 0
    relic_count = len(players[0].get("relics", [])) if players else 0

    total_card_offers = cards_picked + cards_skipped
    pick_rate = (
        round(100.0 * cards_picked / total_card_offers, 0)
        if total_card_offers > 0 else None
    )

    return {
        # Map stats
        "elites_fought":    elites_fought,
        "bosses_fought":    bosses_fought,
        "monsters_fought":  monsters_fought,
        # Personal stats
        "total_damage":     total_damage,
        "total_gold_earned":total_gold_earned,
        "total_gold_spent": total_gold_spent,
        "rest_heals":       rest_heals,
        "rest_smiths":      rest_smiths,
        "potions_used":     potions_used,
        "pick_rate":        pick_rate,
        "lowest_hp":        lowest_hp,
        # Run info
        "deck_size":        deck_size,
        "relic_count":      relic_count,
        "run_time_str":     time_str,
        "is_multiplayer":   is_multi,
    }