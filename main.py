"""
main.py - Orchestrates file watchers, DB, Twitch bot, and overlay server.

Usage:
    python main.py                Normal mode
    python main.py --no-bot       No Twitch posting
    python main.py --no-overlay   No OBS overlay server
    python main.py --debug        Verbose logging
    python main.py --backfill     Process history files then exit
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Callable, Optional

import database
import parser as run_parser
import triggers
from watcher import RunWatcher

log = logging.getLogger(__name__)


def setup_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("sts2_watcher.log", encoding="utf-8"),
        ],
    )


def find_history_dir() -> Path:
    appdata = Path(os.environ.get("APPDATA", "~")).expanduser()
    base = appdata / "SlayTheSpire2" / "steam"

    if not base.exists():
        raise FileNotFoundError(f"STS2 data folder not found at {base}.")

    patterns = [
        "*/modded/profile1/saves/history",
        "*/profile1/saves/history",
        "*/modded/profile1/history",
        "*/profile1/history",
    ]
    for pattern in patterns:
        candidates = sorted(base.glob(pattern))
        if candidates:
            if len(candidates) > 1:
                log.warning("Multiple history folders found — using: %s", candidates[0])
            log.info("Found history dir: %s", candidates[0])
            return candidates[0]

    raise FileNotFoundError(f"Could not find history folder under {base}.")


def make_run_end_callback(
    post_fn: Optional[Callable[[str], None]],
    resolve_prediction_fn: Optional[Callable[[bool], None]] = None,
) -> Callable:
    def callback(path: Path) -> None:
        try:
            result = run_parser.parse_run_file(path)
            if result is None:
                return
            parsed, raw_json = result
            is_new_run = database.insert_run(parsed, raw_json)

            # Attach raw_json so on_run_end can parse aggregate stats
            parsed["raw_json"] = raw_json

            if post_fn and is_new_run and parsed.get("players"):
                msg = triggers.on_run_end(parsed, parsed["players"])
                post_fn(msg)
                # Resolve any open boss prediction.
                # Non-final boss wins are already resolved by the act-transition
                # signal, so resolve() is a no-op in those cases.
                if resolve_prediction_fn:
                    resolve_prediction_fn(bool(parsed.get("victory")))

        except Exception as e:
            log.error("Failed to process %s: %s", path.name, e, exc_info=True)

    return callback


def main() -> None:
    arg_parser = argparse.ArgumentParser(description="STS2 watcher + bot + overlay")
    arg_parser.add_argument("--no-bot",     action="store_true")
    arg_parser.add_argument("--no-overlay", action="store_true")
    arg_parser.add_argument("--debug",      action="store_true")
    arg_parser.add_argument("--backfill",   action="store_true")
    args = arg_parser.parse_args()

    setup_logging(debug=args.debug)
    log.info("=== STS2 Watcher starting ===")

    database.init_db()

    try:
        history_dir = find_history_dir()
    except FileNotFoundError as e:
        log.error("%s", e)
        sys.exit(1)

    saves_dir = history_dir.parent

    # ---- Overlay server (start first so event bus is ready) ----------
    if not args.no_overlay and not args.backfill:
        try:
            from server import start_server_thread, event_bus
            actual_port = start_server_thread()
            triggers.set_event_bus(event_bus)
            log.info("Overlay ready — OBS browser source URL: http://localhost:%d", actual_port)
            if actual_port != 5000:
                log.warning(
                    "Port 5000 was in use — overlay is on port %d. "
                    "Update your OBS browser source URL.", actual_port
                )
        except Exception as e:
            log.warning("Overlay server failed to start: %s", e)

    # ---- Twitch bot --------------------------------------------------
    post_fn: Optional[Callable[[str], None]] = None
    bot = None

    if not args.no_bot and not args.backfill:
        try:
            from bot import start_bot_thread
            bot = start_bot_thread()
            post_fn = bot.post
        except Exception as e:
            log.warning("Could not start Twitch bot: %s", e)

    # ---- History watcher ---------------------------------------------
    resolve_fn = bot.resolve_prediction if bot else None
    db_only_callback = make_run_end_callback(post_fn=None)
    live_callback    = make_run_end_callback(post_fn=post_fn, resolve_prediction_fn=resolve_fn)

    history_watcher = RunWatcher(watch_dir=history_dir, callback=live_callback)
    history_watcher.process_existing(callback=db_only_callback)

    if args.backfill:
        log.info("--backfill complete. Exiting.")
        return

    history_watcher.start()

    # ---- Active run watcher ------------------------------------------
    active_watcher = None
    try:
        from active_run import ActiveRunWatcher
        active_watcher = ActiveRunWatcher(
            saves_dir=saves_dir,
            post_fn=post_fn or (lambda msg: None),
            create_prediction_fn=bot.create_prediction if bot else None,
            resolve_prediction_fn=bot.resolve_prediction if bot else None,
            cancel_prediction_fn=bot.cancel_prediction if bot else None,
        )
        active_watcher.start()
    except Exception as e:
        log.warning("Active run watcher failed to start: %s", e)

    # ---- Chat commands -----------------------------------------------
    if bot and active_watcher and not args.backfill:
        try:
            from chat_commands import RunCommandsCog
            bot.add_cog(RunCommandsCog(bot, active_watcher))
            log.info("Chat commands registered: !run !deck !relics !stats !wr")
        except Exception as e:
            log.warning("Failed to register chat commands: %s", e)

    # ---- Keep alive --------------------------------------------------
    try:
        log.info("Running. Press Ctrl+C to stop.")
        # Brief pause to let the bot finish connecting before posting startup
        time.sleep(2)
        if post_fn:
            run_count = database.get_connection().execute(
                "SELECT COUNT(*) FROM runs"
            ).fetchone()[0]
            post_fn(f"STS2 watcher online — {run_count} runs tracked. Let's go! PogChamp")
        if _event_bus := triggers._event_bus:
            from config import TWITCH_CHANNEL
            _event_bus.push("startup", {
                "channel": TWITCH_CHANNEL,
                "runs":    database.get_connection().execute(
                                "SELECT COUNT(*) FROM runs"
                            ).fetchone()[0],
            })
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        history_watcher.stop()


if __name__ == "__main__":
    main()