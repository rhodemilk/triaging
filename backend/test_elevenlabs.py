"""
Phase 2b smoke test: confirm the ElevenLabs API key + voice_id + models work
in isolation, before wiring them into the Flask voice pipeline. Run from the
`backend/` folder:

    python3 test_elevenlabs.py

Does NOT touch Snowflake or Gemini, and does NOT require a patient/session
to exist - it round-trips text -> speech -> text using only
services/elevenlabs_service.py.
"""

import os
import tempfile

from services import elevenlabs_service

TEST_SENTENCE = "Hello, this is a test of the triage rover's voice."

print("1) Generating speech with ElevenLabs text_to_speech()...")
out_dir = tempfile.mkdtemp(prefix="triage_tts_test_")
audio_path = elevenlabs_service.text_to_speech(TEST_SENTENCE, out_dir)
size_kb = os.path.getsize(audio_path) / 1024
print(f"   Wrote {audio_path} ({size_kb:.1f} KB)")

print("\n2) Transcribing that same file back with speech_to_text()...")
transcript = elevenlabs_service.speech_to_text(audio_path)
print(f"   Transcript: {transcript!r}")

print("\n3) Sanity check: does the transcript resemble the original text?")
original_words = set(TEST_SENTENCE.lower().rstrip(".").split())
transcript_words = set(transcript.lower().rstrip(".").split())
overlap = original_words & transcript_words
print(f"   Original words:    {original_words}")
print(f"   Transcribed words: {transcript_words}")
print(f"   Overlap: {len(overlap)}/{len(original_words)} words matched")

if len(overlap) >= len(original_words) // 2:
    print("\nAll ElevenLabs smoke tests passed.")
else:
    print(
        "\n[warn] Transcript overlap is low - STT may have misheard the "
        "TTS voice, or something is misconfigured. Listen to the file at "
        f"{audio_path} to check manually."
    )
