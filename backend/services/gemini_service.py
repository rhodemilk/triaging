"""
Gemini (Google GenAI) service layer.

Two responsibilities, matching pipeline steps 7 and 10:
  - triage_reply(): text-in/structured-JSON-out AMI triage turn (step 7).
    The audio-to-text step happens in elevenlabs_service.speech_to_text()
    BEFORE this is called; this function never touches raw audio. The
    returned dict's "action"/"escalate"/"drawers" fields are what
    routes/triage.py uses to decide what to actually DO (speak, escalate to
    EMT, light supply drawers) - this function only decides, it never calls
    Snowflake or hardware itself.
  - analyze_injury_image(): vision-in/JSON-out visible-injury description
    (step 10).

Model names and prompts all come from config.py - swap models or tune the
bedside manner via env vars, no code changes needed.
"""

import json
import logging
import time
from typing import Optional

from google import genai
from google.genai import errors as genai_errors
from PIL import Image

import config

logger = logging.getLogger(__name__)

_client = None

# Gemini occasionally returns transient server-side errors (503 model
# overloaded, 500 internal, 504 timeout). These are worth a short retry
# with backoff instead of immediately bubbling up as a 500 to the
# rover/patient. 429 (quota/rate-limit exceeded) is deliberately NOT
# retried here - retrying a request that's already over quota just burns
# more of the (often very limited, e.g. free-tier 20/day) quota for
# nothing, since the server will keep saying no anyway. Anything else
# (4xx auth/bad-request, etc.) also fails fast.
_RETRYABLE_STATUS_CODES = {500, 503, 504}
_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 1.5


def get_client() -> genai.Client:
    global _client
    if _client is None:
        missing = config.require("GEMINI_API_KEY")
        if missing:
            raise RuntimeError(
                "Missing GEMINI_API_KEY. Set it in backend/.env (see .env.example)."
            )
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


def _generate_content_with_retry(client: genai.Client, **kwargs):
    """Wrapper around client.models.generate_content() that retries a few
    times (with backoff) on transient server errors like the 503
    "model is currently experiencing high demand" response, instead of
    letting the first hiccup surface as a 500 to the caller.

    Raises the underlying google.genai.errors.APIError if every attempt is
    exhausted, or immediately for non-retryable errors, so callers should
    still be prepared to catch failures and fall back gracefully.
    """
    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return client.models.generate_content(**kwargs)
        except genai_errors.APIError as exc:
            last_error = exc
            status_code = getattr(exc, "code", None) or getattr(
                exc, "status_code", None
            )

            if status_code not in _RETRYABLE_STATUS_CODES or attempt == _MAX_RETRIES:
                raise
            wait_seconds = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "Gemini call failed with retryable status %s (attempt %d/%d); "
                "retrying in %.1fs",
                status_code,
                attempt,
                _MAX_RETRIES,
                wait_seconds,
            )
            time.sleep(wait_seconds)
    raise last_error


