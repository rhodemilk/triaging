"""
Central configuration for the triage rover backend.

EVERYTHING that could change between laptops, demo rooms, or teammates lives
here as an environment variable with a sane default. Nothing below is a
hardcoded secret or a hardcoded "this only works on my machine" value.

Load order:
1. `.env` file (via python-dotenv) - for local dev, never committed.
2. Real environment variables (export FOO=bar) - always win over `.env`.
3. The default given to os.getenv() - last resort so the app still boots
   and tells you what's missing instead of crashing with a KeyError.
"""

import os
from dotenv import load_dotenv

# Loads backend/.env into os.environ if present. Safe to call even if the
# file doesn't exist (e.g. in production where real env vars are injected).
load_dotenv()


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _get_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or val == "":
        return default
    try:
        return int(val)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Flask / server
# ---------------------------------------------------------------------------
FLASK_HOST = os.getenv("FLASK_HOST", "0.0.0.0")
FLASK_PORT = _get_int("FLASK_PORT", 5001)
FLASK_DEBUG = _get_bool("FLASK_DEBUG", True)

# Where uploaded/generated media files are written so they can be served
# back to the caller (rover, browser, curl, whatever hits the API).
# NOTE: relative paths are resolved against the CURRENT WORKING DIRECTORY
# the process was launched from. Since app.py is meant to be run with
# `cd backend && python3 app.py`, the default here is just "media" (i.e.
# backend/media) - NOT "backend/media", which would double up into
# backend/backend/media if you're already inside backend/.
MEDIA_ROOT = os.getenv("MEDIA_ROOT", "media")
AUDIO_SUBDIR = os.getenv("AUDIO_SUBDIR", "audio")
IMAGE_SUBDIR = os.getenv("IMAGE_SUBDIR", "images")

# ---------------------------------------------------------------------------
# Snowflake
# ---------------------------------------------------------------------------
SNOWFLAKE_USER = os.getenv("SNOWFLAKE_USER")
SNOWFLAKE_PASSWORD = os.getenv("SNOWFLAKE_PASSWORD")
SNOWFLAKE_ACCOUNT = os.getenv("SNOWFLAKE_ACCOUNT")
SNOWFLAKE_WAREHOUSE = os.getenv("SNOWFLAKE_WAREHOUSE")
SNOWFLAKE_DATABASE = os.getenv("SNOWFLAKE_DATABASE", "TRIAGE_ROVER")
SNOWFLAKE_SCHEMA = os.getenv("SNOWFLAKE_SCHEMA", "PUBLIC")
SNOWFLAKE_ROLE = os.getenv("SNOWFLAKE_ROLE")  # optional

PATIENTS_TABLE = os.getenv("SNOWFLAKE_PATIENTS_TABLE", "PATIENTS")
SESSIONS_TABLE = os.getenv("SNOWFLAKE_SESSIONS_TABLE", "TRIAGE_SESSIONS")

# ---------------------------------------------------------------------------
# Gemini (Google GenAI)
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL", "gemini-2.5-flash")
GEMINI_VISION_MODEL = os.getenv("GEMINI_VISION_MODEL", "gemini-2.5-flash")

# The "personality"/instructions for the triage conversation. Kept as an env
# var so you can tune the bedside manner without touching code.
GEMINI_TRIAGE_SYSTEM_PROMPT = os.getenv(
    "GEMINI_TRIAGE_SYSTEM_PROMPT",
    "You are a calm, brief medical triage assistant on an autonomous rover. "
    "You are NOT a doctor and must never diagnose. Ask short, clarifying "
    "questions one at a time (location of pain, internal vs external, "
    "severity 1-10, onset, bleeding, mobility). After enough information, "
    "summarize findings and suggest a triage urgency level (1=critical, "
    "5=minor) as a recommendation only, and clearly state a human medical "
    "professional must confirm. Keep every response under 3 sentences so "
    "it sounds natural when spoken aloud.",
)

GEMINI_IMAGE_PROMPT = os.getenv(
    "GEMINI_IMAGE_PROMPT",
    "You are assisting a medical triage rover. Look at this photo of a "
    "patient's visible skin/injury. Describe only what is visibly observable "
    "(e.g. bruising, swelling, redness, laceration, discoloration) and roughly "
    "where on the body if visible. Do NOT diagnose a medical condition. "
    "Respond ONLY with compact JSON: "
    '{"observations": ["..."], "possible_concern_level": "low|medium|high", '
    '"notes": "..."}',
)

# ---------------------------------------------------------------------------
# ElevenLabs
# ---------------------------------------------------------------------------
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

# Voice used for the rover's spoken responses. Default is one of
# ElevenLabs' public premade voices ("Rachel"); override with your own
# cloned/chosen voice_id from the ElevenLabs dashboard.
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
ELEVENLABS_TTS_MODEL = os.getenv("ELEVENLABS_TTS_MODEL", "eleven_turbo_v2_5")
ELEVENLABS_STT_MODEL = os.getenv("ELEVENLABS_STT_MODEL", "scribe_v1")
# wav_16000 (not mp3) on purpose: it's a real WAV container (still plays fine
# via afplay/browser/etc for laptop testing) but its payload is raw 16-bit
# PCM after a 44-byte header - which means an ESP32 can skip that header and
# stream the rest straight to an I2S amp (e.g. MAX98357A) with NO mp3 decoder
# needed. 16000 matches the INMP441 mic's capture rate used on the firmware
# side, so no resampling is needed either.
ELEVENLABS_OUTPUT_FORMAT = os.getenv("ELEVENLABS_OUTPUT_FORMAT", "wav_16000")
ELEVENLABS_LANGUAGE_CODE = os.getenv("ELEVENLABS_LANGUAGE_CODE") or None

# ---------------------------------------------------------------------------
# Rover / Arduino
# ---------------------------------------------------------------------------
# The rover doesn't need Flask's config to know its own IP - that lives in
# the .ino sketch - but we keep the shared secret / route names here so both
# sides can be reconfigured without touching route logic.
ROVER_API_KEY = os.getenv("ROVER_API_KEY", "")  # optional simple shared secret


def require(*names: str) -> list[str]:
    """Return the subset of `names` whose config value is falsy/missing.

    Use this at startup (or lazily, per-route) to fail with a clear message
    instead of a cryptic SDK error three calls deep.
    """
    missing = []
    module_globals = globals()
    for name in names:
        if not module_globals.get(name):
            missing.append(name)
    return missing
