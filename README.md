# AMY — Autonomous Medical-triage Rover

AMI is an unmanned first-aid rover that performs an initial medical triage
encounter without a human staffing the station. A patient taps an NFC
wristband against the rover, talks to it, and optionally shows it an
injury; the rover decides — in real time — whether to dispense first-aid
supplies from its own onboard drawers, flag a human nurse/EMT, or clear the
patient to go, and produces a structured handoff summary for whoever picks
up the case afterward.

The system is split into two halves that communicate over plain HTTP on the
local network:

- **`backend/`** — a Flask API that owns all the "thinking": speech
  transcription, the triage conversation itself, vision analysis of an
  injury photo, decision logic, and persistence.
- **`backend/arduino/TriageCodeAMI.ino`** — the ESP32 firmware running on
  the physical rover: RFID wristband reader, I2S microphone/speaker,
  onboard camera, and the drawer LEDs. It captures raw input, ships it to
  the backend, and acts on whatever the backend decides.

Nothing about the "clinical" decision-making lives on the microcontroller —
the ESP32's job is strictly I/O. Every judgment call is made server-side by
Gemini, using rules defined entirely in `backend/config.py`.

---

1. **Wristband tap.** The RFID reader on the ESP32 reads an NFC UID and
   `POST`s it to `/api/triage/session`. The backend looks up (or silently
   creates) a patient record for that tag and opens a brand-new triage
   session tied to it.
2. **Fixed intro + vitals prompt.** Two scripted lines are spoken back
   immediately (`/session/<id>/intro`, `/session/<id>/nfc-vitals`) — no AI
   call, no latency, just ElevenLabs TTS reading a config string. If the
   rover has a pulse-oximeter/heart-rate reading at this point, it's
   attached to the session here.
3. **Voice turns.** Every time the patient speaks, the ESP32 records ~5
   seconds of I2S audio and posts it to `/session/<id>/voice`. The backend
   runs it through ElevenLabs speech-to-text, feeds the transcript plus
   full conversation history plus the session's current triage state into
   Gemini, and gets back one structured decision — never free-form prose —
   describing exactly what to say and do next.
