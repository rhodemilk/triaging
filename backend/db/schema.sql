-- Snowflake schema for the triage rover.
-- Table names are the *defaults* used by config.py (SNOWFLAKE_PATIENTS_TABLE /
-- SNOWFLAKE_SESSIONS_TABLE). If you rename them via env vars, update here too
-- (this file is documentation of intent; db/snowflake_client.py builds the
-- actual CREATE TABLE IF NOT EXISTS statements at runtime from config, so
-- the app will self-provision even if you never run this file by hand).

CREATE DATABASE IF NOT EXISTS TRIAGE_ROVER;
USE DATABASE TRIAGE_ROVER;
CREATE SCHEMA IF NOT EXISTS PUBLIC;
USE SCHEMA PUBLIC;

-- One row per patient. `nfc_uid` is filled in later once a physical NFC tag
-- is scanned and linked (step 12 of the pipeline) - it starts NULL.
CREATE TABLE IF NOT EXISTS PATIENTS (
    patient_id       VARCHAR       PRIMARY KEY,
    nfc_uid          VARCHAR       UNIQUE,
    current_status   VARCHAR       DEFAULT 'CHECKED_IN',
    triage_level     VARCHAR,
    triage_summary   VARCHAR,
    created_at       TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    updated_at       TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- One row per triage encounter (a single visit to the rover). A patient can
-- have many sessions over time. `transcript` and `image_analysis` are stored
-- as JSON-encoded strings (app-side json.dumps/json.loads) rather than
-- Snowflake VARIANT, to keep the Python side simple for a hackathon.
CREATE TABLE IF NOT EXISTS TRIAGE_SESSIONS (
    session_id       VARCHAR       PRIMARY KEY,
    patient_id       VARCHAR       NOT NULL,
    status           VARCHAR       DEFAULT 'IN_PROGRESS',
    transcript_json  VARCHAR,      -- [{"role": "user"|"assistant", "text": "..."}]
    image_analysis_json VARCHAR,   -- {"observations": [...], "possible_concern_level": "...", "notes": "..."}
    triage_state_json VARCHAR,     -- {"route", "confirm_attempts", "internal_severity",
                                    --  "red_flags", "lit_drawers", "escalated",
                                    --  "escalate_reason", "vitals"} - AMI turn state
    triage_level     VARCHAR,
    summary          VARCHAR,
    created_at       TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    updated_at       TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);
