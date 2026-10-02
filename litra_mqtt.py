"""Expose a USB-connected Logitech Litra Glow/Beam to Home Assistant via MQTT.

The bridge talks to the light over USB HID (protocol from timrogers/litra-rs) and
announces it through MQTT discovery as a Home Assistant light entity.
Settings live in config.json next to this script; the log goes next to the config.
"""

import argparse
import json
import logging
import logging.handlers
import os
import queue
import socket
import sys
import tempfile
import time
from pathlib import Path

import hid
import paho.mqtt.client as mqtt

log = logging.getLogger("litra-mqtt")

VENDOR_ID = 0x046D
USAGE_PAGE = 0xFF43  # the vendor-defined HID collection that accepts commands
# Product ID -> (model, min lumen, max lumen). The Beam LX uses a different protocol.
MODELS = {0xC900: ("Litra Glow", 20, 250), 0xC901: ("Litra Beam", 30, 400)}
MIN_KELVIN, MAX_KELVIN = 2700, 6500

# Reports look like 11 FF 04 <function> <value hi> <value lo>, zero-padded to 20 bytes.
GET_POWER, GET_BRIGHTNESS, GET_TEMP = 0x01, 0x31, 0x81
SET_POWER, SET_BRIGHTNESS, SET_TEMP = 0x1C, 0x4C, 0x9C
# Unsolicited change reports (as decoded by reznikov/litra2mqtt). Not every firmware sends
# them, so the bridge also polls.
EVT_POWER, EVT_BRIGHTNESS, EVT_TEMP = 0x00, 0x10, 0x20
QUERIES = (GET_POWER, GET_BRIGHTNESS, GET_TEMP)

DEFAULTS = {"port": 1883, "username": None, "password": None,
            "poll_seconds": 30, "discovery_prefix": "homeassistant"}


def single_instance(name):
    """Exit if another bridge runs on this machine; both would fight over the same light."""
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateMutexW(None, False, f"Local\\{name}")
        already_running = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    else:
        import fcntl

        handle = open(Path(tempfile.gettempdir()) / f"{name}.lock", "w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            already_running = False
        except OSError:
            already_running = True
    if already_running:
        log.info("Another bridge is already running, exiting")
        sys.exit(0)
    return handle


def setup_logging(log_file):
    handlers = [logging.handlers.RotatingFileHandler(
        log_file, maxBytes=512_000, backupCount=2, encoding="utf-8")]
    if sys.stderr is not None:  # pythonw.exe has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers
    )


