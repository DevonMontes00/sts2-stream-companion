"""
dump_save.py - Watch current_run.save and print top-level keys + any
fields that look like current room/combat state on every change.

Run this, then walk into a boss or elite room and watch the output.
We're looking for any field that appears/changes BEFORE combat ends.

Usage:
    python dump_save.py
"""

import json
import os
import sys
import time
from pathlib import Path
from watchdog.events import FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer

def find_saves_dir() -> Path:
    appdata = Path(os.environ.get("APPDATA", "~")).expanduser()
    base = appdata / "SlayTheSpire2" / "steam"
    for pattern in [
        "*/modded/profile1/saves",
        "*/profile1/saves",
    ]:
        candidates = sorted(base.glob(pattern))
        if candidates:
            return candidates[0]
    raise FileNotFoundError(f"Could not find saves dir under {base}")


# Keys we already know about — filter these out to surface new ones
KNOWN_KEYS = {
    "acts", "ascension", "build_id", "current_act_index", "events_seen",
        "extra_fields", "game_mode", "killed_by_encounter", "killed_by_event",
        "map_drawings", "map_point_history", "modifiers", "odds", "platform_type",
        "players", "pre_finished_room", "rng", "run_time", "save_time",
        "schema_version", "seed", "shared_relic_grab_bag", "start_time",
        "visited_map_coords", "was_abandoned", "win", "win_time",
}

# Words that suggest a key is about current state
INTERESTING_WORDS = {
    "current", "active", "room", "combat", "encounter", "node",
    "floor", "position", "state", "pending", "battle", "fight",
    "map", "path", "next", "queue"
}


def analyze(path: Path) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [could not parse: {e}]")
        return

    all_keys = set(data.keys())
    new_keys  = all_keys - KNOWN_KEYS

    print(f"\n{'='*60}")
    print(f"FILE CHANGED: {path.name}")
    print(f"All top-level keys: {sorted(all_keys)}")

    if new_keys:
        print(f"\n*** NEW/UNKNOWN KEYS: {sorted(new_keys)} ***")
        for k in sorted(new_keys):
            print(f"  {k}: {json.dumps(data[k])[:200]}")

    # Print any known key whose name suggests current state
    print("\nInteresting fields:")
    for k in sorted(all_keys):
        if any(word in k.lower() for word in INTERESTING_WORDS):
            val = data[k]
            # Truncate large values
            val_str = json.dumps(val)
            if len(val_str) > 300:
                val_str = val_str[:300] + "..."
            print(f"  {k}: {val_str}")

    # Also check top-level keys of each player for anything new
    for i, player in enumerate(data.get("players", [])):
        player_keys = set(player.keys())
        interesting = {k for k in player_keys
                       if any(word in k.lower() for word in INTERESTING_WORDS)}
        if interesting:
            print(f"\n  Player {i} interesting fields:")
            for k in sorted(interesting):
                print(f"    {k}: {json.dumps(player[k])[:200]}")


class SaveHandler(FileSystemEventHandler):
    DEBOUNCE = 1.0

    def __init__(self):
        self._last = 0.0

    def on_modified(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if not (path.name.startswith("current_run") and path.suffix == ".save"):
            return
        now = time.monotonic()
        if now - self._last < self.DEBOUNCE:
            return
        self._last = now
        analyze(path)


def main():
    saves_dir = find_saves_dir()
    print(f"Watching: {saves_dir}")
    print("Enter a boss or elite room and watch for new fields.\n")

    observer = Observer()
    observer.schedule(SaveHandler(), str(saves_dir), recursive=False)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()