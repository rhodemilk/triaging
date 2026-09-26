"""
Phase 1 smoke test: confirm Snowflake connectivity + schema + basic CRUD
work BEFORE wiring anything else on top. Run from the `backend/` folder:

    python3 test_snowflake.py

Does NOT touch Gemini or ElevenLabs - just proves the DB layer.
"""

from db import snowflake_client as db

print("1) Initializing schema (create-if-not-exists)...")
db.init_schema()
print("   OK")

print("2) Creating a test patient...")
patient = db.create_patient(nfc_uid="TEST-NFC-0001")
print("   ", patient)

print("3) Fetching patient by id...")
fetched = db.get_patient(patient["patient_id"])
print("   ", fetched)

print("4) Fetching patient by NFC uid...")
by_nfc = db.get_patient_by_nfc("TEST-NFC-0001")
print("   ", by_nfc)

print("5) Creating a triage session for this patient...")
session = db.create_session(patient["patient_id"])
print("   ", session)

print("6) Appending a transcript turn...")
session = db.append_transcript_turn(session["session_id"], "user", "My arm hurts.")
print("   ", session["transcript"])

print("7) Completing the session...")
completed = db.complete_session(
    session["session_id"], summary="Patient reports arm pain.", triage_level="3"
)
print("   ", completed)

print("\nAll Snowflake smoke tests passed.")
