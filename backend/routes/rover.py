"""
Arduino/ESP32 <-> Flask connectivity: connectivity check + navigation.

Routes here:
  - POST /api/rover/ping               - trivial connectivity sanity check
  - GET  /api/rover/drawer-leds        - full drawer-id -> supply name/LED
                                          map, for the cabinet controller to
                                          fetch once at boot

Everything the rover might send to /ping is treated as opaque JSON and
echoed back so you can verify the link before wiring in real sensors.

An optional shared-secret header (ROVER_API_KEY) lets you gate this without
building real auth - fine for a hackathon LAN demo, not for production.

NOTE: the old POST /api/rover/wristband-detect endpoint (local OpenCV
color-blob tracking, see former services/vision_service.py) has been
removed for now since opencv-python isn't installed and was blocking the
whole app from booting. Re-add it (and the opencv-python requirement) if
that navigation feature comes back.
"""

from flask import Blueprint, jsonify, request

import config

rover_bp = Blueprint("rover", __name__, url_prefix="/api/rover")


def _authorized(req) -> bool:
    if not config.ROVER_API_KEY:
        return True  # no key configured -> auth disabled
    return req.headers.get("X-Rover-Key") == config.ROVER_API_KEY


@rover_bp.post("/ping")
def ping():
    """Sanity-check endpoint for the NodeMCU sketch's first HTTP POST."""
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    print("Received from rover:", data)
    return jsonify({"status": "success", "received": data}), 200


@rover_bp.get("/drawer-leds")
def drawer_leds():
    """Full drawer-id -> {name, led} map, straight from config.py.

    The cabinet controller (whatever's actually driving the LEDs) can call
    this ONCE at boot and cache it locally - after that it only needs the
    "drawers" (or "leds", already resolved) list that comes back from
    /voice or /light-drawers each turn. Re-fetch this if DRAWER_LED_MAP is
    ever changed/reloaded without restarting the controller.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401

    merged = {
        drawer_id: {
            "name": name,
            "led": config.DRAWER_LED_MAP.get(drawer_id),
        }
        for drawer_id, name in config.CABINET_DIRECTORY.items()
    }
    return jsonify(merged), 200