4. **Camera turn (as needed).** If Gemini's decision says it needs to see
   the injury (`action: "request_camera"`), the ESP32's onboard camera
   captures a frame and posts it to `/session/<id>/image`. That triggers a
   vision pass (what's visibly observable — no diagnosis) followed by a
   second, narrower Gemini call that decides nurse vs. onboard supplies vs.
   cleared-to-go for that specific photo.
5. **Auto-bridged effects.** Neither Gemini call is trusted to silently
   "do" anything — the Flask route layer is the single place that turns a
   decision into a real backend effect: escalating to a human
   (`escalate_emt`) or recording which drawers to light
   (`light_drawers`). The API response includes a `leds` field so the
   ESP32 never has to re-derive physical LED numbers from drawer ids
   itself.
6. **Close-out.** `/session/<id>/complete` combines the full transcript and
   any image analysis into one 2–3 sentence handoff summary plus a 1–5
   triage level, persists it, and updates the patient's record.

Every one of those steps is a plain HTTP call — see [`API.md`](API.md) for
the exact request/response shapes if you're integrating hardware or a
dashboard against this backend.

---

## The decision engine

The actual "intelligence" is a single, tightly-specified JSON contract
between the backend and Gemini, defined in
[`config.GEMINI_AMI_SYSTEM_PROMPT`](backend/config.py). Every turn, Gemini
must respond with exactly:

```json
{
  "spoken_reply": "1-3 short spoken sentences, no labels",
  "route": "internal" | "external" | "both" | null,
  "action": "ask_secondary_triage" | "ask_severity_scale" | "request_camera"
           | "confirm_injury" | "reask_camera" | "escalate_emt"
           | "light_drawers" | "offer_stay_or_leave" | "log_stay_monitor"
           | "log_checkout" | "none",
  "suspected_injury": "short label or null",
  "confirm_attempts": 0,
  "internal_severity": 1,
  "red_flags": ["dizziness", "trouble breathing", "chest pressure", "nausea", "confusion"],
  "drawers": ["1-1", "3-2"],
  "escalate": true
}
```

The rules that constrain how Gemini fills that shape are hard safety
priorities, not suggestions:

- **Red flags or internal severity 7–10 always win.** Regardless of what
  else is in progress (mid photo-confirmation, mid stay-or-leave offer),
  that immediately overrides everything into `escalate_emt`.
- **External wounds require confirmation before dispensing.** A photo plus
  the patient's own description of what happened has to be confirmed by
  the patient before drawers are chosen; three failed confirmation
  attempts auto-escalates to a human rather than looping forever.
- **Mild, no-red-flag cases get a choice**, not an autopilot decision — the
  patient is offered to stay and be monitored or check out, and that
  choice is logged either way.
- Every drawer id Gemini can return is drawn from one fixed cabinet
  directory (`config.CABINET_DIRECTORY`) — a hallucinated id is dropped
  rather than acted on (`config.drawers_to_leds()`).

A second, deliberately smaller prompt
(`config.GEMINI_IMAGE_TRIAGE_PROMPT`) handles the narrow "what do I do
about this specific photo" decision on its own — nurse / onboard supplies /
cleared — so a single image doesn't require re-running the entire
multi-turn conversational rulebook just to react to it.

---

## Architecture

```
backend/
  app.py                     Flask app factory + entrypoint (python3 app.py)
  config.py                  Every tunable value (env-var backed): models,
                              prompts, table names, LED wiring, voice ids
  demo_run.py                Scripted end-to-end run with zero hardware -
                              synthesizes fake "patient" audio via
                              ElevenLabs TTS and drives the real API
  test_snowflake.py          Phase 1 smoke test: DB connectivity + CRUD
  test_elevenlabs.py         STT/TTS round-trip smoke test
  gemini.py (repo root)      Standalone Gemini API key/SDK smoke test

  db/
    schema.sql               Reference SQL (documentation - app self-
                              provisions the same tables at boot)
    snowflake_client.py       All Snowflake reads/writes. Two tables:
                              PATIENTS, TRIAGE_SESSIONS.

  services/
    gemini_service.py         triage_reply() / analyze_injury_image() /
                              image_care_decision() / summarize_session() -
                              every Gemini call, with retry-with-backoff on
                              transient 500/503/504 (not 429 - see below)
    elevenlabs_service.py     speech_to_text() / text_to_speech()

  routes/
    patients.py               Create/fetch/link a patient record
    triage.py                 The whole session lifecycle: intro, vitals,
                              voice, image, escalate, light-drawers,
                              complete
    rover.py                  Connectivity ping + drawer/LED lookup for
                              the cabinet controller

  arduino/
    TriageCodeAMI.ino         ESP32 firmware: RFID, I2S mic/speaker,
                              onboard camera, drawer LEDs, WiFi

API.md                        Full HTTP contract for hardware/dashboard integrators
SETUP.md                      Phase-by-phase build/bring-up guide
```

### Tech stack

| Layer               | Choice                                  |
|---------------------|------------------------------------------|
| Web server           | Flask (dev server; not production-hardened) |
| Database             | Snowflake (patient + session records, JSON-blob conversation state) |
| Conversational AI    | Google Gemini (`google-genai` SDK) — text triage turns + vision |
| Speech               | ElevenLabs (speech-to-text + text-to-speech) |
| Microcontroller      | ESP32 (WiFi, camera, I2S audio, MFRC522 RFID reader) |

### Data model

Two Snowflake tables, deliberately simple:

- **`PATIENTS`** — one row per person: `patient_id`, `nfc_uid`,
  `current_status`, latest `triage_level`/`triage_summary`.
- **`TRIAGE_SESSIONS`** — one row per encounter, linked to a patient by
  `patient_id`. The conversation transcript, image analysis, and the full
  AMI decision state (`route`, `confirm_attempts`, `internal_severity`,
  `red_flags`, `lit_drawers`, `escalated`, `vitals`, ...) are each stored as
  a single JSON-encoded `VARCHAR` column rather than normalized rows or
  Snowflake `VARIANT` — a deliberate simplicity trade-off (see the header
  comment in `db/snowflake_client.py`).

Both tables self-provision on every boot via `CREATE TABLE IF NOT EXISTS` /
`ADD COLUMN IF NOT EXISTS` — there's no separate migration step, and
restarting the backend never touches existing data, since none of the
conversation state lives in the Python process itself.

---

## Running it

```bash
cd triaging
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
```

Create `backend/.env` with:

```bash
# Flask
FLASK_HOST=0.0.0.0
FLASK_PORT=5001
FLASK_DEBUG=true

# Snowflake
SNOWFLAKE_USER=...
SNOWFLAKE_PASSWORD=...
SNOWFLAKE_ACCOUNT=...          # <org>-<account> identifier, from Snowflake's UI
SNOWFLAKE_WAREHOUSE=...
SNOWFLAKE_ROLE=...             # optional

# Gemini - https://aistudio.google.com/apikey
GEMINI_API_KEY=...

# ElevenLabs - https://elevenlabs.io/app/settings/api-keys
ELEVENLABS_API_KEY=...
```

Every other value (model names, prompts, table names, drawer/LED wiring,
voice id) has a sane default in `config.py` and only needs overriding if
you're actually changing behavior.

Start the server:

```bash
cd backend
python3 app.py
```

Snowflake schema provisioning happens in a background thread so a slow or
misconfigured Snowflake connection never blocks the HTTP port from opening
— the rover can always reach `/api/health` even if the database is down.

### Testing without any hardware

`demo_run.py` drives the entire pipeline end-to-end using synthetic
"patient" audio (ElevenLabs TTS standing in for a microphone), so the full
Snowflake + Gemini + ElevenLabs stack can be proven out before any ESP32 is
involved:

```bash
python3 demo_run.py --scenario external --image-path some_photo.jpg
python3 demo_run.py --scenario internal_severe
python3 demo_run.py --line "My arm really hurts" --line "It's a 4 out of 10"
```

Individual pieces can also be smoke-tested in isolation:

```bash
python3 test_snowflake.py       # DB connectivity + full CRUD cycle
python3 test_elevenlabs.py      # TTS -> STT round trip
python3 ../gemini.py            # bare Gemini API key/SDK sanity check
```

See [`SETUP.md`](SETUP.md) for the original incremental, phase-by-phase
build order this project was developed in.

---

## Known limitations

- **`escalate_emt()` and `light_drawers()` are stubs.** Both persist state
  and print to the server console; there's no real pager/nurse
  notification or cabinet-hardware integration wired in yet — a future
  hardware layer is expected to poll `triage_state.lit_drawers` /
  `escalated`, or the existing `/light-drawers` and `/escalate-emt`
  endpoints can be called directly (e.g. from a nurse-call button).
- **Gemini's free tier is easy to exhaust during development** — the
  `generativelanguage.googleapis.com` free tier caps at a small number of
  requests/day per model, and every voice turn, image turn (two Gemini
  calls each), and session completion draws from that same quota. A 429
  from Gemini is treated as non-retryable on purpose (retrying a
  quota-exceeded call just burns more quota) and falls back to a graceful
  "please repeat that" response instead of a hard failure.
- **Wristband navigation via OpenCV color-blob tracking was removed** —
  `opencv-python` wasn't installed and importing it at startup crashed the
  whole app. The old `/api/rover/wristband-detect` endpoint and
  `services/vision_service.py` would need to come back together if that
  feature returns.
- **Single cached Snowflake connection, no connection pool** — fine for a
  single-rover deployment; would need pooling for concurrent multi-rover
  load.

---

## License

MIT — see [`LICENSE`](LICENSE).
