"""
Triage pipeline routes - pipeline steps 7, 8, 9, 10, 11.

  POST /api/triage/session                 -> start a new session for a patient
  POST /api/triage/session/<id>/voice      -> mic audio in, spoken reply out
  POST /api/triage/session/<id>/image      -> camera photo in, JSON analysis out
  POST /api/triage/session/<id>/complete   -> combine everything, close session
  GET  /api/triage/session/<id>            -> fetch full session state

Voice turn flow (steps 8 -> 7 -> 9), all inside one request:
  1. Uploaded audio file -> ElevenLabs speech_to_text()      [step 8]
  2. Transcript + prior turns -> Gemini triage_reply()       [step 7]
  3. Gemini's reply text -> ElevenLabs text_to_speech()      [step 9]
  4. Both turns appended to the session's transcript in Snowflake.
"""

import mimetypes
import os

from flask import Blueprint, current_app, jsonify, request, send_file

from db import snowflake_client as db
from services import elevenlabs_service, gemini_service

triage_bp = Blueprint("triage", __name__, url_prefix="/api/triage")


@triage_bp.post("/session")
def start_session():
    """Begin a new triage encounter for an existing patient."""
    body = request.get_json(silent=True) or {}
    patient_id = body.get("patient_id")
    if not patient_id:
        return jsonify({"error": "patient_id is required"}), 400
    if not db.get_patient(patient_id):
        return jsonify({"error": "patient not found"}), 404

    session = db.create_session(patient_id)
    db.update_patient(patient_id, current_status="IN_TRIAGE")
    return jsonify(session), 201


@triage_bp.get("/session/<session_id>")
def get_session(session_id):
    session = db.get_session(session_id)
    if not session:
        return jsonify({"error": "session not found"}), 404
    return jsonify(session), 200


@triage_bp.post("/session/<session_id>/voice")
def voice_turn(session_id):
    """One back-and-forth voice exchange: mic audio in, spoken reply out.

    multipart/form-data:
      audio: the recorded patient audio file (wav/mp3/m4a/webm...)
    """
    session = db.get_session(session_id)
    if not session:
        return jsonify({"error": "session not found"}), 404

    if "audio" not in request.files:
        return jsonify({"error": "multipart field 'audio' is required"}), 400
    audio_file = request.files["audio"]

    upload_dir = current_app.config["AUDIO_UPLOAD_DIR"]
    reply_dir = current_app.config["AUDIO_REPLY_DIR"]
    os.makedirs(upload_dir, exist_ok=True)

    upload_path = os.path.join(upload_dir, f"in-{session_id}-{audio_file.filename}")
    audio_file.save(upload_path)

    try:
        # Step 8: microphone audio -> transcript text
        transcript_text = elevenlabs_service.speech_to_text(upload_path)
        if not transcript_text:
            return jsonify({"error": "could not transcribe audio (empty result)"}), 422

        session = db.append_transcript_turn(session_id, "user", transcript_text)

        # Step 7: transcript + history -> Gemini's next question/response
        reply_text = gemini_service.triage_reply(
            user_text=transcript_text,
            transcript=session["transcript"][:-1],  # history BEFORE this turn
        )

        session = db.append_transcript_turn(session_id, "assistant", reply_text)

        # Step 9: Gemini's reply text -> spoken audio
        reply_audio_path = elevenlabs_service.text_to_speech(reply_text, reply_dir)
        reply_audio_filename = os.path.basename(reply_audio_path)

    finally:
        # Uploaded mic audio is transient - clean it up after transcription.
        if os.path.exists(upload_path):
            os.remove(upload_path)

    return (
        jsonify(
            {
                "session_id": session_id,
                "transcript_text": transcript_text,
                "reply_text": reply_text,
                "reply_audio_url": f"/api/triage/audio/{reply_audio_filename}",
                "transcript": session["transcript"],
            }
        ),
        200,
    )


@triage_bp.get("/audio/<filename>")
def get_reply_audio(filename):
    """Serve a generated ElevenLabs reply clip back to whatever is playing
    audio out loud (browser, rover speaker client, curl -o, etc.).

    Content-Type is derived from the actual file extension rather than
    hardcoded, since ELEVENLABS_OUTPUT_FORMAT controls whether this is a
    .wav (default), .mp3, or raw .pcm file.
    """
    reply_dir = current_app.config["AUDIO_REPLY_DIR"]
    path = os.path.join(reply_dir, filename)
    if not os.path.exists(path):
        return jsonify({"error": "audio not found"}), 404
    mimetype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return send_file(path, mimetype=mimetype)


@triage_bp.post("/session/<session_id>/image")
def image_turn(session_id):
    """Step 10: camera photo -> Gemini visible-injury analysis.

    multipart/form-data:
      image: a jpg/png photo
    """
    session = db.get_session(session_id)
    if not session:
        return jsonify({"error": "session not found"}), 404

    if "image" not in request.files:
        return jsonify({"error": "multipart field 'image' is required"}), 400
    image_file = request.files["image"]

    image_dir = current_app.config["IMAGE_UPLOAD_DIR"]
    os.makedirs(image_dir, exist_ok=True)
    image_path = os.path.join(image_dir, f"{session_id}-{image_file.filename}")
    image_file.save(image_path)

    analysis = gemini_service.analyze_injury_image(image_path)
    session = db.save_image_analysis(session_id, analysis)

    return jsonify({"session_id": session_id, "image_analysis": analysis}), 200


@triage_bp.post("/session/<session_id>/complete")
def complete_session(session_id):
    """Step 11: combine the voice transcript + image analysis into one
    final triage summary, save it, and update the patient's latest status.
    """
    session = db.get_session(session_id)
    if not session:
        return jsonify({"error": "session not found"}), 404

    result = gemini_service.summarize_session(
        transcript=session["transcript"],
        image_analysis=session["image_analysis"],
    )

    session = db.complete_session(
        session_id, summary=result["summary"], triage_level=result["triage_level"]
    )
    db.update_patient(
        session["patient_id"],
        current_status="TRIAGE_COMPLETE",
        triage_level=result["triage_level"],
        triage_summary=result["summary"],
    )

    return jsonify(session), 200
