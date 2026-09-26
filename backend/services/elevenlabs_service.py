"""
ElevenLabs service layer - the "voice" of the rover.

Two responsibilities, matching pipeline steps 8 and 9:
  - speech_to_text(): microphone audio -> transcript text (step 8).
  - text_to_speech(): Gemini's text reply -> spoken audio file (step 9).

Every model/voice choice is an env var via config.py - swap voices or the
STT/TTS model without touching this file.
"""

import mimetypes
import os
import uuid

from elevenlabs.client import ElevenLabs

import config

_client = None


def get_client() -> ElevenLabs:
    global _client
    if _client is None:
        missing = config.require("ELEVENLABS_API_KEY")
        if missing:
            raise RuntimeError(
                "Missing ELEVENLABS_API_KEY. Set it in backend/.env (see .env.example)."
            )
        _client = ElevenLabs(api_key=config.ELEVENLABS_API_KEY)
    return _client


def speech_to_text(audio_path: str) -> str:
    """Step 8: microphone recording (any common audio/video file ElevenLabs
    Scribe supports - wav/mp3/m4a/webm/etc.) -> transcript text.
    """
    client = get_client()
    content_type = mimetypes.guess_type(audio_path)[0] or "application/octet-stream"

    with open(audio_path, "rb") as f:
        kwargs = dict(
            model_id=config.ELEVENLABS_STT_MODEL,
            file=(os.path.basename(audio_path), f, content_type),
        )
        if config.ELEVENLABS_LANGUAGE_CODE:
            kwargs["language_code"] = config.ELEVENLABS_LANGUAGE_CODE

        result = client.speech_to_text.convert(**kwargs)

    # `result` is normally a SpeechToTextChunkResponseModel with a `.text`
    # attribute. Multichannel responses instead expose `.transcripts`; we
    # fall back gracefully so this never hard-crashes the pipeline.
    text = getattr(result, "text", None)
    if text is not None:
        return text.strip()

    transcripts = getattr(result, "transcripts", None)
    if transcripts:
        return " ".join(t.text for t in transcripts if getattr(t, "text", None)).strip()

    return ""


def _extension_for_output_format(output_format: str) -> str:
    """Map an ElevenLabs `output_format` string (e.g. "wav_16000",
    "mp3_44100_128", "pcm_16000") to the right file extension, so the file
    we write and the Content-Type we later serve it with both stay correct
    no matter which format is configured.
    """
    prefix = output_format.split("_")[0].lower()
    return {
        "mp3": ".mp3",
        "wav": ".wav",
        "pcm": ".pcm",
        "opus": ".opus",
        "ulaw": ".raw",
        "alaw": ".raw",
    }.get(prefix, ".bin")


def text_to_speech(text: str, out_dir: str) -> str:
    """Step 9: Gemini's reply text -> an audio file on disk, in whatever
    format ELEVENLABS_OUTPUT_FORMAT is configured to (default wav_16000).
    Returns the full path to the written file so the caller can serve/attach
    it - the extension always matches the actual bytes written.
    """
    client = get_client()

    audio_chunks = client.text_to_speech.convert(
        voice_id=config.ELEVENLABS_VOICE_ID,
        text=text,
        model_id=config.ELEVENLABS_TTS_MODEL,
        output_format=config.ELEVENLABS_OUTPUT_FORMAT,
    )
    audio_bytes = b"".join(audio_chunks)

    os.makedirs(out_dir, exist_ok=True)
    ext = _extension_for_output_format(config.ELEVENLABS_OUTPUT_FORMAT)
    filename = f"reply-{uuid.uuid4().hex[:10]}{ext}"
    out_path = os.path.join(out_dir, filename)
    with open(out_path, "wb") as f:
        f.write(audio_bytes)

    return out_path
