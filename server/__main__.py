"""Run the server: python -m server (from the repo root)."""
import logging

from waitress import serve

from . import config, create_app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load()
    if not cfg["agent_token"] or cfg["agent_token"] == "CHANGE_ME":
        logging.warning("agent_token is not set: agents will be rejected until it is configured")
    app = create_app(cfg)
    logging.info("Vital Signs server on http://%s:%s (data in %s)", cfg["host"], cfg["port"], cfg["data_dir"])
    serve(app, host=cfg["host"], port=cfg["port"], threads=8)


if __name__ == "__main__":
    main()
