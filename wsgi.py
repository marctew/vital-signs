"""Gunicorn entry point for the server: gunicorn wsgi:app, with one worker (deploy/vitalsigns-server.service).

One worker, because the Home Assistant bridge and the module data caches live in memory.
`python -m server` still runs the server on its own (waitress), for development and Windows.
"""
import logging

from server import config, create_app, ha_mqtt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
cfg = config.load()
if not cfg["agent_token"] or cfg["agent_token"] == "CHANGE_ME":
    logging.warning("agent_token is not set: agents will be rejected until it is configured")
app = create_app(cfg)
ha_mqtt.start(app)      # does nothing until Home Assistant is set up on the Settings page
logging.info("Vital Signs server (data in %s)", cfg["data_dir"])
