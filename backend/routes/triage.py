"""
Triage pipeline routes - pipeline steps 7, 8, 9, 10, 11, plus the AMI
structured secondary-triage flow (intro/vitals/escalation/drawer routing).

  POST /api/triage/session                    -> start a session (patient_id OR nfc_uid, auto-creates patient)
  POST /api/triage/session/<id>/nfc-vitals    -> speak NFC/vitals prompt, optionally store vitals
  POST /api/triage/session/<id>/voice         -> mic audio in, AMI-decided spoken reply out
  POST /api/triage/session/<id>/image         -> camera photo in, AMI-decided spoken reply out
                                                  (analysis + reply_audio_url, same as /voice)
  POST /api/triage/session/<id>/escalate-emt  -> manually flag session for human staff
  POST /api/triage/session/<id>/light-drawers -> manually record which supply drawers to light
  POST /api/triage/session/<id>/complete      -> combine everything, close session
  GET  /api/triage/session/<id>               -> fetch full session state (incl. triage_state)

Voice turn flow (steps 8 -> 7 -> 9), all inside one request:
  1. Uploaded audio file -> ElevenLabs speech_to_text()        [step 8]
  2. Transcript + history + triage_state -> Gemini triage_reply() [step 7]
     Returns structured JSON (spoken_reply/route/action/drawers/escalate),
     not plain text - see services/gemini_service.py.
  3. spoken_reply -> ElevenLabs text_to_speech()                [step 9]
  4. Both turns appended to transcript; triage_state persisted; "action"/
     "escalate" auto-bridged into a real escalate_emt()/light_drawers()
     call (routes never let Gemini's text alone silently trigger nothing).

The /intro and /nfc-vitals steps intentionally skip Gemini entirely - no
judgment is needed for a fixed greeting or a sensor-handshake prompt, so
they're spoken directly from config.AMI_INTRO_LINE / AMI_NFC_VITALS_LINE.

Image turn flow (steps 10 -> 7 -> 9), same shape as the voice turn above:
  1. Uploaded photo -> Gemini analyze_injury_image()             [step 10]
  2. The photo moment is logged as a transcript turn, then that same
     analysis is fed into Gemini triage_reply() via triage_state -
     the ONE AMI decision engine voice_turn() also uses, so escalation/
     confirm/drawers rules are identical no matter whether the input was
     speech or a photo.                                           [step 7]
  3. spoken_reply -> ElevenLabs text_to_speech()                  [step 9]
This is what lets Amy automatically say something the instant she "looks"
at an injury, instead of waiting for a separate /voice call.
"""

import mimetypes
import os

from flask import Blueprint, current_app, jsonify, request, send_file

import config
from db import snowflake_client as db
from services import elevenlabs_service, gemini_service

triage_bp = Blueprint("triage", __name__, url_prefix="/api/triage")


@triage_bp.post("/session")
def start_session():
    """Begin a new triage encounter.

    Body (JSON), one of:
      { "patient_id": "PAT-XXXXXXXX" }  -> patient must already exist
      { "nfc_uid": "ABC123" }           -> auto-creates the patient if this
                                            tag has never been seen before,
                                            so a brand-new encounter is ONE
                                            call instead of two (create
                                            patient, then create session).
    """
    body = request.get_json(silent=True) or {}
    patient_id = body.get("patient_id")
    nfc_uid = body.get("nfc_uid")

    if patient_id:
        if not db.get_patient(patient_id):
            return jsonify({"error": "patient not found"}), 404
    elif nfc_uid:
        patient = db.get_or_create_patient_by_nfc(nfc_uid)
        patient_id = patient["patient_id"]
    else:
        return jsonify({"error": "patient_id or nfc_uid is required"}), 400

    session = db.create_session(patient_id)
    db.update_patient(patient_id, current_status="IN_TRIAGE")
    return jsonify(session), 201


@triage_bp.get("/session/<session_id>")
def get_session(session_id):
    session = db.get_session(session_id)
    if not session:
        return jsonify({"error": "session not found"}), 404
    return jsonify(session), 200


def _speak_fixed_line(session_id: str, text: str, reply_dir: str) -> dict:
    """Shared helper for the fixed-script steps (intro, NFC/vitals) that
    don't need a Gemini call - just speak a config line and log it as an
    assistant turn so it shows up in the transcript like everything else.
    """
    session = db.append_transcript_turn(session_id, "assistant", text)
    reply_audio_path = elevenlabs_service.text_to_speech(text, reply_dir)
    reply_audio_filename = os.path.basename(reply_audio_path)
    return {
        "session_id": session_id,
        "reply_text": text,
        "reply_audio_url": f"/api/triage/audio/{reply_audio_filename}",
        "transcript": session["transcript"],
    }




