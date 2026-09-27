"""
One-command scripted demo run: chains every step of the AMI pipeline
(session -> intro -> vitals -> a scripted "conversation" -> optional photo
-> complete) with zero manual curl typing and zero real hardware required.

This exists as a stage-fallback: if the ESP32/mic/speaker hardware has
issues during a live demo, this script proves the whole backend (Snowflake
+ Gemini + ElevenLabs) still works end-to-end, using synthetic "patient"
audio generated locally via ElevenLabs TTS instead of a real microphone.

Usage (with backend/app.py already running in another terminal):

    python3 backend/demo_run.py
    python3 backend/demo_run.py --scenario internal_severe
    python3 backend/demo_run.py --scenario external --image-path photo.jpg
    python3 backend/demo_run.py --line "My arm really hurts" --line "It's a 4 out of 10"
    python3 backend/demo_run.py --no-play   # print everything, skip afplay

Every scenario/tuning knob is a flag (with an env-var-backed default via
config.py where relevant) - nothing about which demo runs is hardcoded.
"""

import argparse
import os
import subprocess
import sys
import tempfile
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
from services import elevenlabs_service  # noqa: E402

# Built-in scripted "patient" lines for a few representative branches of
# the AMI flow. Override any of these on the command line with --line
# (repeatable) if you want a custom scripted conversation instead.
DEMO_SCENARIOS = {
    "external": [
        "Hi, I cut my hand on some glass and it's bleeding a little bit.",
        "Yes, that's right, it is a cut on my hand.",
    ],
    "internal_mild": [
        "I have a dull ache in my stomach, maybe a 3 out of 10. No other symptoms.",
        "I'll just stay here so you can keep monitoring me, thanks.",
    ],
    "internal_severe": [
        "I have severe chest pressure and I feel really dizzy. The pain is like a 9 out of 10.",
    ],
}

DEMO_NFC_UID = os.getenv("DEMO_NFC_UID", "DEMO-TAG-1")
DEMO_PULSE_OX = int(os.getenv("DEMO_PULSE_OX", "98"))
DEMO_HEART_RATE = int(os.getenv("DEMO_HEART_RATE", "76"))
DEMO_STEP_PAUSE = float(os.getenv("DEMO_STEP_PAUSE", "0.5"))


