# Triage Rover — Incremental Build Guide

This maps directly onto the pipeline steps you listed. Do these **in order**
— each phase is independently testable before you move to the next one.
Motors/sensors/hardware polish is deliberately last.

```
backend/
  config.py              <- every env var, one place
  .env.example           <- copy to .env, fill in real values
  app.py                 <- Flask app factory + startup
  db/
    schema.sql           <- reference SQL (app also self-provisions this)
    snowflake_client.py   <- all Snowflake reads/writes
  services/
    gemini_service.py     <- Gemini triage conversation + image analysis
    elevenlabs_service.py <- ElevenLabs speech-to-text + text-to-speech
  routes/
    patients.py           <- create/check/lookup patient (steps 5, 6, 12)
    triage.py              <- voice/image/complete session (steps 7-11)
    rover.py               <- basic NodeMCU<->Flask ping (Milestone 1)
  clients/
    mic_client.py          <- test the voice pipeline w/ your laptop mic
    camera_client.py       <- test the image pipeline w/ your laptop webcam
  test_snowflake.py         <- Phase 1 smoke test
gemini.py                   <- Phase 2 smoke test (repo root, matches old file)
```

## Phase 0 — Install

```bash
cd triaging
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
cp backend/.env.example backend/.env
```

Now open `backend/.env` and fill in:
- `SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD`, `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_WAREHOUSE`
- `GEMINI_API_KEY` (from https://aistudio.google.com/apikey)
- `ELEVENLABS_API_KEY` (from https://elevenlabs.io/app/settings/api-keys)

> ⚠️ The old root `gemini.py` had a real API key hardcoded in it. Treat that
> key as burned — regenerate a new one in AI Studio and only ever put it in
> `backend/.env`, which is gitignored.

## Phase 1 — Snowflake only

```bash
cd backend
python3 test_snowflake.py
```

This creates the database/schema/tables (if missing) and does a full
create → read → update → complete cycle on fake data. Fix Snowflake config
here before moving on — everything downstream depends on it.

## Phase 2 — Gemini only (no Flask, no Snowflake)

```bash
cd ..            # repo root
python3 gemini.py
```

Confirms your `GEMINI_API_KEY` and the SDK call shape work in isolation.

## Phase 3 — Flask + Snowflake (patient records)

```bash
cd backend
python3 app.py
```

In another terminal:

```bash
curl -s http://localhost:5001/api/health
curl -s -X POST http://localhost:5001/api/patients -H "Content-Type: application/json" -d '{}'
# -> {"patient_id": "PAT-XXXXXXXX", "current_status": "CHECKED_IN", ...}

curl -s http://localhost:5001/api/patients/PAT-XXXXXXXX
```

This is pipeline steps 5 + 6.

## Phase 4 — Voice loop (ElevenLabs STT → Gemini → ElevenLabs TTS)

With `app.py` still running:

```bash
# start a session for the patient you created above
curl -s -X POST http://localhost:5001/api/triage/session \
  -H "Content-Type: application/json" -d '{"patient_id": "PAT-XXXXXXXX"}'
# -> {"session_id": "SES-XXXXXXXXXX", ...}

# talk to the rover with your laptop mic
python3 clients/mic_client.py --patient-id PAT-XXXXXXXX --session-id SES-XXXXXXXXXX
```

Each run of `mic_client.py` records a few seconds of audio, sends it to
`/api/triage/session/<id>/voice`, and plays back Gemini's spoken reply.
Run it multiple times in a row (same `--session-id`) to have a real
back-and-forth conversation — this is pipeline steps 8 + 7 + 9.

## Phase 5 — Camera → Gemini vision

```bash
python3 clients/camera_client.py --session-id SES-XXXXXXXXXX
```

Captures one webcam frame, sends it to `/api/triage/session/<id>/image`,
and prints Gemini's non-diagnostic visible-injury observations. This is
pipeline step 10. On the real rover, swap `cv2.VideoCapture` for whatever
camera module you're using — the Flask side doesn't change.

## Phase 6 — Combine into one final triage summary

```bash
curl -s -X POST http://localhost:5001/api/triage/session/SES-XXXXXXXXXX/complete
```

Pulls the full voice transcript + image analysis for that session, asks
Gemini to synthesize one handoff summary + triage level, saves it, and
updates the patient's row. This is pipeline step 11.

## Phase 7 — NFC lookup (link a tag, then retrieve later)

```bash
curl -s -X PATCH http://localhost:5001/api/patients/PAT-XXXXXXXX/nfc \
  -H "Content-Type: application/json" -d '{"nfc_uid": "ABC123"}'

# later, from any device that scanned the tag:
curl -s http://localhost:5001/api/patients/by-nfc/ABC123
# -> {"patient": {...}, "latest_session": {...}}
```

You can also create a patient directly from a first-time NFC scan:

```bash
curl -s -X POST http://localhost:5001/api/patients \
  -H "Content-Type: application/json" -d '{"nfc_uid": "ABC123"}'
```

This is pipeline step 12.

## Phase 8 — Arduino/NodeMCU connectivity (Milestone 1, unchanged)

See `README_Milestone1.md`. `/api/rover/ping` is a generic echo endpoint the
NodeMCU sketch can hit to confirm it can reach Flask over Wi-Fi. Nothing
clinical happens here on purpose.

## Phase 9 — Motors/sensors/polish

Only after Phases 1–8 all work end-to-end. Not implemented yet, intentionally.

---

### If you want to hand these phases to an AI coding agent one at a time
instead of all at once, each phase above is already sized to be one prompt,
e.g.:

> "Implement Phase 1 only: a Snowflake client with `create_patient`,
> `get_patient`, and a smoke test script. Don't touch Gemini/ElevenLabs yet."

> "Now Phase 3: Flask routes to create/fetch a patient using the Phase 1
> Snowflake client."

> "Now Phase 4: a `/voice` endpoint that does ElevenLabs STT → Gemini reply →
> ElevenLabs TTS, plus a local mic test client."

...and so on through Phase 7, each depending only on the previous ones.
