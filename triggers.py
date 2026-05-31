"""
triggers.py - Generate Twitch chat messages and overlay events from STS2 game events.

High-impact triggers use AI commentary (run_start, relic, boss, run_end).
Lower-impact triggers (card, elite, act) use static messages to avoid
API call overhead on frequent events.

Each public function:
  1. Builds a static fallback message
  2. Builds a rich context prompt for the AI
  3. Calls ai_commentary.generate(prompt, fallback) — returns whichever arrives first
  4. Pushes a structured event to the overlay via event_bus
"""

import logging
import database
import ai_commentary
import run_summary as rs
from config import MIN_SAMPLE_SIZE

log = logging.getLogger(__name__)

_event_bus = None

def set_event_bus(bus) -> None:
    global _event_bus
    _event_bus = bus

def _push(event_type: str, **data) -> None:
    if _event_bus:
        _event_bus.push(event_type, data)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_id(raw_id: str) -> str:
    for prefix in ("RELIC.", "CARD.", "ENCOUNTER.", "CHARACTER.", "ENCHANTMENT."):
        if raw_id.startswith(prefix):
            raw_id = raw_id[len(prefix):]
            break
    return raw_id.replace("_", " ").title()


def _winrate_str(stats: dict) -> str:
    return f"{stats['winrate']}% ({stats['wins']}/{stats['total']} runs)"


# ---------------------------------------------------------------------------
# AI-powered triggers
# ---------------------------------------------------------------------------

def on_run_start(character: str, ascension: int) -> str:
    stats     = database.character_stats(character)
    char_name = _fmt_id(character)

    # Reset the run journal so this run starts with a clean slate
    ai_commentary.reset_run(char_name, ascension)

    # Build static fallback
    if not stats or stats["total_runs"] < MIN_SAMPLE_SIZE:
        fallback = f"First few runs with {char_name} — no real data yet. Let's see how this goes."
    else:
        winrate = stats["winrate"]
        runs    = stats["total_runs"]
        wins    = stats["wins"]
        again   = " again" if runs >= 5 else ""
        asc     = f" A{ascension}" if ascension > 0 else ""

        if winrate >= 60:
            comment = f"actually a solid pick at {winrate}% — don't blow it."
        elif winrate >= 45:
            comment = f"{winrate}% winrate. Respectable. Probably fine."
        elif winrate >= 30:
            comment = f"{winrate}% winrate... we've seen worse. Rarely."
        else:
            comment = f"{winrate}% winrate. Bold choice. Truly."

        fallback = f"He picked {char_name}{again}{asc} — {wins}/{runs} wins, {comment}"

    # Push overlay event
    _push("run_start",
          character=char_name,
          ascension=ascension,
          winrate=stats["winrate"] if stats else None,
          wins=stats["wins"] if stats else None,
          total=stats["total_runs"] if stats else None,
          comment=fallback)

    # Build AI prompt
    if stats and stats["total_runs"] >= MIN_SAMPLE_SIZE:
        prompt = (
            f"The streamer just started a new Slay the Spire 2 run.\n"
            f"Character: {char_name}\n"
            f"Ascension: {ascension}\n"
            f"Historical winrate with {char_name}: {stats['winrate']}% "
            f"({stats['wins']} wins / {stats['total_runs']} runs)\n"
            f"Best floor reached: {stats.get('best_floor', '?')}\n"
            f"Write one short Twitch chat message reacting to this character pick. "
            f"Reference the specific winrate."
        )
    else:
        prompt = (
            f"The streamer just started a new Slay the Spire 2 run with {char_name} "
            f"at Ascension {ascension}. Not enough history yet for stats. "
            f"Write one short hype or skeptical Twitch chat message."
        )

    return ai_commentary.generate(prompt, fallback, event_label=f"run start: {char_name} A{ascension}")


def on_relic_acquired(relic_id: str) -> str | None:
    stats      = database.relic_winrate(relic_id)
    relic_name = _fmt_id(relic_id)

    if not stats or stats["total"] < MIN_SAMPLE_SIZE:
        return None

    # Static fallback
    if stats["winrate"] >= 70:
        flavour = "Great sign."
    elif stats["winrate"] >= 50:
        flavour = "Solid historically."
    elif stats["winrate"] >= 30:
        flavour = "Mixed results."
    else:
        flavour = "This hasn't gone well before."

    fallback = f"Relic: {relic_name} | Winrate: {_winrate_str(stats)} | {flavour}"

    _push("relic",
          name=relic_name,
          winrate=stats["winrate"],
          wins=stats["wins"],
          total=stats["total"],
          flavour=flavour)

    prompt = (
        f"The streamer just picked up the relic '{relic_name}' in Slay the Spire 2.\n"
        f"Historical winrate when holding this relic: {stats['winrate']}% "
        f"({stats['wins']} wins out of {stats['total']} runs).\n"
        f"Write one short Twitch chat message about this relic pick. "
        f"Reference the specific winrate. Be direct."
    )

    return ai_commentary.generate(prompt, fallback, event_label=f"relic: {relic_name}")