def play_audio(path: str, enabled: bool) -> None:
    if not enabled:
        return
    try:
        subprocess.run(["afplay", path], check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print(f"  (could not auto-play; reply saved at {path})")


def fetch_and_play(base_url: str, reply_audio_url: str, play: bool) -> None:
    audio_url = f"{base_url}{reply_audio_url}"
    ext = os.path.splitext(reply_audio_url)[1] or ".wav"
    local_path = tempfile.NamedTemporaryFile(suffix=ext, delete=False).name
    resp = requests.get(audio_url)
    resp.raise_for_status()
    with open(local_path, "wb") as f:
        f.write(resp.content)
    play_audio(local_path, play)


def synth_patient_audio(text: str, tmp_dir: str) -> str:
    """Generate a fake 'patient' recording via ElevenLabs TTS, standing in
    for a real microphone so this script needs no hardware to run.
    """
    return elevenlabs_service.text_to_speech(text, tmp_dir)


def main():
    parser = argparse.ArgumentParser(description="Scripted end-to-end AMI demo run")
    parser.add_argument(
        "--base-url",
        default=os.getenv("TRIAGE_API_BASE_URL", f"http://127.0.0.1:{config.FLASK_PORT}"),
    )
    parser.add_argument("--nfc-uid", default=DEMO_NFC_UID)
    parser.add_argument(
        "--scenario",
        choices=sorted(DEMO_SCENARIOS.keys()),
        default=os.getenv("DEMO_SCENARIO", "external"),
    )
    parser.add_argument(
        "--line",
        action="append",
        dest="lines",
        default=None,
        help="Override the scenario with custom patient line(s) (repeatable)",
    )
    parser.add_argument(
        "--image-path",
        default=None,
        help="If given, POSTs this photo to /image after the first voice turn",
    )
    parser.add_argument("--pulse-ox", type=int, default=DEMO_PULSE_OX)
    parser.add_argument("--heart-rate", type=int, default=DEMO_HEART_RATE)
    parser.add_argument("--no-play", action="store_true", help="Skip afplay, just print")
    parser.add_argument("--pause", type=float, default=DEMO_STEP_PAUSE)
    args = parser.parse_args()

    play = not args.no_play
    lines = args.lines or DEMO_SCENARIOS[args.scenario]

    print(f"=== AMI demo run ({args.scenario if not args.lines else 'custom lines'}) ===")

    # Step 1 (collapsed): create patient (if new) + session in ONE call.
    session_resp = requests.post(
        f"{args.base_url}/api/triage/session", json={"nfc_uid": args.nfc_uid}
    )
    session_resp.raise_for_status()
    session = session_resp.json()
    session_id = session["session_id"]
    print(f"session_id={session_id}  patient_id={session['patient_id']}")
    time.sleep(args.pause)

    # Step 2: fixed intro line, no Gemini call.
    print("\n--- intro ---")
    intro_resp = requests.post(f"{args.base_url}/api/triage/session/{session_id}/intro")
    intro_resp.raise_for_status()
    intro_data = intro_resp.json()
    print(intro_data["reply_text"])
    fetch_and_play(args.base_url, intro_data["reply_audio_url"], play)
    time.sleep(args.pause)

    # Step 3: fixed NFC/vitals line, no Gemini call.
    print("\n--- nfc-vitals ---")
    vitals_resp = requests.post(
        f"{args.base_url}/api/triage/session/{session_id}/nfc-vitals",
        json={"pulse_ox": args.pulse_ox, "heart_rate": args.heart_rate},
    )
    vitals_resp.raise_for_status()
    vitals_data = vitals_resp.json()
    print(vitals_data["reply_text"])
    fetch_and_play(args.base_url, vitals_data["reply_audio_url"], play)
    time.sleep(args.pause)

    # Step 4: the scripted "conversation" - each line is synthesized into
    # fake patient audio, then run through the real /voice pipeline.
    image_sent = False
    with tempfile.TemporaryDirectory() as tmp_dir:
        for i, line in enumerate(lines, start=1):
            print(f"\n--- voice turn {i} ---")
            print(f"(patient says): {line}")
            audio_path = synth_patient_audio(line, tmp_dir)

            with open(audio_path, "rb") as f:
                voice_resp = requests.post(
                    f"{args.base_url}/api/triage/session/{session_id}/voice",
                    files={"audio": (os.path.basename(audio_path), f, "audio/wav")},
                )
            if not voice_resp.ok:
                print("  voice turn failed:", voice_resp.status_code, voice_resp.text)
                continue
            data = voice_resp.json()
            print(f"AMI: {data['reply_text']}")
            print(
                f"  route={data['route']} action={data['action']} "
                f"escalate={data['escalate']} drawers={data['drawers']}"
            )
            fetch_and_play(args.base_url, data["reply_audio_url"], play)

            # If AMI asked for a camera and we have a demo photo, send it once.
            if data["action"] == "request_camera" and args.image_path and not image_sent:
                print("\n--- image ---")
                with open(args.image_path, "rb") as img_f:
                    image_resp = requests.post(
                        f"{args.base_url}/api/triage/session/{session_id}/image",
                        files={"image": (os.path.basename(args.image_path), img_f, "image/jpeg")},
                    )
                if image_resp.ok:
                    print(image_resp.json()["image_analysis"])
                    image_sent = True
                else:
                    print("  image turn failed:", image_resp.status_code, image_resp.text)

            time.sleep(args.pause)

    # Step 5: close out the encounter.
    print("\n--- complete ---")
    complete_resp = requests.post(
        f"{args.base_url}/api/triage/session/{session_id}/complete"
    )
    if complete_resp.ok:
        completed = complete_resp.json()
        print(f"summary: {completed['summary']}")
        print(f"triage_level: {completed['triage_level']}")
    else:
        print("  complete failed:", complete_resp.status_code, complete_resp.text)

    print(f"\nsession_id: {session_id}")


if __name__ == "__main__":
    main()
