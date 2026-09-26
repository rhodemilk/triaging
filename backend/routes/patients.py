"""
Patient record routes - pipeline steps 5, 6, 12.

Kept deliberately dumb: create/fetch a row, nothing clinical happens here.
The actual triage logic lives in routes/triage.py.
"""

from flask import Blueprint, jsonify, request

from db import snowflake_client as db

patients_bp = Blueprint("patients", __name__, url_prefix="/api/patients")


@patients_bp.post("")
def create_or_check_patient():
    """Step 5: Flask creates/checks a patient record.

    Body (all optional JSON):
      { "nfc_uid": "ABC123" }

    - If `nfc_uid` is given and already known -> returns the existing patient.
    - If `nfc_uid` is given and unknown -> creates a new patient linked to it.
    - If `nfc_uid` is omitted -> creates a brand new, unlinked patient (NFC
      tag can be linked later via PATCH /api/patients/<id>/nfc).
    """
    body = request.get_json(silent=True) or {}
    nfc_uid = body.get("nfc_uid")

    if nfc_uid:
        patient = db.get_or_create_patient_by_nfc(nfc_uid)
    else:
        patient = db.create_patient()

    return jsonify(patient), 200


@patients_bp.get("/<patient_id>")
def get_patient(patient_id):
    patient = db.get_patient(patient_id)
    if not patient:
        return jsonify({"error": "patient not found"}), 404
    return jsonify(patient), 200


@patients_bp.get("/by-nfc/<nfc_uid>")
def get_patient_by_nfc(nfc_uid):
    """Step 12: scan NFC later -> retrieve that patient's latest record
    (patient row + their most recent triage session, if any).
    """
    patient = db.get_patient_by_nfc(nfc_uid)
    if not patient:
        return jsonify({"error": "no patient linked to this nfc_uid"}), 404

    latest_session = db.get_latest_session_for_patient(patient["patient_id"])
    return jsonify({"patient": patient, "latest_session": latest_session}), 200


@patients_bp.patch("/<patient_id>/nfc")
def link_nfc(patient_id):
    """Link a physical NFC tag to an existing patient record."""
    body = request.get_json(silent=True) or {}
    nfc_uid = body.get("nfc_uid")
    if not nfc_uid:
        return jsonify({"error": "nfc_uid is required"}), 400

    if not db.get_patient(patient_id):
        return jsonify({"error": "patient not found"}), 404

    db.link_nfc_to_patient(patient_id, nfc_uid)
    return jsonify(db.get_patient(patient_id)), 200
