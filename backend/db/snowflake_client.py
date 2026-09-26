"""
Snowflake access layer.

Every table/column name that might reasonably change lives in config.py, not
hardcoded here. Connection is opened lazily and reused (module-level cache)
so every request doesn't pay the Snowflake handshake cost.

Design choice: transcript + image analysis are stored as JSON *strings* in
plain VARCHAR columns instead of Snowflake VARIANT. This avoids PARSE_JSON /
TO_VARIANT ceremony in every query and keeps this file readable during a
hackathon. Swap to VARIANT later if you need real JSON querying in Snowflake.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

import snowflake.connector

import config

_connection = None


def get_connection():
    """Return a cached Snowflake connection, opening one if needed."""
    global _connection
    if _connection is not None and not _connection.is_closed():
        return _connection

    missing = config.require(
        "SNOWFLAKE_USER",
        "SNOWFLAKE_PASSWORD",
        "SNOWFLAKE_ACCOUNT",
        "SNOWFLAKE_WAREHOUSE",
    )
    if missing:
        raise RuntimeError(
            f"Missing Snowflake config: {', '.join(missing)}. "
            "Fill these in backend/.env (see .env.example)."
        )

    connect_kwargs = dict(
        user=config.SNOWFLAKE_USER,
        password=config.SNOWFLAKE_PASSWORD,
        account=config.SNOWFLAKE_ACCOUNT,
        warehouse=config.SNOWFLAKE_WAREHOUSE,
        database=config.SNOWFLAKE_DATABASE,
        schema=config.SNOWFLAKE_SCHEMA,
    )
    if config.SNOWFLAKE_ROLE:
        connect_kwargs["role"] = config.SNOWFLAKE_ROLE

    _connection = snowflake.connector.connect(**connect_kwargs)
    return _connection


def init_schema() -> None:
    """Create the database/schema/tables if they don't exist yet.

    Safe to call every time the app starts - all statements are idempotent.
    Table/column names come from config so renaming via env vars actually
    takes effect.
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS {config.SNOWFLAKE_DATABASE}")
        cur.execute(f"USE DATABASE {config.SNOWFLAKE_DATABASE}")
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {config.SNOWFLAKE_SCHEMA}")
        cur.execute(f"USE SCHEMA {config.SNOWFLAKE_SCHEMA}")

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {config.PATIENTS_TABLE} (
                patient_id       VARCHAR       PRIMARY KEY,
                nfc_uid          VARCHAR       UNIQUE,
                current_status   VARCHAR       DEFAULT 'CHECKED_IN',
                triage_level     VARCHAR,
                triage_summary   VARCHAR,
                created_at       TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
                updated_at       TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
            )
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {config.SESSIONS_TABLE} (
                session_id          VARCHAR       PRIMARY KEY,
                patient_id          VARCHAR       NOT NULL,
                status              VARCHAR       DEFAULT 'IN_PROGRESS',
                transcript_json     VARCHAR,
                image_analysis_json VARCHAR,
                triage_level        VARCHAR,
                summary             VARCHAR,
                created_at          TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
                updated_at          TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
            )
            """
        )
    finally:
        cur.close()


def _row_to_patient(row) -> dict:
    return {
        "patient_id": row[0],
        "nfc_uid": row[1],
        "current_status": row[2],
        "triage_level": row[3],
        "triage_summary": row[4],
        "created_at": str(row[5]) if row[5] else None,
        "updated_at": str(row[6]) if row[6] else None,
    }


PATIENT_COLUMNS = (
    "patient_id, nfc_uid, current_status, triage_level, triage_summary, "
    "created_at, updated_at"
)


# ---------------------------------------------------------------------------
# Patients (step 5 / 6 / 12)
# ---------------------------------------------------------------------------
def create_patient(nfc_uid: Optional[str] = None) -> dict:
    """Create a brand new patient record and return it as a dict."""
    conn = get_connection()
    cur = conn.cursor()
    patient_id = f"PAT-{uuid.uuid4().hex[:8].upper()}"
    try:
        cur.execute(
            f"""
            INSERT INTO {config.PATIENTS_TABLE}
                (patient_id, nfc_uid, current_status)
            VALUES (%s, %s, %s)
            """,
            (patient_id, nfc_uid, "CHECKED_IN"),
        )
    finally:
        cur.close()
    return get_patient(patient_id)


def get_patient(patient_id: str) -> Optional[dict]:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            f"SELECT {PATIENT_COLUMNS} FROM {config.PATIENTS_TABLE} "
            "WHERE patient_id = %s",
            (patient_id,),
        )
        row = cur.fetchone()
    finally:
        cur.close()
    return _row_to_patient(row) if row else None


def get_patient_by_nfc(nfc_uid: str) -> Optional[dict]:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            f"SELECT {PATIENT_COLUMNS} FROM {config.PATIENTS_TABLE} "
            "WHERE nfc_uid = %s",
            (nfc_uid,),
        )
        row = cur.fetchone()
    finally:
        cur.close()
    return _row_to_patient(row) if row else None


def get_or_create_patient_by_nfc(nfc_uid: str) -> dict:
    """Step 12: scan NFC -> return existing patient, or create one if this
    tag has never been seen before."""
    existing = get_patient_by_nfc(nfc_uid)
    if existing:
        return existing
    return create_patient(nfc_uid=nfc_uid)


def link_nfc_to_patient(patient_id: str, nfc_uid: str) -> None:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            f"UPDATE {config.PATIENTS_TABLE} SET nfc_uid = %s, "
            "updated_at = CURRENT_TIMESTAMP() WHERE patient_id = %s",
            (nfc_uid, patient_id),
        )
    finally:
        cur.close()


def update_patient(
    patient_id: str,
    current_status: Optional[str] = None,
    triage_level: Optional[str] = None,
    triage_summary: Optional[str] = None,
) -> Optional[dict]:
    sets, params = [], []
    if current_status is not None:
        sets.append("current_status = %s")
        params.append(current_status)
    if triage_level is not None:
        sets.append("triage_level = %s")
        params.append(triage_level)
    if triage_summary is not None:
        sets.append("triage_summary = %s")
        params.append(triage_summary)
    if not sets:
        return get_patient(patient_id)

    sets.append("updated_at = CURRENT_TIMESTAMP()")
    params.append(patient_id)

    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            f"UPDATE {config.PATIENTS_TABLE} SET {', '.join(sets)} "
            "WHERE patient_id = %s",
            tuple(params),
        )
    finally:
        cur.close()
    return get_patient(patient_id)


# ---------------------------------------------------------------------------
# Triage sessions (step 7 / 8 / 9 / 10 / 11)
# ---------------------------------------------------------------------------
def _row_to_session(row) -> dict:
    return {
        "session_id": row[0],
        "patient_id": row[1],
        "status": row[2],
        "transcript": json.loads(row[3]) if row[3] else [],
        "image_analysis": json.loads(row[4]) if row[4] else None,
        "triage_level": row[5],
        "summary": row[6],
        "created_at": str(row[7]) if row[7] else None,
        "updated_at": str(row[8]) if row[8] else None,
    }


SESSION_COLUMNS = (
    "session_id, patient_id, status, transcript_json, image_analysis_json, "
    "triage_level, summary, created_at, updated_at"
)


def create_session(patient_id: str) -> dict:
    conn = get_connection()
    cur = conn.cursor()
    session_id = f"SES-{uuid.uuid4().hex[:10]}"
    try:
        cur.execute(
            f"""
            INSERT INTO {config.SESSIONS_TABLE}
                (session_id, patient_id, status, transcript_json)
            VALUES (%s, %s, %s, %s)
            """,
            (session_id, patient_id, "IN_PROGRESS", json.dumps([])),
        )
    finally:
        cur.close()
    return get_session(session_id)


def get_session(session_id: str) -> Optional[dict]:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            f"SELECT {SESSION_COLUMNS} FROM {config.SESSIONS_TABLE} "
            "WHERE session_id = %s",
            (session_id,),
        )
        row = cur.fetchone()
    finally:
        cur.close()
    return _row_to_session(row) if row else None


def append_transcript_turn(session_id: str, role: str, text: str) -> dict:
    """Step 7/8/9: append one conversational turn (user or assistant)."""
    session = get_session(session_id)
    if session is None:
        raise ValueError(f"No session found with id {session_id}")

    transcript = session["transcript"]
    transcript.append(
        {
            "role": role,
            "text": text,
            "at": datetime.now(timezone.utc).isoformat(),
        }
    )

    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            f"UPDATE {config.SESSIONS_TABLE} SET transcript_json = %s, "
            "updated_at = CURRENT_TIMESTAMP() WHERE session_id = %s",
            (json.dumps(transcript), session_id),
        )
    finally:
        cur.close()
    return get_session(session_id)


def save_image_analysis(session_id: str, analysis: dict) -> dict:
    """Step 10: attach the Gemini vision result to a session."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            f"UPDATE {config.SESSIONS_TABLE} SET image_analysis_json = %s, "
            "updated_at = CURRENT_TIMESTAMP() WHERE session_id = %s",
            (json.dumps(analysis), session_id),
        )
    finally:
        cur.close()
    return get_session(session_id)


def complete_session(session_id: str, summary: str, triage_level: str) -> dict:
    """Step 11: close out a session with a combined final summary."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            f"""
            UPDATE {config.SESSIONS_TABLE}
            SET status = 'COMPLETE', summary = %s, triage_level = %s,
                updated_at = CURRENT_TIMESTAMP()
            WHERE session_id = %s
            """,
            (summary, triage_level, session_id),
        )
    finally:
        cur.close()
    return get_session(session_id)


def get_latest_session_for_patient(patient_id: str) -> Optional[dict]:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            f"SELECT {SESSION_COLUMNS} FROM {config.SESSIONS_TABLE} "
            "WHERE patient_id = %s ORDER BY created_at DESC LIMIT 1",
            (patient_id,),
        )
        row = cur.fetchone()
    finally:
        cur.close()
    return _row_to_session(row) if row else None
