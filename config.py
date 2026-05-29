"""
config.py - Load settings from a .env file.

Why .env instead of hardcoding?
  Credentials should never live in source code. A .env file is ignored by git
  (add it to .gitignore), lives only on your machine, and is the standard
  pattern for local secrets across basically every language and framework.
"""

import os
import sys
import logging
from pathlib import Path
from dotenv import load_dotenv

log = logging.getLogger(__name__)

# Load .env from the project root (same folder as this file).
_env_path = Path(__file__).parent / ".env"
load_dotenv(_env_path)


def _require(key: str) -> str:
    """Read an env var and exit loudly if it's missing."""
    value = os.getenv(key, "").strip()
    if not value:
        log.error(
            "Missing required config: %s\n"
            "Copy .env.example to .env and fill in your values.", key
        )
        sys.exit(1)
    return value


# Twitch credentials
TWITCH_TOKEN   = _require("TWITCH_TOKEN")    # oauth:xxxxx
TWITCH_BOT_NICK = _require("TWITCH_BOT_NICK")
TWITCH_CHANNEL = _require("TWITCH_CHANNEL")

# Bot behaviour
# Minimum number of historical runs before we quote a winrate.
# Below this threshold the sample is too small to be meaningful.
MIN_SAMPLE_SIZE = int(os.getenv("MIN_SAMPLE_SIZE", "3"))

# Seconds between messages to avoid chat spam.
MESSAGE_COOLDOWN = float(os.getenv("MESSAGE_COOLDOWN", "3.0"))