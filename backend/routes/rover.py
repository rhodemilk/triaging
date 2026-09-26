"""
Basic Arduino/NodeMCU <-> Flask connectivity (Milestone 1).

Intentionally trivial - this is just "can the rover talk to Flask", nothing
clinical. Everything the rover might send is treated as opaque JSON and
echoed back so you can verify the link before wiring in real sensors.

An optional shared-secret header (ROVER_API_KEY) lets you gate this without
building real auth - fine for a hackathon LAN demo, not for production.
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