def on_boss_encounter(encounter_id: str) -> str | None:
    boss_name  = _fmt_id(encounter_id)
    kill_count = database.kill_count(encounter_id)

    _push("boss", name=boss_name)

    # Static fallback
    if kill_count >= 2:
        fallback = f"BOSS: {boss_name} — has killed you {kill_count} times before. No pressure."
    else:
        fallback = f"BOSS: {boss_name}"

    prompt = (
        f"The streamer just entered a boss fight against '{boss_name}' in Slay the Spire 2.\n"
        f"This boss has killed the streamer {kill_count} time(s) before.\n"
        f"Write one short, tense Twitch chat message for entering this boss fight. "
        f"{'Reference that this boss has been deadly before.' if kill_count >= 2 else 'Keep it brief.'}"
    )

    return ai_commentary.generate(prompt, fallback, event_label=f"boss: {boss_name}")


def on_run_end(run: dict, players: list) -> str:
    char_names = " / ".join(_fmt_id(p["character"]) for p in players)
    asc        = run.get("ascension", 0)
    floor      = run.get("floor", "?")
    multi      = len(players) > 1
    mode       = "Co-op" if multi else "Solo"

    # Parse aggregate stats from the raw JSON stored in the run dict
    summary = rs.parse(run.get("raw_json", "{}")) or {}

    if run.get("victory"):
        min_hp   = min((p.get("final_hp") or 0) for p in players)
        hp_note  = f"Survived on {min_hp}hp!" if min_hp <= 10 else None
        fallback = f"{mode} run over | {char_names} A{asc} | Floor {floor} | WIN! PogChamp"
        if hp_note:
            fallback += f" ({hp_note})"

        _push("run_end",
              victory=True,
              character=char_names,
              ascension=asc,
              floor=floor,
              hp_note=hp_note,
              **summary)

        prompt = (
            f"The streamer just WON a Slay the Spire 2 run.\n"
            f"Character(s): {char_names}\n"
            f"Ascension: {asc} | Floors: {floor}\n"
            f"Run time: {summary.get('run_time_str', '?')}\n"
            f"Total damage taken: {summary.get('total_damage', '?')}\n"
            f"Elites fought: {summary.get('elites_fought', '?')}\n"
            f"{'Lowest HP: ' + str(min_hp) + 'hp' if min_hp <= 10 else 'Finished healthy.'}\n"
            f"{'This was a co-op run.' if multi else ''}\n"
            f"Write one short celebratory Twitch chat message. "
            f"{'Mention the close call.' if min_hp <= 10 else ''}"
        )
    else:
        killer     = _fmt_id(run.get("killed_by_encounter", "UNKNOWN"))
        kill_count = database.kill_count(run.get("killed_by_encounter", ""))
        fallback   = f"{mode} run over | {char_names} A{asc} | Floor {floor} | LOSS — died to {killer}"
        if kill_count >= 2:
            fallback += f" ({kill_count}x now)"

        _push("run_end",
              victory=False,
              character=char_names,
              ascension=asc,
              floor=floor,
              killer=killer,
              kill_count=kill_count,
              **summary)

        prompt = (
            f"The streamer just LOST a Slay the Spire 2 run.\n"
            f"Character(s): {char_names}\n"
            f"Ascension: {asc} | Died on floor: {floor}\n"
            f"Killed by: {killer} ({kill_count} times historically)\n"
            f"Total damage taken this run: {summary.get('total_damage', '?')}\n"
            f"Run time: {summary.get('run_time_str', '?')}\n"
            f"{'This was a co-op run.' if multi else ''}\n"
            f"Write one short Twitch chat message about the loss. "
            f"{'This enemy has killed them multiple times.' if kill_count >= 2 else ''}"
        )

    outcome    = "WIN" if run.get("victory") else f"LOSS to {_fmt_id(run.get('killed_by_encounter', 'unknown'))}"
    event_label = f"run end: {outcome} on floor {floor}"
    return ai_commentary.generate(prompt, fallback, event_label=event_label)


