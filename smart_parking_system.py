import os
import json
import time
import logging
import threading
import datetime
import requests
import RPi.GPIO as GPIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8940717795:AAEtIcBNEMK_WX79U2QvROguCUvPCf6QgNY")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "991431522")
THINGSPEAK_KEY = os.getenv("THINGSPEAK_API_KEY", "YSUQZJ46BYUHZN0W")  # unique per spot / channel

PIN_TRIGGER = 7
PIN_ECHO = 11

REQUEST_TIMEOUT = 10          # seconds, for Telegram/ThingSpeak HTTP calls
RELAY_TIMEOUT = 5             # seconds, for LAN calls to sibling Pis (should be fast)
ECHO_TIMEOUT = 0.05           # seconds, max time to wait for sensor echo
THINGSPEAK_INTERVAL = 15      # per-channel ThingSpeak minimum update interval
MAIN_LOOP_DELAY = 1           # seconds between loop iterations
CONSECUTIVE_READINGS_REQUIRED = 2  # readings needed to confirm a state change (debounce)

DEVICE_ID = os.getenv("DEVICE_ID", "leanjieberrypi")
IS_HUB = os.getenv("IS_HUB", "false").lower() == "true"
RELAY_PORT = int(os.getenv("RELAY_PORT", "8765"))
RELAY_SECRET = os.getenv("RELAY_SECRET", "change-me-shared-secret")


def parse_peers(raw):
    peers = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        device_id, _, address = entry.partition("=")
        if device_id and address:
            peers[device_id] = address
    return peers


