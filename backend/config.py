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

import json
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


def _get_json_dict(name: str, default: dict) -> dict:
    """Parse a JSON-object env var (e.g. a drawer-id -> LED-pin map) with a
    safe fallback to `default` if the var is unset or malformed - lets a
    teammate override cabinet/LED wiring per-hardware without ever needing
    to touch code, while the app still boots if they typo the JSON.

    Merges over `default` rather than replacing it wholesale, so overriding
    just the 2-3 pins you've actually wired doesn't blow away the rest of
    the (still-correct) default sequential assignment for everything else.
    """
    val = os.getenv(name)
    if not val:
        return dict(default)
    try:
        parsed = json.loads(val)
        if not isinstance(parsed, dict):
            raise ValueError("not a JSON object")
        return {**default, **parsed}
    except (json.JSONDecodeError, ValueError):
        print(f"[config] WARNING: {name} is not valid JSON, using default")
        return dict(default)


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
GEMINI_TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL", "gemini-3.8-flash")
GEMINI_VISION_MODEL = os.getenv("GEMINI_VISION_MODEL", "gemini-3.8-flash")

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
# AMI structured triage flow (secondary triage / drawer routing / escalation)
# ---------------------------------------------------------------------------
# Fixed-script lines that don't need a Gemini round-trip - no judgment is
# involved, so they're spoken directly via TTS instead of being generated.
# NOTE: these two MUST be defined (routes/triage.py's intro_turn() and
# nfc_vitals_turn() read them directly) - if either is missing/renamed,
# those endpoints 500 instead of speaking anything.

# Everything past the intro/vitals steps needs real judgment (routing
# internal/external/both, severity scoring, red-flag detection, matching an
# injury to supply drawers, deciding when to escalate) - that's Gemini's
# job. It must respond with ONLY the JSON shape described below so
# gemini_service.triage_reply() and routes/triage.py can act on it
# deterministically instead of parsing free-form prose.
GEMINI_AMI_SYSTEM_PROMPT = os.getenv(
    "GEMINI_AMI_SYSTEM_PROMPT",
    "You are AMY, an AI medical staging assistant embedded in a triage "
    "rover. You are NOT a doctor and must NEVER diagnose a medical "
    "condition - you classify, guide, and escalate only, per the rules "
    "below.\n\n"
    "Each turn you will receive: the conversation so far (Patient/Rover "
    "turns), a CURRENT_STATE JSON block (route, confirm_attempts, "
    "internal_severity, red_flags, etc.), and optionally a photo of an "
    "external injury plus the patient's description.\n\n"
    "Respond with ONLY compact JSON in exactly this shape (no extra text, "
    "no markdown fences):\n"
    '{"spoken_reply": "<1-3 short sentences, plain spoken language, no '
    'labels>", "route": "internal"|"external"|"both"|null, "action": '
    '"ask_secondary_triage"|"ask_severity_scale"|"request_camera"|'
    '"confirm_injury"|"reask_camera"|"escalate_emt"|"light_drawers"|'
    '"offer_stay_or_leave"|"log_stay_monitor"|"log_checkout"|"none", '
    '"suspected_injury": "<short label or null>", "confirm_attempts": '
    '<int>, "internal_severity": <int 1-10 or null>, "red_flags": '
    '["dizziness","trouble breathing","chest pressure","nausea",'
    '"confusion"], "drawers": ["<container>-<cabinet>", ...], '
    '"escalate": true|false}\n\n'
    "RULES (do not invent alternative workflows):\n"
    "1. Safety-first hierarchy: if internal_severity is 7-10 OR any "
    "red_flags are present, set escalate=true and action=\"escalate_emt\" "
    "- this overrides ANY in-progress external-wound handling, at any "
    "point.\n"
    "2. External route: after a photo + description exist, set "
    "suspected_injury and action=\"confirm_injury\", asking the patient to "
    "confirm via spoken_reply. If disputed, increment confirm_attempts and "
    "action=\"reask_camera\" (max 3 attempts total). On the 3rd failed "
    "attempt, set escalate=true, action=\"escalate_emt\", and mention in "
    "spoken_reply that a nurse/EMT has been signaled.\n"
    "3. Once suspected_injury is confirmed, or internal severity is mild "
    "(1-6, no red flags) with no external component, choose the smallest "
    "correct set of drawers from the CABINET DIRECTORY below and set "
    "action=\"light_drawers\".\n"
    "4. Internal-only, mild severity (1-6, no red flags): action="
    "\"offer_stay_or_leave\" until the patient picks; then action="
    "\"log_stay_monitor\" or \"log_checkout\" accordingly.\n"
    "5. \"Both\" route: apply rule 1 first. Only proceed to external "
    "confirm/drawers once internal safety is cleared (severity <=6, no "
    "red flags).\n"
    "6. Keep spoken_reply under 3 sentences of natural spoken language "
    "only - never include JSON, field names, or explanations inside "
    "spoken_reply.\n"
    "7. If uncertain what to ask next, ask exactly ONE clarifying "
    "question via spoken_reply with the closest matching "
    "\"ask_*\"/\"request_*\" action.\n\n"
    "CABINET DIRECTORY (drawers values as \"<container>-<cabinet>\"):\n"
    "1-1 Burn Jel (Lidocaine 2%)\n"
    "1-2 Triple Antibiotic\n"
    "1-3 New Skin Liquid Bandage\n"
    "2-1 Fabric Knuckle Bandages\n"
    "2-2 1in x 3in Fabric Adhesive Bandages\n"
    "2-3 3/4in x 3in Heavy Woven Adhesive Bandages\n"
    "3-1 Finger Injury Kit\n"
    "3-2 Dry Eye Relief\n"
    "3-3 Gauze Pads\n"
    "4-1 Cooling Gel Sheets\n"
    "4-2 Vaseline\n"
    "4-3 Rash Cream",
)

