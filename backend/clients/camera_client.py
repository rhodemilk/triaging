"""
Phase 5 test client: grab one frame from your laptop webcam, POST it to the
running Flask server's image-turn endpoint, and print Gemini's visible-
injury observations. Stands in for "the rover's camera" until real hardware
is wired up.

Usage (with backend/app.py already running in another terminal):

    python3 backend/clients/camera_client.py --patient-id PAT-XXXXXXXX
    python3 backend/clients/camera_client.py --session-id SES-XXXXXXXXXX
"""

import argparse
import os
import sys
import tempfile

import cv2
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402


def capture_frame(camera_index: int) -> str:
    cam = cv2.VideoCapture(camera_index)
    if not cam.isOpened():
        raise RuntimeError(f"Could not open camera index {camera_index}")

    # Warm up: first frame or two from a webcam is often dark/unfocused.
    for _ in range(5):
        cam.read()
    ok, frame = cam.read()
    cam.release()
    if not ok:
        raise RuntimeError("Failed to capture frame from camera")

    tmp_path = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False).name
    cv2.imwrite(tmp_path, frame)
    print(f"Captured photo to {tmp_path}")
    return tmp_path


def main():
    parser = argparse.ArgumentParser(description="Camera -> Flask image-turn test client")
    parser.add_argument(
        "--base-url",
        default=os.getenv("TRIAGE_API_BASE_URL", f"http://127.0.0.1:{config.FLASK_PORT}"),
    )
    parser.add_argument("--patient-id", default=None)
    parser.add_argument("--session-id", default=None)
    parser.add_argument(
        "--camera-index", type=int, default=int(os.getenv("CAMERA_INDEX", "0"))
    )
    args = parser.parse_args()

    session_id = args.session_id
    if not session_id:
        if not args.patient_id:
            parser.error("Provide --session-id or --patient-id")
        resp = requests.post(
            f"{args.base_url}/api/triage/session", json={"patient_id": args.patient_id}
        )
        resp.raise_for_status()
        session_id = resp.json()["session_id"]
        print(f"Started new session: {session_id}")

    image_path = capture_frame(args.camera_index)

    with open(image_path, "rb") as f:
        resp = requests.post(
            f"{args.base_url}/api/triage/session/{session_id}/image",
            files={"image": ("capture.jpg", f, "image/jpeg")},
        )
    os.remove(image_path)

    if not resp.ok:
        print("Request failed:", resp.status_code, resp.text)
        return

    print("\nGemini image analysis:")
    print(resp.json()["image_analysis"])
    print(f"\nsession_id: {session_id}")


if __name__ == "__main__":
    main()