@triage_bp.post("/session/<session_id>/intro")
def intro_turn(session_id):
    

    session = db.get_session(session_id)
    if not session:
        return jsonify({"error": "session not found"}), 404

    reply_dir = current_app.config["AUDIO_REPLY_DIR"]
    result = _speak_fixed_line(session_id, config.AMI_INTRO_LINE, reply_dir)
    return jsonify(result), 200


@triage_bp.post("/session/<session_id>/nfc-vitals")
def nfc_vitals_turn(session_id):
    """Fixed NFC-tap + baseline-vitals prompt - no Gemini call.

    Optional JSON body once the hardware has a reading:
      { "pulse_ox": 98, "heart_rate": 72 }
    If provided, it's stored on the session's triage_state so later AMI
    turns (and any dashboard) can see it.
    """
    session = db.get_session(session_id)
    if not session:
        return jsonify({"error": "session not found"}), 404

    body = request.get_json(silent=True) or {}
    vitals = {k: body[k] for k in ("pulse_ox", "heart_rate") if k in body}
    if vitals:
        db.update_triage_state(session_id, {"vitals": vitals})

    reply_dir = current_app.config["AUDIO_REPLY_DIR"]
    result = _speak_fixed_line(session_id, config.AMI_NFC_VITALS_LINE, reply_dir)
    result["vitals"] = vitals or None
    return jsonify(result), 200


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

        # Step 7: transcript + history + current AMI state -> Gemini's next
        # move. `result` is a structured dict (spoken_reply/route/action/
        # drawers/escalate/...), NOT plain text - see gemini_service.
        result = gemini_service.triage_reply(
            user_text=transcript_text,
            transcript=session["transcript"][:-1],  # history BEFORE this turn
            # Include the photo analysis (if any) so Gemini can reference
            # real observations once a camera photo has been taken mid-
            # conversation, instead of guessing from the transcript alone.
            triage_state={
                **session["triage_state"],
                "image_analysis": session["image_analysis"],
            },
        )
        reply_text = result["spoken_reply"]

        session = db.append_transcript_turn(session_id, "assistant", reply_text)

        # Persist the AMI state Gemini just decided on (route, attempts,
        # severity, red flags) so the next turn has accurate CURRENT_STATE.
        session = db.update_triage_state(
            session_id,
            {
                "route": result["route"],
                "confirm_attempts": result["confirm_attempts"],
                "internal_severity": result["internal_severity"],
                "red_flags": result["red_flags"],
            },
        )

        # Auto-bridge Gemini's decision into an actual backend effect.
        # gemini_service never touches Snowflake/hardware itself - this is
        # the one place that turns "action": "escalate_emt" into a real
        # escalate_emt() call, same for lighting drawers.
        if result["escalate"] or result["action"] == "escalate_emt":
            session = db.escalate_emt(session_id, reason=result.get("suspected_injury") or "")
        if result["action"] == "light_drawers" and result["drawers"]:
            session = db.light_drawers(session_id, result["drawers"])

        # Step 9: Gemini's spoken reply -> audio
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
                # Additive fields for the AMI hardware layer - existing
                # consumers that only read the four keys above are
                # unaffected.
                "route": result["route"],
                "action": result["action"],
                "suspected_injury": result["suspected_injury"],
                "drawers": result["drawers"],
                # Which physical LED(s) to light for those drawer ids, per
                # config.DRAWER_LED_MAP - saves the cabinet controller a
                # second lookup call for the common case.
                "leds": config.drawers_to_leds(result["drawers"]),
                "escalate": result["escalate"],
                "triage_state": session["triage_state"],
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


# Maps gemini_service.image_care_decision()'s 3-way "decision" straight onto
# the same action/escalate vocabulary voice_turn() already exposes, so
# existing consumers (rover/hardware, per API.md) don't need to learn a new
# field - they just see action="escalate_emt"/"light_drawers"/"none" like
# always.
_IMAGE_DECISION_TO_ACTION = {
    "nurse": "escalate_emt",
    "supplies": "light_drawers",
    "clear": "none",
}