def report(function, value=0):
    return [0x11, 0xFF, 0x04, function, value >> 8, value & 0xFF] + [0] * 14


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cmds = queue.Queue()
        self.light = None  # serial, model, lo, hi, base of the light that is currently open
        self.state = {}
        self.last_state = None

        hostname = socket.gethostname().lower()
        # Per host, so bridges on several PCs neither kick each other off the broker
        # nor overwrite each other's status.
        self.bridge_topic = f"litra/bridge/{hostname}"
        self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"litra-mqtt-{hostname}")
        if cfg["username"]:
            self.mqtt.username_pw_set(cfg["username"], cfg["password"])
        self.mqtt.will_set(self.bridge_topic, "offline", retain=True)
        self.mqtt.reconnect_delay_set(1, 60)
        self.mqtt.on_connect = self.on_connect
        self.mqtt.on_disconnect = self.on_disconnect
        self.mqtt.on_message = self.on_message

    # --- MQTT ---------------------------------------------------------------

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT connection refused: %s", reason_code)
            return
        log.info("MQTT connected")
        client.publish(self.bridge_topic, "online", retain=True)
        client.subscribe([("litra/+/set", 0), (f"{self.cfg['discovery_prefix']}/status", 0)])
        self.announce()

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        log.warning("MQTT disconnected: %s", reason_code)

    def on_message(self, client, userdata, msg):
        if msg.topic == f"{self.cfg['discovery_prefix']}/status":
            if msg.payload == b"online":  # Home Assistant restarted
                self.announce()
            return
        light = self.light
        if light is None or msg.topic != f"{light['base']}/set":
            return
        try:
            command = json.loads(msg.payload)
        except ValueError:
            log.warning("Unreadable command: %r", msg.payload)
            return
        log.info("Command: %s", command)
        self.queue_command(command, light)

    def queue_command(self, command, light):
        state = command.get("state")
        if command.get("brightness") == 0:
            state = "OFF"
        if state != "OFF":
            # Brightness and colour first, then power on, so the old values never flash up.
            if "brightness" in command:
                b = min(max(int(command["brightness"]), 1), 255)
                lm = round(light["lo"] + (b - 1) * (light["hi"] - light["lo"]) / 254)
                self.cmds.put((SET_BRIGHTNESS, lm))
            if "color_temp" in command:
                k = round(int(command["color_temp"]) / 100) * 100
                self.cmds.put((SET_TEMP, min(max(k, MIN_KELVIN), MAX_KELVIN)))
        if state in ("ON", "OFF"):
            # Power goes in the first value byte, not as a 16-bit number like lumen and kelvin.
            self.cmds.put((SET_POWER, 0x0100 if state == "ON" else 0))
        # The light acknowledges without echoing the value, so read the state back.
        for q in QUERIES:
            self.cmds.put((q, 0))

    def announce(self):
        """Publish discovery and the current state (on connect and after an HA restart)."""
        light = self.light
        if light is None or not self.mqtt.is_connected():
            return
        model, serial, base = light["model"], light["serial"], light["base"]
        config = {
            "name": None,  # the entity takes the device name
            "unique_id": f"litra_{serial}",
            "default_entity_id": "light." + model.lower().replace(" ", "_"),
            "schema": "json",
            "command_topic": f"{base}/set",
            "state_topic": f"{base}/state",
            "availability": [{"topic": self.bridge_topic}, {"topic": f"{base}/availability"}],
            "availability_mode": "all",
            "supported_color_modes": ["color_temp"],
            "color_temp_kelvin": True,
            "min_kelvin": MIN_KELVIN,
            "max_kelvin": MAX_KELVIN,
            "device": {
                "identifiers": [f"litra_{serial}"],
                "name": model,
                "manufacturer": "Logitech",
                "model": model,
                "serial_number": serial,
            },
            "origin": {"name": "litra-mqtt", "support_url": "https://github.com/axsb/litra-mqtt"},
        }
        topic = f"{self.cfg['discovery_prefix']}/light/litra_{serial}/config"
        self.mqtt.publish(topic, json.dumps(config), retain=True)
        self.mqtt.publish(f"{base}/availability", "online", retain=True)
        self.publish_state(force=True)

    def publish_state(self, force=False):
        light, s = self.light, self.state
        if light is None or len(s) < 3:
            return
        payload = {
            "state": "ON" if s["on"] else "OFF",
            "brightness": max(1, round(1 + (s["lm"] - light["lo"]) * 254 / (light["hi"] - light["lo"]))),
            "color_mode": "color_temp",
            "color_temp": s["k"],
        }
        if payload == self.last_state and not force:
            return
        self.last_state = payload
        log.info("State: %s, %d lm, %d K", payload["state"], s["lm"], s["k"])
        self.mqtt.publish(f"{light['base']}/state", json.dumps(payload), retain=True)

    # --- USB ----------------------------------------------------------------

    def handle_report(self, data):
        if len(data) < 6 or data[:3] != [0x11, 0xFF, 0x04]:
            return
        function, value = data[3], data[4] << 8 | data[5]
        if function in (GET_POWER, EVT_POWER):
            self.state["on"] = data[4] == 1
        elif function in (GET_BRIGHTNESS, EVT_BRIGHTNESS):
            self.state["lm"] = value
        elif function in (GET_TEMP, EVT_TEMP):
            self.state["k"] = value
        else:
            return  # acknowledgements of set commands
        self.publish_state()

    def find(self):
        for info in hid.enumerate(VENDOR_ID, 0):
            if info["usage_page"] == USAGE_PAGE and info["product_id"] in MODELS:
                return info
        return None

    def device_loop(self):
        while True:
            info = self.find()
            if info is None:
                time.sleep(5)
                continue
            dev = hid.device()
            try:
                dev.open_path(info["path"])
            except OSError as e:
                log.warning("Cannot open the light: %s", e)
                time.sleep(5)
                continue

            model, lo, hi = MODELS[info["product_id"]]
            serial = info["serial_number"] or "unknown"
            log.info("Found %s (serial %s)", model, serial)
            while not self.cmds.empty():  # drop commands queued while no light was present
                self.cmds.get_nowait()
            self.state, self.last_state = {}, None
            self.light = {"model": model, "serial": serial, "lo": lo, "hi": hi, "base": f"litra/{serial}"}
            self.announce()
            for q in QUERIES:
                self.cmds.put((q, 0))
            next_poll = time.monotonic() + self.cfg["poll_seconds"]
            try:
                while True:
                    # At most one command per round; waiting for the reply paces fast sequences.
                    try:
                        function, value = self.cmds.get_nowait()
                        if dev.write(report(function, value)) < 0:
                            raise OSError("write failed")
                    except queue.Empty:
                        pass
                    data = dev.read(20, 200)
                    if data:
                        self.handle_report(data)
                    if time.monotonic() >= next_poll:
                        next_poll = time.monotonic() + self.cfg["poll_seconds"]
                        for q in QUERIES:
                            self.cmds.put((q, 0))
            except (OSError, ValueError) as e:
                log.warning("Lost the light: %s", e)
            finally:
                dev.close()
                if self.mqtt.is_connected():
                    self.mqtt.publish(f"{self.light['base']}/availability", "offline", retain=True)
                self.light = None
                time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent / "config.json",
                        help="path to config.json (default: next to this script)")
    args = parser.parse_args()

    setup_logging(args.config.parent / "litra-mqtt.log")
    lock = single_instance("litra-mqtt")  # noqa: F841 - must stay alive until exit
    cfg = DEFAULTS | json.loads(args.config.read_text(encoding="utf-8"))
    bridge = Bridge(cfg)
    log.info("Starting, broker %s:%s", cfg["host"], cfg["port"])
    bridge.mqtt.connect_async(cfg["host"], cfg["port"], keepalive=30)
    bridge.mqtt.loop_start()
    try:
        bridge.device_loop()
    except KeyboardInterrupt:
        pass
    finally:
        if bridge.mqtt.is_connected():
            bridge.mqtt.publish(bridge.bridge_topic, "offline", retain=True).wait_for_publish(2)
        bridge.mqtt.disconnect()
        bridge.mqtt.loop_stop()


if __name__ == "__main__":
    main()
