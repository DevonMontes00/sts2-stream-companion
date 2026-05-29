# STS2 Stream Companion

A real-time Twitch bot and OBS overlay for Slay the Spire 2 streamers.

Watches your STS2 save files, tracks run history in a local database, and posts AI-generated commentary to Twitch chat — with a live overlay in OBS showing stats as you play.

## Features

- **Run history tracking** — every run stored in SQLite with cards, relics, characters, and outcomes
- **Live Twitch bot** — posts on run start, relic picks, boss encounters, and run end
- **AI commentary** — powered by Claude (Anthropic API), falls back to static messages silently if unavailable
- **OBS overlay** — real-time notifications for relics, cards, elites, bosses, and a full run summary panel on win/loss
- **Card stats** — winrate, upgrade rate, and upgraded vs base winrate split per card
- **Relic stats** — historical winrate whenever a relic is picked up
- **Boss/Elite detection** — fires on room entry, not after combat

## Setup

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/sts2-stream-companion.git
cd sts2-stream-companion
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.example .env
```

Edit `.env` and fill in:
- `TWITCH_TOKEN` — get from https://twitchapps.com/tmi/ while logged into your **bot** account
- `TWITCH_BOT_NICK` — your bot account username
- `TWITCH_CHANNEL` — your streamer channel name
- `ANTHROPIC_API_KEY` — get from https://console.anthropic.com/ (optional, falls back to static messages)

### 3. Add OBS browser source

- URL: `http://localhost:5000`
- Width: `1920`, Height: `1080`
- Custom CSS: leave empty

### 4. Run

```bash
python main.py
```

## Flags

```bash
python main.py --debug       # verbose logging
python main.py --no-bot      # file watching + overlay only, no Twitch posting
python main.py --no-overlay  # no OBS overlay server
python main.py --backfill    # process existing history files then exit
```

## Project structure

```
├── main.py           Entry point — wires everything together
├── active_run.py     Watches current_run.save for live game events
├── watcher.py        Watches history folder for completed runs
├── parser.py         Parses STS2 history .run files (JSON)
├── database.py       SQLite schema, inserts, and stat queries
├── run_summary.py    Extracts aggregate stats from a completed run
├── triggers.py       Generates chat messages and overlay events
├── ai_commentary.py  Anthropic API wrapper with timeout + fallback
├── bot.py            Twitch IRC bot (twitchio)
├── server.py         FastAPI WebSocket server for OBS overlay
├── overlay.html      OBS browser source — renders all notifications
├── config.py         Loads settings from .env
└── .env.example      Credential template
```

## Requirements

- Python 3.11+
- Slay the Spire 2 (Steam, Early Access)
- OBS Studio
- Twitch account for the bot