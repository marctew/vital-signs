"""Home Assistant over MQTT discovery.

The server (never the Pis) connects to the MQTT broker Home Assistant uses and
announces every Pi and display. They appear in Home Assistant as devices with
entities, with nothing to configure there. State is published when it changes,
and Home Assistant can switch screens, change playlists and press buttons.

If the server stops, the broker marks everything unavailable (its last will),
which is how Home Assistant tells "the server is down" from "a Pi is down".
"""
import json
import logging
import re
import threading
import time

from . import db as database
from . import services

log = logging.getLogger("server.ha_mqtt")

BASE = "vitalsigns"
STATUS_TOPIC = f"{BASE}/status"
SYNC_SECONDS = 3
SETTINGS = ("mqtt_enabled", "mqtt_host", "mqtt_port", "mqtt_username", "mqtt_password", "mqtt_discovery_prefix")
NO_PLAYLIST = "None"

# (key in the agent's health readings, name, extra discovery fields)
AGENT_SENSORS = (
    ("temperature_c", "Temperature", {"device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement"}),
    ("memory_used_percent", "Memory used", {"unit_of_measurement": "%", "state_class": "measurement", "icon": "mdi:memory"}),
    ("disk_used_percent", "Disk used", {"unit_of_measurement": "%", "state_class": "measurement", "icon": "mdi:harddisk"}),
    ("load", "Load", {"state_class": "measurement", "icon": "mdi:gauge"}),
    ("wifi_signal_dbm", "Wi-Fi signal", {"device_class": "signal_strength", "unit_of_measurement": "dBm", "state_class": "measurement"}),
)
DISPLAY_BUTTONS = (("identify", "Identify", "mdi:crosshairs-question"), ("reload", "Reload", "mdi:reload"),
                   ("next", "Next item", "mdi:skip-next"), ("previous", "Previous item", "mdi:skip-previous"))
AGENT_BUTTONS = (("restart_browser", "Restart browsers", "mdi:web-refresh"), ("reboot", "Reboot", "mdi:restart"))


def load_settings(db):
    rows = dict(db.execute("SELECT key, value FROM settings WHERE key LIKE 'mqtt_%'").fetchall())
    return {
        "enabled": rows.get("mqtt_enabled") == "1",
        "host": rows.get("mqtt_host", ""),
        "port": int(rows.get("mqtt_port") or 1883),
        "username": rows.get("mqtt_username", ""),
        "password": rows.get("mqtt_password", ""),
        "prefix": rows.get("mqtt_discovery_prefix") or "homeassistant",
    }


def save_settings(db, form):
    """Store the settings form. A blank password keeps the stored one."""
    host = (form.get("host") or "").strip()
    prefix = (form.get("prefix") or "homeassistant").strip().strip("/")
    try:
        port = int(form.get("port") or 1883)
    except ValueError:
        raise services.ServiceError("Port must be a number")
    if not 1 <= port <= 65535:
        raise services.ServiceError("Port must be between 1 and 65535")
    if form.get("enabled") and not host:
        raise services.ServiceError("Enter the broker's address")
    if not re.fullmatch(r"[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*", prefix):
        raise services.ServiceError("The discovery prefix uses letters, digits, - and _")
    values = {"mqtt_enabled": "1" if form.get("enabled") else "0", "mqtt_host": host, "mqtt_port": str(port),
              "mqtt_username": (form.get("username") or "").strip(), "mqtt_discovery_prefix": prefix}
    if form.get("password"):
        values["mqtt_password"] = form["password"]
    for key, value in values.items():
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?)"
                   " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    db.commit()


