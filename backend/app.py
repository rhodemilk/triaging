"""
Main Flask entrypoint. Run with:

    python3 backend/app.py

Everything configurable (host/port, media paths, table names, model names,
voice ids, API keys) lives in config.py / backend/.env - nothing here is
hardcoded.
"""

import os
import threading

from flask import Flask, jsonify

import config
from db import snowflake_client as db
from routes.patients import patients_bp
from routes.rover import rover_bp
from routes.triage import triage_bp


def create_app() -> Flask:
    app = Flask(__name__)

    # Resolve + create the folders used for uploaded/generated media once,
    # at startup, so routes don't need to think about it per-request.
    media_root = config.MEDIA_ROOT
    audio_dir = os.path.join(media_root, config.AUDIO_SUBDIR)
    image_dir = os.path.join(media_root, config.IMAGE_SUBDIR)
    app.config["AUDIO_UPLOAD_DIR"] = os.path.join(audio_dir, "uploads")
    app.config["AUDIO_REPLY_DIR"] = os.path.join(audio_dir, "replies")
    app.config["IMAGE_UPLOAD_DIR"] = image_dir
    for path in (
        app.config["AUDIO_UPLOAD_DIR"],
        app.config["AUDIO_REPLY_DIR"],
        app.config["IMAGE_UPLOAD_DIR"],
    ):
        os.makedirs(path, exist_ok=True)

    app.register_blueprint(patients_bp)
    app.register_blueprint(triage_bp)
    app.register_blueprint(rover_bp)

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"}), 200

    return app


app = create_app()


def _init_snowflake_in_background() -> None:
    """Provision the Snowflake schema without blocking the HTTP port from
    opening. Slow/failing Snowflake auth (bad account id, network hiccups,
    retries) must NEVER delay `app.run()` - a rover/Arduino making its one
    boot-time request can't wait around for that.
    """
    missing_sf = config.require(
        "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_WAREHOUSE"
    )
    if missing_sf:
        print(
            f"[warn] Snowflake config incomplete ({', '.join(missing_sf)}); "
            "skipping schema init. Patient/triage DB routes will fail until "
            "backend/.env is filled in."
        )
        return

    print("Initializing Snowflake schema (create-if-not-exists)...")
    try:
        db.init_schema()
        print("Snowflake ready.")
    except Exception as exc:  # noqa: BLE001 - background thread must never crash the app
        print(
            f"[warn] Snowflake connection failed ({exc}). "
            "Patient/triage DB routes will fail until backend/.env has "
            "valid Snowflake credentials, but the HTTP server is unaffected."
        )


if __name__ == "__main__":
    threading.Thread(target=_init_snowflake_in_background, daemon=True).start()

    print(f"Starting Flask on {config.FLASK_HOST}:{config.FLASK_PORT} "
          f"(debug={config.FLASK_DEBUG})")
    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT, debug=config.FLASK_DEBUG)
