"""
watcher.py - Watch the STS2 history folder and process new run files.

Uses the `watchdog` library for cross-platform filesystem events.
A debounce mechanism prevents double-processing: Godot sometimes writes
a file in multiple flushes, which fires multiple events for one file.
"""

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer

log = logging.getLogger(__name__)

# Only process files with these extensions.
WATCHED_EXTENSIONS = {".save", ".run", ".json"}

# Seconds to wait after the last event before processing a file.
# Prevents acting on a half-written file.
DEBOUNCE_SECONDS = 2.0


class DebounceHandler(FileSystemEventHandler):
    """
    Watchdog event handler with per-file debouncing.

    When a file event fires, we schedule a delayed callback instead of
    acting immediately. If another event for the same file arrives before
    the delay expires, we reset the timer — so we only process once the
    file has stopped changing.

    This is a common pattern for file-watching tools. The threading.Timer
    approach is simple and works well at this scale. If you later need to
    handle high-volume events, swap it for a queue + worker thread.
    """

    def __init__(self, callback: Callable[[Path], None]):
        super().__init__()
        self._callback = callback
        self._pending: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def _schedule(self, path_str: str) -> None:
        with self._lock:
            # Cancel any existing timer for this path and start a fresh one.
            if path_str in self._pending:
                self._pending[path_str].cancel()

            timer = threading.Timer(
                DEBOUNCE_SECONDS,
                self._fire,
                args=(path_str,),
            )
            self._pending[path_str] = timer
            timer.start()

    def _fire(self, path_str: str) -> None:
        with self._lock:
            self._pending.pop(path_str, None)

        path = Path(path_str)
        if path.suffix.lower() in WATCHED_EXTENSIONS:
            log.debug("Debounce elapsed, processing: %s", path.name)
            self._callback(path)

    def on_created(self, event: FileCreatedEvent) -> None:
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_modified(self, event: FileModifiedEvent) -> None:
        if not event.is_directory:
            self._schedule(event.src_path)


class RunWatcher:
    """
    High-level watcher. Owns the Observer lifecycle and exposes
    start() / stop() so main.py stays clean.
    """

    def __init__(self, watch_dir: Path, callback: Callable[[Path], None]):
        self._watch_dir = watch_dir
        self._handler  = DebounceHandler(callback)
        self._observer = Observer()

    def start(self) -> None:
        if not self._watch_dir.exists():
            raise FileNotFoundError(
                f"Watch directory does not exist: {self._watch_dir}\n"
                "Make sure you've completed at least one run with the "
                "CombatLogMod installed so the history folder is created."
            )

        self._observer.schedule(
            self._handler,
            str(self._watch_dir),
            recursive=False,   # history files are flat in this folder
        )
        self._observer.start()
        log.info("Watching: %s", self._watch_dir)

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join()
        log.info("Watcher stopped.")

    def process_existing(self, callback: Callable[[Path], None]) -> None:
        """
        On startup, backfill any run files that already exist in the folder
        but haven't been stored in the DB yet. Passes each file to the same
        callback used for live events.
        """
        files = [
            f for f in self._watch_dir.iterdir()
            if f.is_file() and f.suffix.lower() in WATCHED_EXTENSIONS
        ]
        if not files:
            log.info("No existing run files found — starting fresh.")
            return

        log.info("Backfilling %d existing run file(s)...", len(files))
        for f in sorted(files):  # sorted = chronological by filename timestamp
            callback(f)
            time.sleep(0.05)    # tiny yield — be polite to the filesystem
