"""
database.py - SQLite layer for STS2 run history.

Schema design reflects the actual STS2 save format (confirmed from real files):
  - Runs are top-level, may be singleplayer or multiplayer (co-op).
  - Each run has 1+ players, each with their own deck and relics.
  - Cards now store enchantment data (new STS2 mechanic).

Why SQLite: zero setup, single file, trivially backed up, queryable
in DB Browser for SQLite without any Python during development.
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DB_PATH = Path("sts2_runs.db")

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT    UNIQUE NOT NULL,  -- from filename stem (start_time)
    file_path           TEXT    NOT NULL,
    ascension           INTEGER,
    floor               INTEGER,                  -- derived: total map points traversed
    victory             INTEGER,                  -- 0/1
    seed                TEXT,
    run_time            INTEGER,                  -- seconds
    start_time          INTEGER,                  -- unix timestamp
    was_abandoned       INTEGER,                  -- 0/1
    killed_by_encounter TEXT,
    killed_by_event     TEXT,
    game_mode           TEXT,
    build_id            TEXT,
    schema_version      INTEGER,
    is_multiplayer      INTEGER,                  -- 0/1, derived from player count
    raw_json            TEXT    NOT NULL,
    parsed_at           TEXT    NOT NULL
);

-- One row per player per run. Handles both solo and co-op.
CREATE TABLE IF NOT EXISTS run_players (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT    NOT NULL,
    player_index  INTEGER NOT NULL,               -- 0-based, preserves order
    steam_id      TEXT,
    character     TEXT,
    final_gold    INTEGER,
    final_hp      INTEGER,
    final_max_hp  INTEGER,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

-- Cards linked to a specific player in a specific run.
CREATE TABLE IF NOT EXISTS run_cards (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT    NOT NULL,
    player_index    INTEGER NOT NULL,
    card_id         TEXT    NOT NULL,
    upgrade_level   INTEGER DEFAULT 0,
    enchantment_id  TEXT,                         -- e.g. "ENCHANTMENT.SOWN"
    enchantment_amt INTEGER,
    floor_acquired  INTEGER,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

-- Relics linked to a specific player in a specific run.
CREATE TABLE IF NOT EXISTS run_relics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT    NOT NULL,
    player_index    INTEGER NOT NULL,
    relic_id        TEXT    NOT NULL,
    floor_acquired  INTEGER,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_runs_victory    ON runs(victory);
CREATE INDEX IF NOT EXISTS idx_runs_ascension  ON runs(ascension);
CREATE INDEX IF NOT EXISTS idx_rp_run_id       ON run_players(run_id);
CREATE INDEX IF NOT EXISTS idx_rc_run_id       ON run_cards(run_id);
CREATE INDEX IF NOT EXISTS idx_rc_card_id      ON run_cards(card_id);
CREATE INDEX IF NOT EXISTS idx_rr_run_id       ON run_relics(run_id);
CREATE INDEX IF NOT EXISTS idx_rr_relic_id     ON run_relics(relic_id);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables and indexes. Safe to call on every startup."""
    with get_connection() as conn:
        conn.executescript(SCHEMA)
    log.info("Database initialised at %s", DB_PATH.resolve())


def run_exists(run_id: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    return row is not None


def insert_run(parsed: dict, raw_json: str) -> None:
    """
    Write a fully parsed run (with players, cards, relics) to the DB.
    Everything in one transaction — either all lands or nothing does.
    """
    run_id = parsed["run_id"]
    if run_exists(run_id):
        log.debug("Run %s already in DB — skipping.", run_id)
        return

    now = datetime.utcnow().isoformat()

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO runs (
                run_id, file_path, ascension, floor, victory, seed,
                run_time, start_time, was_abandoned, killed_by_encounter,
                killed_by_event, game_mode, build_id, schema_version,
                is_multiplayer, raw_json, parsed_at
            ) VALUES (
                :run_id, :file_path, :ascension, :floor, :victory, :seed,
                :run_time, :start_time, :was_abandoned, :killed_by_encounter,
                :killed_by_event, :game_mode, :build_id, :schema_version,
                :is_multiplayer, :raw_json, :parsed_at
            )
            """,
            {**parsed, "raw_json": raw_json, "parsed_at": now},
        )

        for player in parsed.get("players", []):
            conn.execute(
                """
                INSERT INTO run_players
                    (run_id, player_index, steam_id, character,
                     final_gold, final_hp, final_max_hp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    player["player_index"],
                    player.get("steam_id"),
                    player.get("character"),
                    player.get("final_gold"),
                    player.get("final_hp"),
                    player.get("final_max_hp"),
                ),
            )

            for card in player.get("cards", []):
                conn.execute(
                    """
                    INSERT INTO run_cards
                        (run_id, player_index, card_id, upgrade_level,
                         enchantment_id, enchantment_amt, floor_acquired)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        player["player_index"],
                        card["id"],
                        card.get("upgrade_level", 0),
                        card.get("enchantment_id"),
                        card.get("enchantment_amt"),
                        card.get("floor_acquired"),
                    ),
                )

            for relic in player.get("relics", []):
                conn.execute(
                    """
                    INSERT INTO run_relics
                        (run_id, player_index, relic_id, floor_acquired)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        player["player_index"],
                        relic["id"],
                        relic.get("floor_acquired"),
                    ),
                )

    # Build a readable summary for the log
    player_summaries = ", ".join(
        f"{p.get('character', '?')} ({p.get('final_hp', '?')}hp)"
        for p in parsed.get("players", [])
    )
    log.info(
        "Stored run %s | A%s | Floor %s | %s | Players: [%s]",
        run_id,
        parsed.get("ascension", "?"),
        parsed.get("floor", "?"),
        "WIN" if parsed.get("victory") else "LOSS",
        player_summaries,
    )


