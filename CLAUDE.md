# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Slay the Spire 2 stream companion: a Python app that watches STS2 save files, detects in-game events, posts AI-generated Twitch chat commentary, and drives a live OBS overlay via WebSocket.

## Commands

**Setup:**
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
cp env.example .env   # then fill in credentials
```

**Run:**
```bash
python main.py                # normal operation
python main.py --debug        # verbose logging
python main.py --no-bot       # skip Twitch bot (overlay + watching only)
python main.py --no-overlay   # skip OBS WebSocket server
python main.py --backfill     # process existing history files then exit
```

There is no test suite. Testing is done by running the app against real STS2 sessions or by manually triggering events.

## Architecture

**Data flow:** STS2 writes save files → `watchdog` detects changes → `parser.py` extracts run state → `database.py` stores it → `active_run.py` diffs live state → `triggers.py` decides what events to fire → `bot.py` posts to Twitch and `server.py` pushes WebSocket events to `overlay.html`.

**Threading model:** The app runs three concurrent async loops bridged by `asyncio.run_coroutine_threadsafe()`:
- Main thread — watchdog file observers, parsing, DB writes
- Bot thread — twitchio async event loop (`bot.py`)
- Server thread — uvicorn async loop for FastAPI + WebSocket (`server.py`)
- AI pool — `ThreadPoolExecutor(2)` for non-blocking Anthropic API calls (`ai_commentary.py`)

**Key modules:**
| File | Role |
|------|------|
| `main.py` | Wires all components, handles CLI flags |
| `config.py` | Loads `.env`; validates required credentials |
| `watcher.py` | `watchdog` observer with 2s debounce; routes `.save`/`.run`/`.json` to parser |
| `active_run.py` | Polls `current_run.save` for live state diffs (relics, cards, floor, room type) |
| `parser.py` | Parses STS2 JSON save format into Python dicts |
| `database.py` | SQLite schema (`runs`, `run_players`, `run_cards`, `run_relics`) + aggregation queries |
| `run_summary.py` | Post-run aggregation: elites fought, damage, rest choices, card pick rate |
| `triggers.py` | Maps game events → Twitch messages + overlay payloads |
| `ai_commentary.py` | Anthropic API wrapper; 4s timeout with static-text fallback |
| `bot.py` | Twitch IRC bot via `twitchio`; thread-safe async message posting |
| `server.py` | FastAPI + WebSockets; auto-reconnect + late-client event replay |
| `overlay.html` | Self-contained OBS browser source (1920×1080); notification stack + run-end panel |

## Important Behaviors

- **Debounce:** File events are buffered 2s to avoid duplicate triggers on rapid writes.
- **Message cooldown:** Configurable (default 3s) in `triggers.py` to prevent Twitch spam.
- **AI fallback:** If the Anthropic call exceeds 4s or fails, a static message is used instead — never block the event pipeline.
- **Late-join replay:** `server.py` replays the last overlay event to any browser source that connects after the fact.
- **Co-op:** Parser and DB support multi-player runs via the `run_players` table.

## Environment Variables

Required: `TWITCH_TOKEN`, `BOT_NICK`, `CHANNEL`
Optional: `ANTHROPIC_API_KEY` (disables AI commentary if absent), `COOLDOWN_SECONDS`, `STS2_HISTORY_PATH`

See `env.example` for the full list and where to obtain credentials.