# Structured (non-prose) version of the same directory above, keyed by the
# exact "<container>-<cabinet>" ids Gemini returns in "drawers". This is
# what the API/hardware layer uses to look up a human-readable supply name
# and (below) a physical LED index - the prose block above is only for
# Gemini's prompt; this dict is for code/HTTP responses so the two never
# drift silently out of sync (both live in config.py, edit together).
CABINET_DIRECTORY = {
    "1-1": "Burn Jel (Lidocaine 2%)",
    "1-2": "Triple Antibiotic (Neosporin)",
    "1-3": "New Skin Liquid Bandage (Benzethonium chloride 0.2%)",
    "2-1": "Fabric Knuckle Bandages",
    "2-2": '1" x 3" Fabric Adhesive Bandages',
    "2-3": '3/4" x 3" Heavy Woven Adhesive Bandages',
    "3-1": "Finger Injury Kit",
    "3-2": "Dry Eye Relief",
    "3-3": "Gauze Pads",
    "4-1": "Cooling Gel Sheets",
    "4-2": "Vaseline",
    "4-3": "Rash Cream",
}

# Lean, purpose-built decision prompt for the moment right after a photo is
# analyzed (step 10.5, gemini_service.image_care_decision()). Deliberately
# SEPARATE from GEMINI_AMI_SYSTEM_PROMPT below: that one carries the full
# multi-turn conversational rulebook (route/confirm_attempts/internal_
# severity/red_flags/etc.) and costs a lot of input tokens per call. This
# one only has to answer ONE narrow question - nurse vs. robot-dispensed
# supplies vs. free-to-go - using ONLY what the vision model already saw
# (no transcript, no CURRENT_STATE), so it stays a small, fast, cheap call.
# The CABINET DIRECTORY is appended at call time from CABINET_DIRECTORY
# (single source of truth) instead of being duplicated here as prose.
GEMINI_IMAGE_TRIAGE_PROMPT = os.getenv(
    "GEMINI_IMAGE_TRIAGE_PROMPT",
    "You are AMY, a triage rover assistant. You are NOT a doctor and must "
    "NEVER diagnose. You just looked at a photo of a patient's visible "
    "injury - OBSERVATIONS below is what a vision model already extracted "
    "from that photo. Decide exactly ONE outcome.\n\n"
    "Respond with ONLY compact JSON (no markdown fences, no extra text):\n"
    '{"decision": "nurse"|"supplies"|"clear", "spoken_reply": "<=2 short '
    'spoken sentences, plain language, no labels>", "drawers": '
    '["<container>-<cabinet>", ...], "reason": "<very short internal note, '
    'not spoken aloud>"}\n\n'
    "RULES:\n"
    '- "nurse": heavy/uncontrolled bleeding, a deep or gaping wound, '
    "exposed bone/tendon, a burn beyond minor, or anything a basic "
    "first-aid kit can't safely handle.\n"
    '- "supplies": a minor visible injury (small cut, scrape, mild redness '
    "or swelling) that basic first-aid supplies can address - pick the "
    "smallest correct set of drawers from the CABINET DIRECTORY below.\n"
    '- "clear": no visible injury of concern - patient is free to go.\n'
    "- If uncertain between nurse and supplies, choose nurse (err safe).\n"
    '- drawers must be [] unless decision is "supplies".',
)