# ---------------------------------------------------------------------------
# Query helpers — these will feed the Twitch bot and overlay.
# ---------------------------------------------------------------------------

def relic_winrate(relic_id: str) -> Optional[dict]:
    """
    Winrate for any relic, across all players/runs that held it.

    The DISTINCT ensures a co-op run where both players had the relic
    only counts as one run — not two.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(DISTINCT r.run_id) AS total,
                COUNT(DISTINCT CASE WHEN r.victory = 1 THEN r.run_id END) AS wins,
                ROUND(
                    100.0 * COUNT(DISTINCT CASE WHEN r.victory = 1
                                  THEN r.run_id END) / COUNT(DISTINCT r.run_id),
                    1
                ) AS winrate
            FROM run_relics rr
            JOIN runs r ON r.run_id = rr.run_id
            WHERE rr.relic_id = ?
            """,
            (relic_id,),
        ).fetchone()
    return dict(row) if row else None


def character_stats(character: str) -> Optional[dict]:
    """
    Per-character win/loss stats. In co-op both players are IRONCLAD,
    so this counts unique runs where at least one player used the character.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(DISTINCT r.run_id)    AS total_runs,
                COUNT(DISTINCT CASE WHEN r.victory = 1 THEN r.run_id END) AS wins,
                ROUND(
                    100.0 * COUNT(DISTINCT CASE WHEN r.victory = 1
                                  THEN r.run_id END) / COUNT(DISTINCT r.run_id),
                    1
                )                           AS winrate,
                MAX(r.floor)                AS best_floor,
                MAX(r.ascension)            AS highest_ascension
            FROM run_players rp
            JOIN runs r ON r.run_id = rp.run_id
            WHERE rp.character = ?
            """,
            (character,),
        ).fetchone()
    return dict(row) if row else None


def recent_runs(limit: int = 10) -> list[dict]:
    """Fetch the N most recent runs — useful for the stream overlay."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT run_id, ascension, floor, victory, seed,
                   killed_by_encounter, game_mode, start_time
            FROM runs
            ORDER BY start_time DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def card_upgrade_stats(card_id: str) -> Optional[dict]:
    """
    Upgrade behaviour and split winrates for a card.

    Returns:
        times_picked      - runs where this card was in the final deck
        times_upgraded    - subset where upgrade_level > 0
        upgrade_rate      - percentage upgraded
        winrate_upgraded  - winrate in runs where it was upgraded
        winrate_base      - winrate in runs where it was NOT upgraded
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(DISTINCT r.run_id) AS times_picked,

                COUNT(DISTINCT CASE WHEN rc.upgrade_level > 0
                      THEN r.run_id END) AS times_upgraded,

                ROUND(
                    100.0 * COUNT(DISTINCT CASE WHEN rc.upgrade_level > 0
                                  THEN r.run_id END)
                    / COUNT(DISTINCT r.run_id), 1
                ) AS upgrade_rate,

                -- Winrate in runs where the card was upgraded
                ROUND(
                    100.0 * COUNT(DISTINCT CASE WHEN rc.upgrade_level > 0
                                               AND r.victory = 1
                                  THEN r.run_id END)
                    / NULLIF(COUNT(DISTINCT CASE WHEN rc.upgrade_level > 0
                                  THEN r.run_id END), 0),
                    1
                ) AS winrate_upgraded,

                -- Winrate in runs where it was NOT upgraded
                ROUND(
                    100.0 * COUNT(DISTINCT CASE WHEN rc.upgrade_level = 0
                                               AND r.victory = 1
                                  THEN r.run_id END)
                    / NULLIF(COUNT(DISTINCT CASE WHEN rc.upgrade_level = 0
                                  THEN r.run_id END), 0),
                    1
                ) AS winrate_base

            FROM run_cards rc
            JOIN runs r ON r.run_id = rc.run_id
            WHERE rc.card_id = ?
            """,
            (card_id,),
        ).fetchone()
    return dict(row) if row and row["times_picked"] else None


def card_winrate(card_id: str) -> Optional[dict]:
    """
    Winrate for runs where the player's final deck contained this card.
    Uses COUNT(DISTINCT run_id) to avoid double-counting runs where
    the player had multiple copies of the same card.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(DISTINCT r.run_id) AS total,
                COUNT(DISTINCT CASE WHEN r.victory = 1 THEN r.run_id END) AS wins,
                ROUND(
                    100.0 * COUNT(DISTINCT CASE WHEN r.victory = 1 THEN r.run_id END)
                    / COUNT(DISTINCT r.run_id),
                    1
                ) AS winrate
            FROM run_cards rc
            JOIN runs r ON r.run_id = rc.run_id
            WHERE rc.card_id = ?
            """,
            (card_id,),
        ).fetchone()
    return dict(row) if row and row["total"] else None


def top_killer(exclude_none: bool = True) -> str | None:
    """Return the encounter that has killed the player most often."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT killed_by_encounter, COUNT(*) as count
            FROM runs
            WHERE victory = 0
              AND (:exclude_none = 0 OR killed_by_encounter != 'NONE.NONE')
            GROUP BY killed_by_encounter
            ORDER BY count DESC
            LIMIT 1
            """,
            {"exclude_none": int(exclude_none)},
        ).fetchone()
    return row["killed_by_encounter"] if row else None


def kill_count(encounter_id: str) -> int:
    """How many times has a specific encounter killed the player."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as count FROM runs WHERE killed_by_encounter = ?",
            (encounter_id,),
        ).fetchone()
    return row["count"] if row else 0