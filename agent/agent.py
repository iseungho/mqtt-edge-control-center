import json
import os
import random
import signal
import threading
from datetime import datetime, timezone

import paho.mqtt.client as mqtt


AGENT_ID = os.getenv("AGENT_ID", "agent-unknown")
MQTT_HOST = os.getenv("MQTT_HOST", "broker")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
HEARTBEAT_INTERVAL = float(os.getenv("HEARTBEAT_INTERVAL", "10"))
HEARTBEAT_TOPIC = f"agents/{AGENT_ID}/heartbeat"

stop_event = threading.Event()


def build_heartbeat() -> dict[str, str | float]:
    heartbeat = {
        "agent_id": AGENT_ID,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cpu_usage": round(random.uniform(0, 100), 1),
        "memory_usage": round(random.uniform(0, 100), 1),
        "disk_usage": round(random.uniform(0, 100), 1),
    }
    assert all(
        0 <= heartbeat[field] <= 100
        for field in ("cpu_usage", "memory_usage", "disk_usage")
    )
    return heartbeat


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print(f"[{AGENT_ID}] connected to MQTT broker at {MQTT_HOST}:{MQTT_PORT}")
    else:
        print(f"[{AGENT_ID}] MQTT connection failed: {reason_code}")


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    if reason_code != 0 and not stop_event.is_set():
        print(f"[{AGENT_ID}] disconnected unexpectedly; reconnecting: {reason_code}")


def stop_agent(signum, frame):
    stop_event.set()


def main():
    if not AGENT_ID.strip():
        raise ValueError("AGENT_ID must not be empty")
    if HEARTBEAT_INTERVAL <= 0:
        raise ValueError("HEARTBEAT_INTERVAL must be greater than zero")

    signal.signal(signal.SIGTERM, stop_agent)
    signal.signal(signal.SIGINT, stop_agent)

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=AGENT_ID,
    )
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=10)
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()

    try:
        while not stop_event.is_set():
            if not client.is_connected():
                stop_event.wait(1)
                continue

            heartbeat = build_heartbeat()
            result = client.publish(
                HEARTBEAT_TOPIC,
                json.dumps(heartbeat),
                qos=1,
            )
            try:
                result.wait_for_publish(timeout=5)
                print(f"[{AGENT_ID}] heartbeat published: {heartbeat}")
            except RuntimeError as error:
                print(f"[{AGENT_ID}] heartbeat publish failed: {error}")

            stop_event.wait(HEARTBEAT_INTERVAL)
    finally:
        client.disconnect()
        client.loop_stop()
        print(f"[{AGENT_ID}] stopped")


if __name__ == "__main__":
    main()