# ============================================================
# AMY / GREEN ROVER DRAWER SYSTEM
# ============================================================
# Drawer IDs use "<category>-<drawer>", e.g. "1-1" = Category 1 Drawer 1.
# Each logical drawer maps to ONE physical LED (1-12, matching the
# ESP32/Uno firmware's numbering - the ESP32 doesn't need to know
# categories, only the flat LED number):
#
#   Gemini picks drawer id(s) -> drawers_to_leds() finds the physical LED
#   -> API response's "leds" field -> ESP32 sends LED number to Uno ->
#   Uno flashes that physical drawer.
#
# Override via the DRAWER_LED_MAP env var (JSON) if the real wiring ever
# changes, e.g.:
#   DRAWER_LED_MAP={"1-1":14,"1-2":27,"1-3":26,"2-1":25,"2-2":33,"2-3":32,
#                    "3-1":35,"3-2":34,"3-3":39,"4-1":36,"4-2":4,"4-3":16}
DRAWER_LED_MAP = _get_json_dict(
    "DRAWER_LED_MAP",
    {
        # Category 1
        "1-1": 1,
        "1-2": 2,
        "1-3": 3,
        # Category 2
        "2-1": 4,
        "2-2": 5,
        "2-3": 6,
        # Category 3
        "3-1": 7,
        "3-2": 8,
        "3-3": 9,
        # Category 4
        "4-1": 10,
        "4-2": 11,
        "4-3": 12,
    },
)


def drawers_to_leds(drawer_ids: list) -> list:
    """Map drawer ids Gemini picked (e.g. ["1-2", "3-1"]) to their physical
    LED numbers (e.g. [2, 7]) using DRAWER_LED_MAP.

    - Unknown drawer ids are skipped (with a console warning) rather than
      raising, since a Gemini hallucination here should never crash a live
      triage turn.
    - Duplicate LEDs are collapsed so the same physical drawer never gets
      told to flash twice in one response.
    """
    leds = []
    for drawer_id in drawer_ids:
        drawer_id = str(drawer_id).strip()
        led_number = DRAWER_LED_MAP.get(drawer_id)
        if led_number is None:
            print(f"WARNING: No LED mapping for drawer {drawer_id}")
            continue
        if led_number not in leds:
            leds.append(led_number)
    return leds

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



def require(*names: str) -> list[str]:
    """Return the subset of `names` whose config value is missing.
    """
    missing = []
    module_globals = globals()
    for name in names:
        if not module_globals.get(name):
            missing.append(name)
    return missing