# ---------------------------------------------------------------------------
# Static triggers (no AI — too frequent or too low-stakes)
# ---------------------------------------------------------------------------

def on_act_transition(act: int, character: str) -> str | None:
    nemesis   = database.top_killer(exclude_none=True)
    char_name = _fmt_id(character)

    if act == 2:
        msg = f"Act 2 | Your #1 killer is {_fmt_id(nemesis)} — stay healthy, {char_name}."
    elif act == 3:
        msg = f"Act 3 | Final stretch. Don't get greedy."
    else:
        return None

    _push("act", act=act, message=msg)
    return msg


def on_elite_encounter(encounter_id: str) -> str | None:
    elite_name = _fmt_id(encounter_id)
    kill_count = database.kill_count(encounter_id)

    _push("elite", name=elite_name)

    if kill_count >= 2:
        return f"Elite: {elite_name} — has killed you {kill_count} times. Stay sharp."
    return None


def on_card_picked(card_id: str, upgrade_level: int = 0) -> str | None:
    winrate_stats  = database.card_winrate(card_id)
    upgrade_stats  = database.card_upgrade_stats(card_id)
    card_name      = _fmt_id(card_id)
    upgraded       = "+" if upgrade_level > 0 else ""

    if not winrate_stats or winrate_stats["total"] < MIN_SAMPLE_SIZE:
        return None

    # Build the most interesting stat line for the overlay
    stat_parts = []

    if winrate_stats:
        stat_parts.append(f"{winrate_stats['winrate']}% winrate  ·  {winrate_stats['wins']}/{winrate_stats['total']} runs")

    if upgrade_stats and upgrade_stats["times_picked"] >= MIN_SAMPLE_SIZE:
        ur = upgrade_stats["upgrade_rate"]
        if ur >= 90:
            upgrade_note = f"Upgraded {ur}% of the time"
        elif ur <= 10:
            upgrade_note = f"Almost never upgraded ({ur}%)"
        else:
            upgrade_note = f"Upgraded {ur}% of the time"
        stat_parts.append(upgrade_note)

        # Winrate split — only show if there's a meaningful difference (5%+)
        wr_up   = upgrade_stats.get("winrate_upgraded")
        wr_base = upgrade_stats.get("winrate_base")
        if wr_up is not None and wr_base is not None:
            diff = abs(wr_up - wr_base)
            if diff >= 5:
                if wr_up > wr_base:
                    stat_parts.append(f"Better upgraded ({wr_up}% vs {wr_base}%)")
                else:
                    stat_parts.append(f"Upgrade doesn't help much ({wr_up}% vs {wr_base}%)")

    # Flavour for chat message
    wr = winrate_stats["winrate"]
    if wr >= 65:
        flavour = "Strong pick."
    elif wr >= 45:
        flavour = "Decent historically."
    elif wr >= 25:
        flavour = "Questionable, but okay."
    else:
        flavour = "This card has not been kind."

    _push("card",
          name=f"{card_name}{upgraded}",
          stat=stat_parts[0] if stat_parts else None,
          extra_stats=stat_parts[1:],
          flavour=flavour)

    return f"Card: {card_name}{upgraded} | {_winrate_str(winrate_stats)} | {flavour}"


def on_card_upgraded(card_id: str, upgrade_level: int) -> str | None:
    upgrade_stats = database.card_upgrade_stats(card_id)
    card_name     = _fmt_id(card_id)

    stat      = None
    flavour   = "Upgraded"

    if upgrade_stats and upgrade_stats["times_picked"] >= MIN_SAMPLE_SIZE:
        ur     = upgrade_stats["upgrade_rate"]
        wr_up  = upgrade_stats.get("winrate_upgraded")
        wr_base= upgrade_stats.get("winrate_base")

        if ur >= 90:
            flavour = f"Upgraded {ur}% of the time — you love this card"
        elif ur <= 15:
            flavour = f"Rarely upgraded ({ur}%) — bold choice"
        else:
            flavour = f"Upgraded {ur}% of the time"

        if wr_up is not None and wr_base is not None:
            diff = abs(wr_up - wr_base)
            if diff >= 5:
                if wr_up > wr_base:
                    stat = f"{wr_up}% winrate upgraded vs {wr_base}% base"
                else:
                    stat = f"Upgrade doesn't help: {wr_up}% vs {wr_base}% base"

    _push("card",
          name=f"{card_name}+",
          stat=stat,
          extra_stats=[],
          flavour=flavour)
    return None