def save_general_settings(db, form):
    """Settings other features fall back to, such as the Frigate address."""
    frigate = (form.get("frigate_url") or "").strip().rstrip("/")
    if frigate and not frigate.startswith(("http://", "https://")):
        raise services.ServiceError("The Frigate address must start with http:// or https://")
    db.execute("INSERT INTO settings (key, value) VALUES ('frigate_url', ?)"
               " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (frigate,))
    db.commit()


def on_off(value):
    return "ON" if value else "OFF"


def build_messages(db, prefix):
    """Every retained message Home Assistant should have right now: {topic: payload string}.

    Discovery configs describe the entities; state topics hold their values.
    """
    messages = {}
    playlists = [p["name"] for p in services.list_playlists(db)]

    def entity(component, uid, config, device):
        config = {"unique_id": f"vitalsigns_{uid}", "availability_topic": STATUS_TOPIC, "device": device, **config}
        messages[f"{prefix}/{component}/vitalsigns_{uid}/config"] = json.dumps(config, sort_keys=True)

    for agent in services.list_agents(db):
        device = {"identifiers": [f"vitalsigns_agent_{agent['id']}"], "name": f"Vital Signs Pi {agent['hostname']}",
                  "manufacturer": "Vital Signs", "model": "Display agent", "sw_version": agent["git_sha"]}
        base = f"{BASE}/agent/{agent['id']}"
        uid = f"agent_{agent['id']}"
        health = agent["health"]
        entity("binary_sensor", f"{uid}_online", {
            "name": "Online", "device_class": "connectivity", "state_topic": f"{base}/state",
            "value_template": "{{ value_json.online }}"}, device)
        state = {"online": on_off(agent["online"])}
        for key, name, extra in AGENT_SENSORS:
            if key in health:
                entity("sensor", f"{uid}_{key}", {"name": name, "state_topic": f"{base}/state", "entity_category": "diagnostic",
                                                  "value_template": "{{ value_json.%s }}" % key, **extra}, device)
                state[key] = health[key]
        if "under_voltage" in health:
            entity("binary_sensor", f"{uid}_power", {
                "name": "Power problem", "device_class": "problem", "entity_category": "diagnostic",
                "state_topic": f"{base}/state", "value_template": "{{ value_json.power_problem }}"}, device)
            state["power_problem"] = on_off(health.get("under_voltage") or health.get("throttled"))
        for action, name, icon in AGENT_BUTTONS:
            entity("button", f"{uid}_{action}", {"name": name, "icon": icon, "entity_category": "config",
                                                 "command_topic": f"{base}/press/{action}"}, device)
        messages[f"{base}/state"] = json.dumps(state, sort_keys=True)

    for d in services.list_displays(db):
        device = {"identifiers": [f"vitalsigns_display_{d['id']}"], "name": f"Vital Signs {d['name']}",
                  "manufacturer": "Vital Signs", "model": f"Display {d['width']}x{d['height']}",
                  "via_device": f"vitalsigns_agent_{d['agent_id']}"}
        base = f"{BASE}/display/{d['id']}"
        uid = f"display_{d['id']}"
        entity("binary_sensor", f"{uid}_online", {
            "name": "Online", "device_class": "connectivity", "state_topic": f"{base}/state",
            "value_template": "{{ value_json.online }}"}, device)
        entity("switch", f"{uid}_screen", {
            "name": "Screen", "icon": "mdi:monitor", "state_topic": f"{base}/state",
            "value_template": "{{ value_json.screen }}", "command_topic": f"{base}/set/screen"}, device)
        entity("select", f"{uid}_playlist", {
            "name": "Playlist", "icon": "mdi:playlist-play", "options": [NO_PLAYLIST] + playlists,
            "state_topic": f"{base}/state", "value_template": "{{ value_json.playlist }}",
            "command_topic": f"{base}/set/playlist"}, device)
        entity("sensor", f"{uid}_showing", {
            "name": "Showing", "icon": "mdi:television-play", "state_topic": f"{base}/state",
            "value_template": "{{ value_json.showing }}"}, device)
        entity("binary_sensor", f"{uid}_problem", {
            "name": "Page problem", "device_class": "problem", "state_topic": f"{base}/state",
            "value_template": "{{ value_json.problem }}", "json_attributes_topic": f"{base}/state",
            "json_attributes_template": "{{ {'errors': value_json.errors} | tojson }}"}, device)
        for action, name, icon in DISPLAY_BUTTONS:
            entity("button", f"{uid}_{action}", {"name": name, "icon": icon, "command_topic": f"{base}/press/{action}"}, device)

        current = d["current"] or {}
        showing = ("override: " if current.get("override") else "") + (current.get("name") or current.get("url") or "")
        if d["browser"] == "down":
            showing = "browser restarting"
        errors = [f"{e.get('name') or e.get('url')}: {e.get('error')}" for e in d["errors"] or []]
        messages[f"{base}/state"] = json.dumps({
            "online": on_off(d["online"] and d["browser"] != "down"),
            "screen": on_off(d["online"] and d["screen_on"]),
            "playlist": d["playlist"]["name"] if d["playlist"] else NO_PLAYLIST,
            "showing": (showing or "nothing")[:250],
            "problem": on_off(errors or d["placement_ok"] is False),
            "errors": errors[:10],
        }, sort_keys=True)
    return messages


def handle_command(db, topic, payload):
    """Act on a message from Home Assistant. Returns a short description, or None if it was not for us."""
    match = re.fullmatch(rf"{BASE}/(display|agent)/(\d+)/(set|press)/(\w+)", topic)
    if not match:
        return None
    kind, ref, verb, what = match.groups()
    payload = payload.strip()
    try:
        if kind == "display":
            display = services.get_display(db, ref)
            if verb == "set" and what == "screen" and payload in ("ON", "OFF"):
                services.set_power_override(db, display["id"], payload == "ON")
                return f"{display['name']}: screen {payload.lower()}"
            if verb == "set" and what == "playlist":
                row = db.execute("SELECT id FROM playlists WHERE name = ?", (payload,)).fetchone()
                if row is None and payload != NO_PLAYLIST:
                    return None
                services.assign(db, display["id"], row["id"] if row else None)
                return f"{display['name']}: playlist {payload}"
            if verb == "press" and what in [b[0] for b in DISPLAY_BUTTONS]:
                services.queue_command(db, display, what)
                return f"{display['name']}: {what}"
        elif verb == "press" and what in [b[0] for b in AGENT_BUTTONS]:
            agent = services.get_agent(db, ref)
            services.queue_agent_command(db, agent["id"], what)
            return f"{agent['hostname']}: {what}"
    except services.ServiceError as e:
        log.warning("Ignored %s: %s", topic, e)
    return None


class Bridge(threading.Thread):
    """Keeps Home Assistant in step with the server over one MQTT connection."""

    def __init__(self, db_path):
        super().__init__(name="ha-mqtt", daemon=True)
        self.db_path = db_path
        self.client = None
        self.settings = None
        self.connected = False
        self.status = "Not set up"
        self.published = {}         # topic -> payload last sent on this connection
        self._changed = threading.Event()
        self._halt = threading.Event()

    def reload(self):
        """Settings were saved: reconnect with them on the next pass."""
        self._changed.set()

    def stop(self):
        self._halt.set()
        self._changed.set()

    # --- connection -----------------------------------------------------------

    def _disconnect(self):
        if self.client is not None:
            try:
                if self.connected:
                    self.client.publish(STATUS_TOPIC, "offline", qos=1, retain=True)
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:       # shutting a broken connection must never raise
                pass
        self.client, self.connected, self.published = None, False, {}

    def _connect(self, settings):
        import paho.mqtt.client as mqtt
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="vitalsigns-server")
        if settings["username"]:
            client.username_pw_set(settings["username"], settings["password"])
        client.will_set(STATUS_TOPIC, "offline", qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=2, max_delay=60)
        self.status = f"Connecting to {settings['host']}:{settings['port']}"
        client.connect_async(settings["host"], settings["port"], keepalive=30)
        client.loop_start()
        self.client = client

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            self.connected = False
            self.status = f"The broker refused the connection: {reason_code}"
            log.warning("MQTT: %s", self.status)
            return
        self.connected, self.published = True, {}       # send everything again on a fresh connection
        self.status = "Connected"
        log.info("MQTT: connected to %s", self.settings["host"])
        client.publish(STATUS_TOPIC, "online", qos=1, retain=True)
        client.subscribe([(f"{BASE}/+/+/set/#", 1), (f"{BASE}/+/+/press/#", 1)])

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        if self.connected:
            log.warning("MQTT: connection lost (%s); retrying", reason_code)
        self.connected = False
        if self.status == "Connected":
            self.status = f"Connection lost ({reason_code}); retrying"

    def _on_message(self, client, userdata, message):
        db = database.connect(self.db_path)
        try:
            done = handle_command(db, message.topic, message.payload.decode("utf-8", "replace"))
            if done:
                log.info("From Home Assistant: %s", done)
                self._changed.set()         # reflect the change back without waiting for the next pass
        finally:
            db.close()

    # --- keeping Home Assistant up to date ----------------------------------------

    def sync(self, db):
        """Publish whatever differs from what this connection last sent. Returns how many messages went out."""
        wanted = build_messages(db, self.settings["prefix"])
        sent = 0
        for topic, payload in wanted.items():
            if self.published.get(topic) != payload:
                self.client.publish(topic, payload, qos=1, retain=True)
                self.published[topic] = payload
                sent += 1
        # Entities for Pis and displays that no longer exist are removed by clearing their config.
        known = set(json.loads(dict(db.execute("SELECT key, value FROM settings WHERE key = 'mqtt_discovered'").fetchall())
                               .get("mqtt_discovered", "[]")))
        configs = {t for t in wanted if t.endswith("/config")}
        for topic in known - configs:
            self.client.publish(topic, "", qos=1, retain=True)
            self.published.pop(topic, None)
            sent += 1
        if known != configs:
            db.execute("INSERT INTO settings (key, value) VALUES ('mqtt_discovered', ?)"
                       " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(sorted(configs)),))
            db.commit()
        return sent

    def run(self):
        while not self._halt.is_set():
            db = database.connect(self.db_path)
            try:
                settings = load_settings(db)
                if settings != self.settings:
                    self._disconnect()
                    self.settings = settings
                    if settings["enabled"] and settings["host"]:
                        try:
                            self._connect(settings)
                        except Exception as e:      # paho missing, bad host name, and so on
                            self.status = f"Could not start: {e}"
                            log.warning("MQTT: %s", self.status)
                    else:
                        self.status = "Switched off" if settings["host"] else "Not set up"
                if self.client is not None and self.connected:
                    self.sync(db)
            except Exception:
                log.exception("MQTT: sync failed")
            finally:
                db.close()
            self._changed.wait(SYNC_SECONDS)
            self._changed.clear()
        self._disconnect()


bridge = None       # the running Bridge, when the server was started with python -m server


def start(app):
    global bridge
    bridge = Bridge(app.config["DB_PATH"])
    bridge.start()
    return bridge