PEERS = parse_peers(os.getenv("PEERS", ""))
KNOWN_DEVICE_IDS = sorted(set(PEERS.keys()) | {DEVICE_ID})
TARGET_USAGE = f"<target> = {'|'.join(KNOWN_DEVICE_IDS)}|all"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [%(levelname)s] [{DEVICE_ID}] %(message)s",
    handlers=[
        logging.FileHandler("parking_node.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)
logger.info(
    f"Config: DEVICE_ID={DEVICE_ID} IS_HUB={IS_HUB} "
    f"PEERS={PEERS} KNOWN_DEVICE_IDS={KNOWN_DEVICE_IDS}"
)

# ---------------------------------------------------------------------------
# System state
# ---------------------------------------------------------------------------
monitoring_enabled = True     # can be paused per spot (reserved/maintenance)
proximity_threshold = 10.0    # cm; below this = a car is present
telegram_offset = None
state_lock = threading.Lock()  # guards monitoring_enabled / proximity_threshold

# Occupancy tracking (main-loop thread only)
occupied_state = None   # confirmed state: None (unknown, pre-boot-reading) / True / False
candidate_state = None  # state currently being confirmed via debounce
candidate_count = 0


def setup_gpio():
    """Initializes GPIO pins for the ultrasonic sensor."""
    GPIO.setmode(GPIO.BOARD)
    GPIO.setwarnings(False)
    GPIO.setup(PIN_TRIGGER, GPIO.OUT)
    GPIO.setup(PIN_ECHO, GPIO.IN)
    GPIO.output(PIN_TRIGGER, GPIO.LOW)


def measure_distance():
    """Triggers the ultrasonic sensor and returns distance in cm.

    Returns None if the sensor doesn't respond within ECHO_TIMEOUT
    (disconnected/faulty sensor) instead of hanging forever.
    """
    GPIO.output(PIN_TRIGGER, GPIO.HIGH)
    time.sleep(0.00001)
    GPIO.output(PIN_TRIGGER, GPIO.LOW)

    wait_start = time.time()
    pulse_start = wait_start
    while GPIO.input(PIN_ECHO) == 0:
        pulse_start = time.time()
        if pulse_start - wait_start > ECHO_TIMEOUT:
            logger.warning("Sensor timeout waiting for echo start.")
            return None

    pulse_end = pulse_start
    while GPIO.input(PIN_ECHO) == 1:
        pulse_end = time.time()
        if pulse_end - pulse_start > ECHO_TIMEOUT:
            logger.warning("Sensor timeout waiting for echo end.")
            return None

    duration = pulse_end - pulse_start
    return round(duration * 17150, 2)


def send_telegram_msg(msg):
    """Sends an outbound message via the Telegram Bot API. Any device may
    call this directly -- sendMessage doesn't use the offset/polling
    mechanism, so there's no conflict between spots sharing one bot."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg}
    try:
        requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException as e:
        logger.error(f"Telegram Send Error: {e}")


def update_thingspeak(distance, occupied):
    """Pushes distance + occupancy data to this spot's own ThingSpeak
    channel. field1 = distance (cm), field2 = occupied (1/0). Logging this
    continuously (even while monitoring is disabled) is what lets you graph
    occupancy trends later -- turnover rate, peak hours, etc."""
    url = "https://api.thingspeak.com/update"
    params = {"api_key": THINGSPEAK_KEY, "field1": distance}
    if occupied is not None:
        params["field2"] = int(occupied)
    try:
        requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException as e:
        logger.error(f"ThingSpeak Update Error: {e}")


# ---------------------------------------------------------------------------
# Core actions -- shared by both the local Telegram handler (hub) and the
# LAN relay handler (satellite receiving a forwarded command), so the logic
# only lives in one place. Each returns a dict so status responses can carry
# a structured "occupied" flag alongside the human-readable message.
# ---------------------------------------------------------------------------
def do_enable():
    global monitoring_enabled
    with state_lock:
        monitoring_enabled = True
    return {"message": f"{DEVICE_ID}: monitoring ENABLED"}


def do_disable():
    global monitoring_enabled
    with state_lock:
        monitoring_enabled = False
    return {"message": f"{DEVICE_ID}: monitoring DISABLED"}


def do_set_threshold(value):
    global proximity_threshold
    if value <= 0:
        return {"message": f"{DEVICE_ID}: ERROR threshold must be positive"}
    with state_lock:
        proximity_threshold = value
    return {"message": f"{DEVICE_ID}: detection threshold set to {value} cm"}


def do_status():
    with state_lock:
        enabled, threshold = monitoring_enabled, proximity_threshold
    dist = measure_distance()
    if dist is None:
        return {
            "message": f"{DEVICE_ID}: sensor error | monitoring {'ON' if enabled else 'OFF'}",
            "occupied": None,
        }
    occupied = dist < threshold
    state_str = "OCCUPIED" if occupied else "VACANT"
    return {
        "message": f"{DEVICE_ID}: {state_str} | distance {dist} cm | monitoring {'ON' if enabled else 'OFF'}",
        "occupied": occupied,
    }


def relay_to_peer(device_id, action, value=None):
    """Hub-side: forwards a command to another Pi's local relay server."""
    address = PEERS.get(device_id)
    if not address:
        return {"message": f"{device_id}: ERROR unknown device", "occupied": None}
    url = f"http://{address}/relay"
    payload = {"secret": RELAY_SECRET, "action": action}
    if value is not None:
        payload["value"] = value
    try:
        resp = requests.post(url, json=payload, timeout=RELAY_TIMEOUT)
        data = resp.json()
        data.setdefault("occupied", None)
        return data
    except requests.exceptions.RequestException as e:
        logger.warning(f"Relay to {device_id} failed: {e}")
        return {"message": f"{device_id}: UNREACHABLE", "occupied": None}


def dispatch_action(target, action, value=None):
    """Applies action locally if this device is targeted, relays to peers
    otherwise, and fans out across everyone for 'all'. Returns a list of
    result dicts (one per targeted device)."""
    targets = KNOWN_DEVICE_IDS if target == "all" else [target]
    results = []
    for t in targets:
        if t == DEVICE_ID:
            if action == "enable":
                results.append(do_enable())
            elif action == "disable":
                results.append(do_disable())
            elif action == "status":
                results.append(do_status())
            elif action == "threshold":
                results.append(do_set_threshold(value))
        else:
            results.append(relay_to_peer(t, action, value))
    return results


def format_dispatch_reply(results, target, action):
    """Joins per-device messages into one Telegram reply, and for
    '/status all' appends a free-spot count computed from the occupied
    flags returned by each device."""
    text = "\n".join(r.get("message", "") for r in results)
    if action == "status" and target == "all":
        counted = [r for r in results if r.get("occupied") is not None]
        if counted:
            free = sum(1 for r in counted if not r["occupied"])
            text += f"\n\n{free} of {len(counted)} spot(s) free"
    return text


# ---------------------------------------------------------------------------
# LAN relay server, runs on every Pi so the hub can reach it
# ---------------------------------------------------------------------------
class RelayHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/relay":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            if body.get("secret") != RELAY_SECRET:
                self.send_response(403)
                self.end_headers()
                return

            action = body.get("action")
            if action == "enable":
                result = do_enable()
            elif action == "disable":
                result = do_disable()
            elif action == "status":
                result = do_status()
            elif action == "threshold":
                result = do_set_threshold(float(body.get("value")))
            else:
                result = {"message": f"{DEVICE_ID}: ERROR unknown action"}

            payload = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as e:
            logger.error(f"Relay handler error: {e}")
            self.send_response(500)
            self.end_headers()

    def log_message(self, format, *args):
        logger.info("Relay: " + (format % args))


def start_relay_server():
    server = ThreadingHTTPServer(("0.0.0.0", RELAY_PORT), RelayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Relay server listening on port {RELAY_PORT}")


# ---------------------------------------------------------------------------
# Telegram command polling - hub only
# ---------------------------------------------------------------------------
def initialize_telegram_offset():
    """Fast-forwards past any commands that arrived while the script was
    offline, so a restart doesn't replay (and execute) stale commands."""
    global telegram_offset
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT).json()
        if response.get("ok") and response.get("result"):
            telegram_offset = response["result"][-1]["update_id"] + 1
            logger.info(f"Skipped {len(response['result'])} stale update(s) on startup.")
    except requests.exceptions.RequestException as e:
        logger.warning(f"Could not initialize Telegram offset: {e}")


def _parse_target(parts, index):
    if len(parts) > index and (parts[index] in KNOWN_DEVICE_IDS or parts[index] == "all"):
        return parts[index]
    return None


def process_telegram_commands():
    """Polls getUpdates endpoint and executes incoming commands. Only the
    hub Pi should call this -- see IS_HUB."""
    global telegram_offset

    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    params = {"timeout": 1}
    if telegram_offset is not None:
        params["offset"] = telegram_offset

    try:
        response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT).json()
        if response.get("ok") and response.get("result"):
            for update in response["result"]:
                telegram_offset = update["update_id"] + 1  # Acknowledge update

                message = update.get("message", {})
                text = message.get("text", "").strip()
                sender_id = str(message.get("chat", {}).get("id"))

                if sender_id != CHAT_ID:
                    logger.warning(f"Ignored command from unauthorized chat_id={sender_id}")
                    continue

                logger.info(f"Received Command: '{text}'")
                parts = text.split()

                if text.startswith("/status"):
                    target = _parse_target(parts, 1)
                    if target:
                        results = dispatch_action(target, "status")
                        send_telegram_msg(format_dispatch_reply(results, target, "status"))
                    else:
                        send_telegram_msg(f"Usage: /status {TARGET_USAGE}")

                elif text.startswith("/enable"):
                    target = _parse_target(parts, 1)
                    if target:
                        results = dispatch_action(target, "enable")
                        send_telegram_msg(format_dispatch_reply(results, target, "enable"))
                    else:
                        send_telegram_msg(f"Usage: /enable {TARGET_USAGE}")

                elif text.startswith("/disable"):
                    target = _parse_target(parts, 1)
                    if target:
                        results = dispatch_action(target, "disable")
                        send_telegram_msg(format_dispatch_reply(results, target, "disable"))
                    else:
                        send_telegram_msg(f"Usage: /disable {TARGET_USAGE}")

                elif text.startswith("/threshold"):
                    target = _parse_target(parts, 1)
                    if target and len(parts) == 3:
                        try:
                            value = float(parts[2])
                            results = dispatch_action(target, "threshold", value)
                            send_telegram_msg(format_dispatch_reply(results, target, "threshold"))
                        except ValueError:
                            send_telegram_msg("Format error. Use: /threshold <target> <value_in_cm>")
                    else:
                        send_telegram_msg(f"Usage: /threshold {TARGET_USAGE} <value_in_cm>")

                else:
                    send_telegram_msg(
                        "Available commands:\n"
                        f"/status {TARGET_USAGE}\n"
                        f"/enable {TARGET_USAGE}\n"
                        f"/disable {TARGET_USAGE}\n"
                        f"/threshold {TARGET_USAGE} <value_cm>"
                    )

    except requests.exceptions.RequestException as e:
        logger.error(f"Telegram Polling Error: {e}")


if __name__ == "__main__":
    setup_gpio()
    start_relay_server()  # every Pi listens, so it can be a relay target
    last_thingspeak_time = 0.0

    try:
        if IS_HUB:
            initialize_telegram_offset()
        send_telegram_msg(f"{DEVICE_ID} online and monitoring." + (" (hub)" if IS_HUB else ""))

        while True:
            if IS_HUB:
                process_telegram_commands()

            dist = measure_distance()
            occ_now = None

            if dist is not None:
                occ_now = dist < proximity_threshold

                if monitoring_enabled:
                    if occ_now == candidate_state:
                        candidate_count += 1
                    else:
                        candidate_state = occ_now
                        candidate_count = 1

                    if (
                        candidate_count >= CONSECUTIVE_READINGS_REQUIRED
                        and candidate_state != occupied_state
                    ):
                        was_initial_reading = occupied_state is None
                        occupied_state = candidate_state
                        state_str = "OCCUPIED" if occupied_state else "VACANT"

                        if was_initial_reading:
                            # Don't ping Telegram just because the Pi rebooted only notify on genuine changes after the baseline is established.
                            logger.info(f"Initial state: {state_str}")
                        else:
                            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            if occupied_state:
                                event_text = f"[{DEVICE_ID}] Car parked. ({timestamp})"
                            else:
                                event_text = f"[{DEVICE_ID}] Spot now VACANT. ({timestamp})"
                            send_telegram_msg(event_text)
                            logger.info(event_text)

            # ThingSpeak logging continues even while monitoring is disabled
            if dist is not None and time.time() - last_thingspeak_time >= THINGSPEAK_INTERVAL:
                update_thingspeak(dist, occ_now)
                last_thingspeak_time = time.time()

            time.sleep(MAIN_LOOP_DELAY)

    except KeyboardInterrupt:
        logger.info("Shutting down (keyboard interrupt).")
    finally:
        GPIO.cleanup()