"""
Gemini (Google GenAI) service layer.

Two responsibilities, matching pipeline steps 7 and 10:
  - triage_reply(): text-in/text-out conversational triage (step 7). The
    audio-to-text step happens in elevenlabs_service.speech_to_text() BEFORE
    this is called; this function never touches raw audio.
  - analyze_injury_image(): vision-in/JSON-out visible-injury description
    (step 10).

Model names and prompts all come from config.py - swap models or tune the
bedside manner via env vars, no code changes needed.
"""

import json
from typing import Optional

from google import genai
from PIL import Image

import config

_client = None


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


def triage_reply(user_text: str, transcript: Optional[list] = None) -> str:
    """Step 7: given the latest thing the patient said (already
    transcribed by ElevenLabs STT) plus prior turns, return Gemini's next
    triage question/response as plain text ready to be spoken by TTS.

    `transcript` is the list of {"role": "user"|"assistant", "text": ...}
    dicts stored in Snowflake for this session (oldest first). Pass None or
    [] for the first turn of a new session.
    """
    client = get_client()
    transcript = transcript or []

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
        f"{config.GEMINI_TRIAGE_SYSTEM_PROMPT}\n\n"
        f"Conversation so far:\n{history_text}\n\nRover:"
    )

    response = client.models.generate_content(
        model=config.GEMINI_TEXT_MODEL,
        contents=prompt,
    )
    return (response.text or "").strip()


def analyze_injury_image(image_path: str) -> dict:
    """Step 10: send a photo to Gemini vision and get back a small
    structured (non-diagnostic) description of what's visible.

    Returns a dict, always containing at least `observations`,
    `possible_concern_level`, and `notes` keys, even on parse failure
    (falls back to putting the raw text in `notes`).
    """
    client = get_client()
    image = Image.open(image_path)

    response = client.models.generate_content(
        model=config.GEMINI_VISION_MODEL,
        contents=[config.GEMINI_IMAGE_PROMPT, image],
    )
    raw_text = (response.text or "").strip()

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

    response = client.models.generate_content(
        model=config.GEMINI_TEXT_MODEL,
        contents=prompt,
    )
    raw_text = (response.text or "").strip()
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
