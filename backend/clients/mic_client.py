"""
Phase 4 test client: record N seconds from your laptop mic, POST it to the
running Flask server's voice-turn endpoint, and play back ElevenLabs'
spoken reply. This stands in for "the rover's microphone" until real
hardware is wired up.

Usage (with backend/app.py already running in another terminal):

    python3 backend/clients/mic_client.py --patient-id PAT-XXXXXXXX
    python3 backend/clients/mic_client.py --patient-id PAT-XXXXXXXX --session-id SES-XXXXXXXXXX
    python3 backend/clients/mic_client.py --patient-id PAT-XXXXXXXX --seconds 8

If --session-id is omitted, a new triage session is started automatically.
Every flag has a default pulled from env vars / config so nothing is
hardcoded to one laptop's setup.
"""

import argparse
import os
import subprocess
import sys
import tempfile

import requests
import sounddevice as sd
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402


def record_audio(seconds: float, samplerate: int) -> str:
    print(f"Recording for {seconds}s... speak now.")
    frames = sd.rec(int(seconds * samplerate), samplerate=samplerate, channels=1)
    sd.wait()
    tmp_path = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    sf.write(tmp_path, frames, samplerate)
    print(f"Saved recording to {tmp_path}")
    return tmp_path


def play_audio(path: str) -> None:
    # `afplay` ships with macOS - simplest zero-extra-dependency playback.
    # Swap this out if you're demoing from Linux/Windows.
    try:
        subprocess.run(["afplay", path], check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print(f"(Could not auto-play. Reply audio saved at: {path})")


def main():
    parser = argparse.ArgumentParser(description="Mic -> Flask voice-turn test client")
    parser.add_argument(
        "--base-url",
        default=os.getenv("TRIAGE_API_BASE_URL", f"http://127.0.0.1:{config.FLASK_PORT}"),
    )
    parser.add_argument("--patient-id", required=True)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--seconds", type=float, default=float(os.getenv("MIC_SECONDS", "5")))
    parser.add_argument(
        "--samplerate", type=int, default=int(os.getenv("MIC_SAMPLERATE", "16000"))
    )
    args = parser.parse_args()

    session_id = args.session_id
    if not session_id:
        resp = requests.post(
            f"{args.base_url}/api/triage/session", json={"patient_id": args.patient_id}
        )
        resp.raise_for_status()
        session_id = resp.json()["session_id"]
        print(f"Started new session: {session_id}")

    audio_path = record_audio(args.seconds, args.samplerate)

    with open(audio_path, "rb") as f:
        resp = requests.post(
            f"{args.base_url}/api/triage/session/{session_id}/voice",
            files={"audio": ("recording.wav", f, "audio/wav")},
        )
    os.remove(audio_path)

    if not resp.ok:
        print("Request failed:", resp.status_code, resp.text)
        return

    data = resp.json()
    print("\nYou said     :", data["transcript_text"])
    print("Rover replied:", data["reply_text"])

    audio_url = f"{args.base_url}{data['reply_audio_url']}"
    # Match the real file extension the server returned (wav by default,
    # but could be mp3/pcm depending on ELEVENLABS_OUTPUT_FORMAT) so
    # afplay knows how to decode it.
    reply_ext = os.path.splitext(data["reply_audio_url"])[1] or ".wav"
    reply_local_path = tempfile.NamedTemporaryFile(suffix=reply_ext, delete=False).name
    audio_resp = requests.get(audio_url)
    audio_resp.raise_for_status()
    with open(reply_local_path, "wb") as f:
        f.write(audio_resp.content)

    play_audio(reply_local_path)
    print(f"\nsession_id for next turn: {session_id}")


if __name__ == "__main__":
    main()