def triage_reply(
    user_text: str,
    transcript: Optional[list] = None,
    triage_state: Optional[dict] = None,
) -> dict:
    """Step 7 (AMI structured flow): given the latest thing the patient said
    (already transcribed by ElevenLabs STT) plus prior turns and the
    session's current triage_state, ask Gemini to decide the single next
    thing to say/do and return it as a dict.

    `transcript` is the list of {"role": "user"|"assistant", "text": ...}
    dicts stored in Snowflake for this session (oldest first). Pass None or
    [] for the first turn of a new session.

    `triage_state` is the session's persisted state dict (route,
    confirm_attempts, internal_severity, red_flags, ...) - see
    db.snowflake_client.DEFAULT_TRIAGE_STATE for the shape. Pass None or {}
    for a brand new session.

    Always returns every key below, even on a parse failure, so callers
    never need defensive .get() chains:
      spoken_reply, route, action, suspected_injury, confirm_attempts,
      internal_severity, red_flags, drawers, escalate
    """
    client = get_client()
    transcript = transcript or []
    triage_state = triage_state or {}

    # Build a simple back-and-forth text block Gemini can follow. We keep
    # this as plain text rather than the SDK's multi-turn `chat` session
    # object so every turn is explicitly reproducible from what's stored in
    # Snowflake (no hidden client-side state to lose between requests).
    history_lines = []
    for turn in transcript:
        speaker = "Patient" if turn["role"] == "user" else "Rover"
        history_lines.append(f"{speaker}: {turn['text']}")
    history_lines.append(f"Patient: {user_text}")
    history_text = "\n".join(history_lines)

    prompt = (
        f"{config.GEMINI_AMI_SYSTEM_PROMPT}\n\n"
        f"CURRENT_STATE: {json.dumps(triage_state)}\n\n"
        f"Conversation so far:\n{history_text}\n\n"
        "Respond with ONLY the JSON object described above."
    )

    try:
        response = _generate_content_with_retry(
            client,
            model=config.GEMINI_TEXT_MODEL,
            contents=prompt,
        )
        raw_text = (response.text or "").strip()
    except genai_errors.APIError:
        # Gemini is down/overloaded even after retries. Don't 500 the whole
        # voice turn - fall back to a generic "please repeat" reply and
        # carry the prior triage_state forward untouched so the
        # conversation/session state isn't lost, just this one turn.
        logger.exception("Gemini triage_reply call failed after retries")
        return {
            "spoken_reply": (
                "Sorry, I'm having trouble thinking right now. "
                "Could you say that one more time?"
            ),
            "route": triage_state.get("route"),
            "action": "none",
            "suspected_injury": triage_state.get("suspected_injury"),
            "confirm_attempts": triage_state.get("confirm_attempts", 0),
            "internal_severity": triage_state.get("internal_severity"),
            "red_flags": triage_state.get("red_flags", []) or [],
            "drawers": [],
            "escalate": False,
        }

    # Gemini sometimes wraps JSON in ```json fences despite instructions -
    # strip those before parsing (same pattern as analyze_injury_image()).
    cleaned = raw_text
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned[4:] if cleaned.lower().startswith("json") else cleaned

    try:
        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict):
            parsed = {}
    except (json.JSONDecodeError, AttributeError):
        parsed = {}

    return {
        "spoken_reply": parsed.get("spoken_reply")
        or raw_text
        or "Could you say that again?",
        "route": parsed.get("route"),
        "action": parsed.get("action", "none"),
        "suspected_injury": parsed.get("suspected_injury"),
        "confirm_attempts": parsed.get(
            "confirm_attempts", triage_state.get("confirm_attempts", 0)
        ),
        "internal_severity": parsed.get("internal_severity"),
        "red_flags": parsed.get("red_flags", []) or [],
        "drawers": parsed.get("drawers", []) or [],
        "escalate": bool(parsed.get("escalate", False)),
    }


def image_care_decision(image_analysis: dict) -> dict:
    """Step 10.5: lean, single-purpose Gemini call made right after
    analyze_injury_image() (step 10). Deliberately does NOT pass the full
    conversation transcript or CURRENT_STATE, and uses the short
    config.GEMINI_IMAGE_TRIAGE_PROMPT instead of the much larger
    GEMINI_AMI_SYSTEM_PROMPT - so this stays a cheap, fast, few-hundred-
    token call instead of re-running the whole multi-turn rulebook, while
    still using the model's full reasoning to answer the one question that
    actually matters right after a photo: does this patient need a nurse,
    robot-dispensed first-aid supplies, or nothing (cleared to go)?

    Always returns every key below, even on a parse/API failure - and on
    failure it defaults to "nurse" (the safe side to fail toward) rather
    than silently doing nothing:
      decision ("nurse"|"supplies"|"clear"), spoken_reply, drawers (list),
      reason
    """
    client = get_client()

    directory_lines = "\n".join(
        f"{drawer_id} {name}" for drawer_id, name in config.CABINET_DIRECTORY.items()
    )
    prompt = (
        f"{config.GEMINI_IMAGE_TRIAGE_PROMPT}\n\n"
        'CABINET DIRECTORY (drawers values as "<container>-<cabinet>"):\n'
        f"{directory_lines}\n\n"
        f"OBSERVATIONS: {json.dumps(image_analysis)}"
    )

    try:
        response = _generate_content_with_retry(
            client,
            model=config.GEMINI_TEXT_MODEL,
            contents=prompt,
        )
        raw_text = (response.text or "").strip()
    except genai_errors.APIError:
        logger.exception("Gemini image_care_decision call failed after retries")
        return {
            "decision": "nurse",
            "spoken_reply": (
                "I'm having trouble analyzing that right now, so I'm "
                "going to get a nurse to take a look, just to be safe."
            ),
            "drawers": [],
            "reason": "Gemini service error - defaulted to nurse for safety.",
        }

    cleaned = raw_text
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned[4:] if cleaned.lower().startswith("json") else cleaned

    try:
        parsed = json.loads(cleaned)
        decision = parsed.get("decision")
        if decision not in ("nurse", "supplies", "clear"):
            decision = "nurse"  # unknown/malformed model output -> err safe
        drawers = parsed.get("drawers", []) or []
        if decision != "supplies":
            drawers = []
        return {
            "decision": decision,
            "spoken_reply": parsed.get("spoken_reply")
            or raw_text
            or "Let me get you the right help.",
            # Drop any hallucinated drawer id that isn't in the real
            # cabinet, same defensive pattern as drawers_to_leds() below.
            "drawers": [d for d in drawers if d in config.CABINET_DIRECTORY],
            "reason": parsed.get("reason", ""),
        }
    except (json.JSONDecodeError, AttributeError):
        return {
            "decision": "nurse",
            "spoken_reply": raw_text or "I'm not certain, so I'm getting a nurse to check.",
            "drawers": [],
            "reason": "Could not parse Gemini response - defaulted to nurse for safety.",
        }


