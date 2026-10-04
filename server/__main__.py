"""Run the server: python -m server (from the repo root).

    python -m server                  run the server
    python -m server set-password     set the admin UI password (turns the login on)
    python -m server clear-password   remove it (turns the login off)
"""
import argparse
import getpass
import logging
import sys

from waitress import serve

from . import auth, config, create_app


def set_password(cfg):
    password = getpass.getpass("New admin password: ")
    if len(password) < 8:
        sys.exit("Use at least 8 characters.")
    if getpass.getpass("Repeat it: ") != password:
        sys.exit("The passwords do not match.")
    cfg["data_dir"].mkdir(parents=True, exist_ok=True)
    auth.set_password(cfg["data_dir"], password)
    print("Password set. The admin UI now asks for it; no restart needed.")
    if not cfg["control_token"]:
        print("The control API (/api/v1) now needs a login too. For Home Assistant or n8n, "
              "set control_token in the server config and restart the server.")


def main():
    parser = argparse.ArgumentParser(prog="python -m server", description="Vital Signs server")
    parser.add_argument("command", nargs="?", default="run", choices=("run", "set-password", "clear-password"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load()
    if args.command == "set-password":
        return set_password(cfg)
    if args.command == "clear-password":
        auth.clear_password(cfg["data_dir"])
        return print("Password removed. The admin UI is open again.")
    if not cfg["agent_token"] or cfg["agent_token"] == "CHANGE_ME":
        logging.warning("agent_token is not set: agents will be rejected until it is configured")
    app = create_app(cfg)
    logging.info("Vital Signs server on http://%s:%s (data in %s)", cfg["host"], cfg["port"], cfg["data_dir"])
    serve(app, host=cfg["host"], port=cfg["port"], threads=8)


if __name__ == "__main__":
    main()