@triage_bp.post("/session/<session_id>/image")
def image_turn(session_id):
    """Step 10 -> 10.5 -> 9: camera photo -> Gemini visible-injury analysis
    -> a lean nurse/supplies/clear decision -> AMY speaks it, all in one
    request, so Amy reacts the instant she "looks" at the injury.

      1. Photo -> Gemini vision -> `analysis` dict                 [step 10]
      2. `analysis` (ONLY - no transcript, no CURRENT_STATE) -> Gemini
         image_care_decision(): exactly ONE of nurse / supplies / clear,
         plus which drawers if any. Deliberately a separate, much smaller
         prompt than the full AMI conversational rulebook (see
         config.GEMINI_IMAGE_TRIAGE_PROMPT) - keeps this call cheap and
         fast instead of re-running the whole multi-turn state machine
         just to react to a single photo.                       [step 10.5]
      3. Auto-bridge into a real backend effect, same pattern as
         voice_turn(): "nurse" -> real escalate_emt(); "supplies" -> real
         light_drawers() for the returned drawer ids.
      4. spoken_reply -> ElevenLabs audio, same as every other turn. [step 9]

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

    # Step 10: photo -> visible-injury observations (vision call).
    analysis = gemini_service.analyze_injury_image(image_path)
    session = db.save_image_analysis(session_id, analysis)

    # Log the photo moment in the transcript for a human-readable history,
    # even though (unlike voice_turn) the decision call right below doesn't
    # itself read the transcript - keeps this cheap/lean on purpose.
    photo_note = "[Patient held the injury up to the camera.]"
    session = db.append_transcript_turn(session_id, "user", photo_note)

    reply_dir = current_app.config["AUDIO_REPLY_DIR"]

    # Step 10.5: lean, image-only decision - nurse vs. supplies vs. clear.
    decision_result = gemini_service.image_care_decision(analysis)
    decision = decision_result["decision"]
    action = _IMAGE_DECISION_TO_ACTION[decision]
    escalate = decision == "nurse"
    drawers = decision_result["drawers"]
    reply_text = decision_result["spoken_reply"]

    session = db.append_transcript_turn(session_id, "assistant", reply_text)

    # Persist the decision so it shows up alongside the rest of the AMI
    # state (route/red_flags/etc.) for any dashboard reading triage_state.
    session = db.update_triage_state(
        session_id,
        {
            "route": "external",
            "care_decision": decision,
            "care_decision_reason": decision_result["reason"],
        },
    )

    # Auto-bridge the decision into a real backend effect - same pattern as
    # voice_turn(), so a photo alone can trigger escalation or drawers
    # without needing a follow-up voice exchange first.
    if escalate:
        session = db.escalate_emt(
            session_id, reason=decision_result["reason"] or "Image analysis flagged for nurse review."
        )
    if decision == "supplies" and drawers:
        session = db.light_drawers(session_id, drawers)

    # Step 9: Gemini's spoken reply -> audio, so Amy actually says it.
    reply_audio_path = elevenlabs_service.text_to_speech(reply_text, reply_dir)
    reply_audio_filename = os.path.basename(reply_audio_path)

    return (
        jsonify(
            {
                "session_id": session_id,
                "image_analysis": analysis,
                "reply_text": reply_text,
                "reply_audio_url": f"/api/triage/audio/{reply_audio_filename}",
                "transcript": session["transcript"],
                # Additive fields, same shape as voice_turn()'s response.
                "route": "external",
                "action": action,
                "care_decision": decision,
                "suspected_injury": (analysis.get("observations") or [None])[0],
                "drawers": drawers,
                "leds": config.drawers_to_leds(drawers),
                "escalate": escalate,
                "triage_state": session["triage_state"],
            }
        ),
        200,
    )


@triage_bp.post("/session/<session_id>/escalate-emt")
def escalate_emt_turn(session_id):
    """Manually flag a session for human staff attention.

    Normally this fires automatically from voice_turn() when Gemini's
    triage_reply() sets escalate=true, but it's also exposed directly so a
    nurse-call button, a nurse/EMT dashboard, or a manual override can
    trigger it without going through a voice exchange.

    Optional JSON body: { "reason": "..." }

    Stubbed for now - no real pager/hardware integration, just persists
    state (triage_state.escalated) and bumps the patient's current_status.
    """
    if not db.get_session(session_id):
        return jsonify({"error": "session not found"}), 404

    body = request.get_json(silent=True) or {}
    session = db.escalate_emt(session_id, reason=body.get("reason", ""))
    return jsonify(session), 200


@triage_bp.post("/session/<session_id>/light-drawers")
def light_drawers_turn(session_id):
    """Manually record which supply drawers should light up for this
    session. Normally fires automatically from voice_turn() when Gemini's
    triage_reply() sets action="light_drawers", but also exposed directly
    for manual testing or a future cabinet-controller poll loop.

    JSON body: { "drawers": ["1-2", "2-3"] }  (see the cabinet directory in
    config.GEMINI_AMI_SYSTEM_PROMPT for valid "<container>-<cabinet>" ids)

    Stubbed for now - no real cabinet hardware wired in, just persists
    triage_state.lit_drawers for whatever hardware layer polls it later.
    """
    if not db.get_session(session_id):
        return jsonify({"error": "session not found"}), 404

    body = request.get_json(silent=True) or {}
    drawers = body.get("drawers")
    if not drawers or not isinstance(drawers, list):
        return jsonify({"error": "'drawers' (non-empty list) is required"}), 400

    session = db.light_drawers(session_id, drawers)
    return jsonify({**session, "leds": config.drawers_to_leds(drawers)}), 200


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