def analyze_injury_image(image_path: str) -> dict:
    """Step 10: send a photo to Gemini vision and get back a small
    structured (non-diagnostic) description of what's visible.

    Returns a dict, always containing at least `observations`,
    `possible_concern_level`, and `notes` keys, even on parse failure
    (falls back to putting the raw text in `notes`).
    """
    client = get_client()
    image = Image.open(image_path)

    try:
        response = _generate_content_with_retry(
            client,
            model=config.GEMINI_VISION_MODEL,
            contents=[config.GEMINI_IMAGE_PROMPT, image],
        )
        raw_text = (response.text or "").strip()
    except genai_errors.APIError:
        logger.exception("Gemini analyze_injury_image call failed after retries")
        return {
            "observations": [],
            "possible_concern_level": "unknown",
            "notes": "Image analysis unavailable (Gemini service error).",
        }

    # Gemini sometimes wraps JSON in ```json fences despite instructions -
    # strip those before parsing.
    cleaned = raw_text
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned[4:] if cleaned.lower().startswith("json") else cleaned

    try:
        parsed = json.loads(cleaned)
        parsed.setdefault("observations", [])
        parsed.setdefault("possible_concern_level", "unknown")
        parsed.setdefault("notes", "")
        return parsed
    except (json.JSONDecodeError, AttributeError):
        return {
            "observations": [],
            "possible_concern_level": "unknown",
            "notes": raw_text,
        }


def summarize_session(transcript: list, image_analysis: Optional[dict]) -> dict:
    """Step 11: combine the voice transcript + image analysis into one
    final human-readable summary + a suggested triage level.

    Returns {"summary": str, "triage_level": str}.
    """
    client = get_client()

    convo_text = "\n".join(
        f"{'Patient' if t['role'] == 'user' else 'Rover'}: {t['text']}"
        for t in transcript
    )
    image_text = (
        json.dumps(image_analysis) if image_analysis else "No image was captured."
    )

    prompt = (
        "You are combining a rover's triage conversation and an optional "
        "visible-injury photo analysis into ONE short handoff summary for "
        "a human medic. Do not diagnose. Respond ONLY with compact JSON: "
        '{"summary": "2-3 sentence handoff summary", '
        '"triage_level": "1"|"2"|"3"|"4"|"5"} '
        "where 1 is most urgent and 5 is least urgent.\n\n"
        f"Conversation transcript:\n{convo_text}\n\n"
        f"Image analysis:\n{image_text}"
    )

    try:
        response = _generate_content_with_retry(
            client,
            model=config.GEMINI_TEXT_MODEL,
            contents=prompt,
        )
        raw_text = (response.text or "").strip()
    except genai_errors.APIError:
        logger.exception("Gemini summarize_session call failed after retries")
        return {
            "summary": "Summary unavailable (Gemini service error). See raw transcript.",
            "triage_level": "3",
        }

    cleaned = raw_text
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned[4:] if cleaned.lower().startswith("json") else cleaned

    try:
        parsed = json.loads(cleaned)
        return {
            "summary": parsed.get("summary", raw_text),
            "triage_level": str(parsed.get("triage_level", "3")),
        }
    except json.JSONDecodeError:
        return {"summary": raw_text, "triage_level": "3"}
