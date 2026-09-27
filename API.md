# Triage Rover API Reference

For anyone integrating hardware (mic/speaker, camera, NFC reader) with the
Flask backend. This is the full HTTP contract - no backend code needed on
your side, just these requests.

**Base URL:** `http://<host-machine-lan-ip>:5001`
(NOT `127.0.0.1` - that only means "this machine" to whoever's calling it.
Ask whoever's running `python3 app.py` for their actual LAN IP.)

Confirm the server is up first:
```
GET /api/health          -> {"status": "ok"}
```

---

## The core flow (in order)

### 1. Start an encounter - ONE call
```
POST /api/triage/session
Content-Type: application/json

{"nfc_uid": "ABC123"}
```
or, if you already have a `patient_id` from a separate patient lookup:
```
{"patient_id": "PAT-XXXXXXXX"}
```

Using `nfc_uid` auto-creates the patient record if that tag has never been
seen before, so a brand-new encounter is one call instead of two. If you
don't have real NFC hardware yet, any placeholder string works for testing
(e.g. `"DEMO-TAG-1"`).

**Response (201):**
```json
{
  "session_id": "SES-XXXXXXXXXX",
  "patient_id": "PAT-XXXXXXXX",
  "status": "IN_PROGRESS",
  "transcript": [],
  "image_analysis": null,
  "triage_level": null,
  "summary": null,
  "triage_state": {
    "route": null, "confirm_attempts": 0, "internal_severity": null,
    "red_flags": [], "lit_drawers": [], "escalated": false,
    "escalate_reason": null, "vitals": null
  }
}
```

**Save `session_id`.** Reuse this exact value for every call below, for
the entire encounter. Only create a new session when a genuinely new
patient encounter starts.

### 2. Speak the fixed intro (optional, no AI call, instant)
```
POST /session/<session_id>/intro
```
Response: `{reply_text, reply_audio_url, transcript, session_id}`.
Play back `reply_audio_url` (see "Playing a reply" below).

### 3. Speak the fixed vitals prompt (optional)
```
POST /session/<session_id>/nfc-vitals
Body (optional): {"pulse_ox": 98, "heart_rate": 76}
```
Same response shape as intro, plus `vitals`.

### 4. The main voice loop - repeat for every exchange
```
POST /session/<session_id>/voice
multipart/form-data, field "audio" = recorded patient audio (wav/mp3/m4a/webm)
```
**Response (200):**
```json
{
  "session_id": "SES-...",
  "transcript_text": "what the patient said",
  "reply_text": "AMI's spoken reply",
  "reply_audio_url": "/api/triage/audio/reply-xxxx.wav",
  "transcript": [ ...full turn history... ],
  "route": "internal" | "external" | "both" | null,
  "action": "ask_secondary_triage" | "ask_severity_scale" | "request_camera"
           | "confirm_injury" | "reask_camera" | "escalate_emt"
           | "light_drawers" | "offer_stay_or_leave" | "log_stay_monitor"
           | "log_checkout" | "none",
  "suspected_injury": "..." ,
  "drawers": ["1-2", "2-3"],
  "escalate": true | false,
  "triage_state": { ...updated state... }
}
```
Play back `reply_audio_url`. Keep looping this step for as long as the
conversation continues, same `session_id` every time.

### 5. Camera photo (if your hardware has one)
Send this whenever `action` in a `/voice` response is `"request_camera"`:
```
POST /session/<session_id>/image
multipart/form-data, field "image" = a jpg/png photo
```
Response: `{"session_id": "...", "image_analysis": {"observations": [...], "possible_concern_level": "low|medium|high", "notes": "..."}}`

### 6. Close out the encounter
```
POST /session/<session_id>/complete
```
Response: full session with `summary` (2-3 sentence handoff) and
`triage_level` ("1"=critical .. "5"=minor).

---

## Playing a reply

Every step above returns a `reply_audio_url` like
`/api/triage/audio/reply-xxxx.wav`. To actually play it:
```
GET <base-url><reply_audio_url>
```
Returns raw audio bytes (`.wav` by default - a real WAV container, so any
standard audio library/player works). `Content-Type` is set from the real
file extension.

---

## Hardware-trigger actions (manual/direct)

These normally fire automatically from `/voice` based on `action`/
`escalate`, but are also exposed directly - e.g. for a nurse-call button,
manual override, or a cabinet controller that polls for changes.

```
POST /session/<session_id>/escalate-emt
Body (optional): {"reason": "..."}
```
Flags the session and bumps the patient's `current_status` to
`EMT_ESCALATION`. **Stub** - no real pager/hardware yet, just persists
state and logs to the server console.

```
POST /session/<session_id>/light-drawers
Body: {"drawers": ["1-2", "2-3"]}
```
Records which cabinet drawers should light up. **Stub** - no real cabinet
hardware wired in yet, just persists `triage_state.lit_drawers`.

---

## Patient record endpoints

```
POST /api/patients                        Body: {} or {"nfc_uid": "..."}
GET  /api/patients/<patient_id>
GET  /api/patients/by-nfc/<nfc_uid>       -> {"patient": {...}, "latest_session": {...}}
PATCH /api/patients/<patient_id>/nfc      Body: {"nfc_uid": "..."}
```
Usually not needed directly if you're using the collapsed `nfc_uid` flow
in step 1 above - these exist for looking up an existing patient without
starting a new session, or manually linking a tag after the fact.

---

## Basic connectivity check (not part of the triage flow)

```
POST /api/rover/ping
Body: any JSON, echoed back
```
Original sanity-check endpoint - "can my device reach Flask at all,"
nothing clinical.

---

## Wristband navigation - REMOVED for now

`POST /api/rover/wristband-detect` (local OpenCV color-blob tracking) has
been removed - `opencv-python` wasn't installed and importing it at
startup (via `services/vision_service.py`) crashed the whole Flask app.
Re-add the endpoint/service and the `opencv-python` requirement if this
navigation feature comes back.

---

## Quick reference: the minimum loop

```
1. POST /api/triage/session {"nfc_uid": "..."}     -> session_id
2. POST /session/<id>/intro                         -> play reply
3. POST /session/<id>/nfc-vitals                    -> play reply
4. loop: POST /session/<id>/voice (your mic audio)  -> play reply
5. POST /session/<id>/image  (if you have a camera, when asked)
6. POST /session/<id>/complete
```
