"""
active_run.py - Watch the live current_run.save file and detect state changes.

State we track and diff on every save write:
  - Character + ascension       → run start message
  - Floor / act                 → act transition message
  - Relics per player           → relic acquired message
  - Deck per player             → card picked message
  - Card upgrade levels         → card upgraded message
  - Current room type + model   → boss/elite encounter message

The current room is the last entry in map_point_history. STS2 writes
current_run.save when you enter a room, so we see the room type immediately
on entry — before the fight starts.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from watchdog.events import FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer

import triggers

log = logging.getLogger(__name__)

ACT2_FLOOR_THRESHOLD = 17
ACT3_FLOOR_THRESHOLD = 33


# ---------------------------------------------------------------------------
# State dataclass
# ---------------------------------------------------------------------------

@dataclass
class CardState:
    """Minimal card snapshot for diffing — id + upgrade level is all we need."""
    card_id:       str
    upgrade_level: int = 0

    def __hash__(self):
        return hash((self.card_id, self.upgrade_level))

    def __eq__(self, other):
        return self.card_id == other.card_id and self.upgrade_level == other.upgrade_level


@dataclass
class RunState:
    character:    Optional[str] = None
    ascension:    Optional[int] = None
    floor:        int           = 0
    act:          int           = 1

    # {player_index: set of relic IDs}
    relics: dict = field(default_factory=dict)

    # {player_index: list of CardState}
    cards:  dict = field(default_factory=dict)

    # Current room info — None until a room is entered.
    # current_coord is the (col, row) of the last visited map node and is
    # the authoritative signal for room-entry detection. current_room_type
    # and current_room_model are derived from it via saved_map.
    current_coord:      Optional[tuple] = None
    current_room_type:  Optional[str]   = None
    current_room_model: Optional[str]   = None


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _parse_active_run(path: Path, text: Optional[str] = None) -> Optional[RunState]:
    try:
        if text is None:
            text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError):
        return None

    state = RunState()
    state.ascension = data.get("ascension", 0)

    history = data.get("map_point_history", [])
    state.floor = sum(len(act) for act in history if isinstance(act, list))

    if state.floor >= ACT3_FLOOR_THRESHOLD:
        state.act = 3
    elif state.floor >= ACT2_FLOOR_THRESHOLD:
        state.act = 2
    else:
        state.act = 1

    # ---- Current room via visited_map_coords + saved_map -----------------
    # visited_map_coords gains a new entry the moment the player selects a
    # room node on the map — before combat starts. Cross-referencing the
    # last coord against the current act's saved_map gives us the room type.
    # This is more reliable than pre_finished_room, which is always null in
    # practice (never observed populated in real save files).
    visited    = data.get("visited_map_coords", [])
    act_index  = data.get("current_act_index", 0)
    acts       = data.get("acts", [])

    state.current_coord      = None
    state.current_room_type  = None
    state.current_room_model = None

    if visited and acts and act_index < len(acts):
        act        = acts[act_index]
        saved_map  = act.get("saved_map", {})
        rooms_data = act.get("rooms", {})

        # Build (col, row) → room-type lookup from the saved map
        coord_type: dict[tuple, str] = {}
        for pt in saved_map.get("points", []):
            c = pt.get("coord", {})
            coord_type[(c.get("col"), c.get("row"))] = pt.get("type", "")
        boss_pt = saved_map.get("boss", {})
        if boss_pt:
            bc = boss_pt.get("coord", {})
            coord_type[(bc.get("col"), bc.get("row"))] = "boss"

        last       = visited[-1]
        last_coord = (last.get("col"), last.get("row"))
        room_type  = coord_type.get(last_coord)

        state.current_coord     = last_coord
        state.current_room_type = room_type

        if room_type == "elite":
            n         = rooms_data.get("elite_encounters_visited", 0)
            elite_ids = rooms_data.get("elite_encounter_ids", [])
            if n < len(elite_ids):
                state.current_room_model = elite_ids[n]
        elif room_type == "boss":
            state.current_room_model = rooms_data.get("boss_id")

    # ---- Per-player state ---------------------------------------------
    for i, player in enumerate(data.get("players", [])):
        char = player.get("character", "")
        if i == 0:
            state.character = char.replace("CHARACTER.", "") if char else None

        state.relics[i] = {
            r["id"] for r in player.get("relics", []) if "id" in r
        }

        state.cards[i] = [
            CardState(
                card_id=c.get("id", "UNKNOWN"),
                upgrade_level=c.get("current_upgrade_level", 0),
            )
            for c in player.get("deck", [])
            if "id" in c
        ]

    return state


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------

def _diff_cards(old_cards: list[CardState], new_cards: list[CardState]) -> tuple[list, list]:
    """
    Return (newly_added, upgraded) card lists.

    Why not a simple set diff?
    Decks can have duplicates — two Strikes are valid. So we compare
    sorted tuples of (card_id, upgrade_level) counts.

    Strategy:
      - Build a frequency map for old and new
      - Cards whose (id, upgrade) count increased → newly added
      - Cards whose id exists in old at lower upgrade level → upgraded

    This correctly handles:
      - Picking a card that's already in the deck (adds one copy)
      - Upgrading a card (same id, higher upgrade_level)
      - Picking an already-upgraded card from a shop
    """
    from collections import Counter

    old_counter = Counter((c.card_id, c.upgrade_level) for c in old_cards)
    new_counter = Counter((c.card_id, c.upgrade_level) for c in new_cards)

    # Net new (id, upgrade) pairs
    added = list((new_counter - old_counter).elements())

    # Separate true new picks from upgrades:
    # An upgrade appears as a new (id, level+1) and a removed (id, level)
    old_ids = Counter(c.card_id for c in old_cards)
    new_ids = Counter(c.card_id for c in new_cards)

    newly_picked  = []
    newly_upgraded = []

    for card_id, upgrade_level in added:
        # If the total count of this card_id didn't increase, it's an upgrade
        if new_ids[card_id] <= old_ids[card_id]:
            newly_upgraded.append(CardState(card_id, upgrade_level))
        else:
            newly_picked.append(CardState(card_id, upgrade_level))

    return newly_picked, newly_upgraded


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------

def _fmt_boss_name(raw_id: str) -> str:
    """Human-readable boss name from a raw encounter ID."""
    for prefix in ("ENCOUNTER.", "RELIC.", "CARD.", "CHARACTER.", "ENCHANTMENT."):
        if raw_id.startswith(prefix):
            raw_id = raw_id[len(prefix):]
            break
    return raw_id.replace("_", " ").title()


class ActiveRunWatcher:

    def __init__(
        self,
        saves_dir: Path,
        post_fn: Callable[[str], None],
        create_prediction_fn: Optional[Callable[[str], None]] = None,
        resolve_prediction_fn: Optional[Callable[[bool], None]] = None,
        cancel_prediction_fn: Optional[Callable[[], None]] = None,
    ):
        self._saves_dir           = saves_dir
        self._post                = post_fn
        self._create_prediction   = create_prediction_fn
        self._resolve_prediction  = resolve_prediction_fn
        self._cancel_prediction   = cancel_prediction_fn
        self._last_state: Optional[RunState] = None
        self._observer  = Observer()

        handler = _ActiveRunHandler(
            saves_dir=saves_dir,
            on_change=self._handle_state_change,
        )
        self._observer.schedule(handler, str(saves_dir), recursive=False)

    @property
    def current_state(self) -> Optional[RunState]:
        return self._last_state

    def start(self) -> None:
        if not self._saves_dir.exists():
            log.warning("Saves dir not found: %s — active run watching disabled.", self._saves_dir)
            return
        self._observer.start()
        log.info("Active run watcher started: %s", self._saves_dir)

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join()

    def _handle_state_change(self, new: RunState) -> None:
        # ----------------------------------------------------------------
        # First observation — establish baseline without firing any events.
        # If the script starts mid-run, we don't want to diff the entire
        # existing deck/relic list against nothing.
        # Only exception: fire run_start if floor == 0, meaning we caught
        # the very beginning of a run before any rooms were entered.
        # ----------------------------------------------------------------
        if self._last_state is None:
            if new.floor == 0 and new.character:
                msg = triggers.on_run_start(new.character, new.ascension or 0)
                self._maybe_post(msg)
                # Announce starting relics (Neow's gifts) by diffing against
                # empty. Cards are skipped — announcing the whole starting deck
                # is too noisy.
                for idx, current_relics in new.relics.items():
                    for relic_id in sorted(current_relics):
                        msg = triggers.on_relic_acquired(relic_id)
                        self._maybe_post(msg)
            # Set baseline to current state — diffs start from next change
            self._last_state = new
            return

        old = self._last_state

        # If nothing relevant changed, update state and skip all diffing.
        # This prevents duplicate relic/card events when the game writes
        # the file multiple times without changing game state.
        state_unchanged = (
            new.floor               == old.floor
            and new.current_coord      == old.current_coord
            and new.relics              == old.relics
            and all(
                sorted((c.card_id, c.upgrade_level) for c in new.cards.get(i, []))
                ==
                sorted((c.card_id, c.upgrade_level) for c in old.cards.get(i, []))
                for i in new.cards
            )
        )
        if state_unchanged:
            self._last_state = new
            return

        # ----------------------------------------------------------------
        # New run detection (after first observation)
        # Fires when character changes or floor resets to 0
        # ----------------------------------------------------------------
        is_new_run = (
            (new.character and new.character != old.character)
            or (new.floor < old.floor)
        )

        if is_new_run and new.character:
            if self._cancel_prediction:
                self._cancel_prediction()
            msg = triggers.on_run_start(new.character, new.ascension or 0)
            self._maybe_post(msg)
            # Reset baseline so diffs start fresh for the new run
            old = RunState(
                character=new.character,
                ascension=new.ascension,
                floor=new.floor,
                act=new.act,
                relics={i: set()                for i in new.relics},
                cards ={i: list(new.cards[i])   for i in new.cards},
                current_coord=None,
                current_room_type=None,
                current_room_model=None,
            )
            self._last_state = old

        # ----------------------------------------------------------------
        # Act transition — also resolves the boss prediction as a WIN,
        # since you can only reach the next act by defeating the boss.
        # ----------------------------------------------------------------
        if old and new.act > old.act:
            msg = triggers.on_act_transition(new.act, new.character or "")
            self._maybe_post(msg)
            if self._resolve_prediction:
                self._resolve_prediction(True)

        # ----------------------------------------------------------------
        # Boss / Elite room entry
        # Trigger on coord change, not model change. After a fight completes,
        # elite_encounters_visited increments which changes current_room_model
        # even though the player hasn't moved — triggering on model would fire
        # a false "new elite" event after every elite victory.
        # ----------------------------------------------------------------
        if old and new.current_coord != old.current_coord and new.current_coord is not None:
            room_type  = new.current_room_type
            room_model = new.current_room_model

            if room_type == "boss" and room_model:
                msg = triggers.on_boss_encounter(room_model)
                self._maybe_post(msg)
                if self._create_prediction:
                    self._create_prediction(_fmt_boss_name(room_model))

            elif room_type == "elite" and room_model:
                msg = triggers.on_elite_encounter(room_model)
                self._maybe_post(msg)

        # ----------------------------------------------------------------
        # New relics
        # ----------------------------------------------------------------
        for idx, current_relics in new.relics.items():
            prev_relics = (old.relics.get(idx) or set()) if old else set()
            for relic_id in sorted(current_relics - prev_relics):
                msg = triggers.on_relic_acquired(relic_id)
                self._maybe_post(msg)

        # ----------------------------------------------------------------
        # Card picks and upgrades
        # ----------------------------------------------------------------
        for idx, new_cards in new.cards.items():
            old_cards = (old.cards.get(idx) or []) if old else []
            picked, upgraded = _diff_cards(old_cards, new_cards)

            for card in picked:
                msg = triggers.on_card_picked(card.card_id, card.upgrade_level)
                self._maybe_post(msg)

            for card in upgraded:
                msg = triggers.on_card_upgraded(card.card_id, card.upgrade_level)
                self._maybe_post(msg)

        self._last_state = new

    def _maybe_post(self, message: Optional[str]) -> None:
        if message:
            log.info("[BOT] %s", message)
            self._post(message)


# ---------------------------------------------------------------------------
# Watchdog handler
# ---------------------------------------------------------------------------

class _ActiveRunHandler(FileSystemEventHandler):
    """
    Process every write to current_run.save immediately.

    We removed debouncing because pre_finished_room (used for boss/elite
    detection) is a transient field — it's set on room entry and cleared
    on the very next write. A debounce that drops intermediate events was
    causing us to always read the post-cleared version.

    Half-written file reads are handled gracefully by _parse_active_run's
    try/except — a failed JSON parse returns None and is silently skipped.
    """

    def __init__(self, saves_dir: Path, on_change: Callable[[RunState], None]):
        super().__init__()
        self._saves_dir   = saves_dir
        self._on_change   = on_change
        self._last_hash: Optional[int] = None

    def on_modified(self, event: FileModifiedEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        if not (path.name.startswith("current_run") and path.suffix == ".save"):
            return

        # Read raw bytes first for hashing — cheap dedup before JSON parse.
        # Windows fires two events per write (content + metadata change).
        # Both events read identical bytes, so the hash catches the duplicate
        # without any risk of dropping legitimate rapid writes that have
        # different content (e.g. boss room entry followed by combat start).
        try:
            raw = path.read_bytes()
        except OSError:
            return

        content_hash = hash(raw)
        if content_hash == self._last_hash:
            return
        self._last_hash = content_hash

        try:
            data = raw.decode("utf-8")
        except UnicodeDecodeError:
            return

        state = _parse_active_run(path, data)
        if state:
            log.debug(
                "Save parsed — floor:%s room_type:%s room_model:%s",
                state.floor, state.current_room_type, state.current_room_model
            )
            self._on_change(state)