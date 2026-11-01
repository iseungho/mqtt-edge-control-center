import json
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


MQTT_HOST = os.getenv("MQTT_HOST", "broker")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
HEARTBEAT_TOPIC = "agents/+/heartbeat"
RESPONSE_TOPIC = "agents/+/responses"
OFFLINE_AFTER_SECONDS = 30
COMMAND_TIMEOUT_SECONDS = 10
AGENT_IDS = ("agent-a", "agent-b", "agent-c")
STATIC_DIR = Path(__file__).parent / "static"

agents_lock = threading.Lock()
# shortcut: state resets with the server, use persistent storage only if history is required.
agents = {}


def reset_agents():
    with agents_lock:
        agents.clear()
        agents.update(
            {
                agent_id: {
                    "agent_id": agent_id,
                    "last_heartbeat": None,
                    "cpu_usage": None,
                    "memory_usage": None,
                    "disk_usage": None,
                    "_last_seen": None,
                    "_command": None,
                }
                for agent_id in AGENT_IDS
            }
        )


def parse_heartbeat(topic: str, payload: bytes) -> tuple[str, dict]:
    topic_parts = topic.split("/")
    if len(topic_parts) != 3 or topic_parts[0] != "agents" or topic_parts[2] != "heartbeat":
        raise ValueError("invalid heartbeat topic")

    agent_id = topic_parts[1]
    heartbeat = json.loads(payload)
    if agent_id not in agents or not isinstance(heartbeat, dict):
        raise ValueError("unknown agent or invalid JSON object")
    if heartbeat.get("agent_id") != agent_id:
        raise ValueError("topic agent ID and payload agent ID do not match")

    reported_at = datetime.fromisoformat(heartbeat.get("timestamp", ""))
    if reported_at.tzinfo is None:
        raise ValueError("timestamp must include a timezone")

    for field in ("cpu_usage", "memory_usage", "disk_usage"):
        value = heartbeat.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
            raise ValueError(f"{field} must be a number from 0 to 100")

    return agent_id, heartbeat


def record_heartbeat(
    agent_id: str,
    heartbeat: dict,
    *,
    now: float | None = None,
    received_at: str | None = None,
):
    with agents_lock:
        agents[agent_id].update(
            {
                "last_heartbeat": received_at or datetime.now(timezone.utc).isoformat(),
                "cpu_usage": heartbeat["cpu_usage"],
                "memory_usage": heartbeat["memory_usage"],
                "disk_usage": heartbeat["disk_usage"],
                "_last_seen": time.monotonic() if now is None else now,
            }
        )


def command_view(command: dict | None) -> dict | None:
    if command is None:
        return None
    return {key: value for key, value in command.items() if not key.startswith("_")}


def begin_command(
    agent_id: str,
    request_id: str,
    *,
    now: float | None = None,
    requested_at: str | None = None,
) -> dict:
    current_time = time.monotonic() if now is None else now
    with agents_lock:
        current = agents[agent_id]["_command"]
        if current and current["status"] == "PENDING":
            if current_time < current["_deadline"]:
                raise ValueError("a command is already pending")
            current["status"] = "TIMEOUT"

        command = {
            "request_id": request_id,
            "command": "RESTART",
            "status": "PENDING",
            "requested_at": requested_at or datetime.now(timezone.utc).isoformat(),
            "responded_at": None,
            "_deadline": current_time + COMMAND_TIMEOUT_SECONDS,
        }
        agents[agent_id]["_command"] = command
        return command_view(command)


def parse_response(topic: str, payload: bytes) -> tuple[str, dict]:
    topic_parts = topic.split("/")
    if len(topic_parts) != 3 or topic_parts[0] != "agents" or topic_parts[2] != "responses":
        raise ValueError("invalid response topic")

    agent_id = topic_parts[1]
    response = json.loads(payload)
    if agent_id not in agents or not isinstance(response, dict):
        raise ValueError("unknown agent or invalid JSON object")
    if response.get("agent_id") != agent_id:
        raise ValueError("topic agent ID and payload agent ID do not match")
    if not isinstance(response.get("request_id"), str) or not response["request_id"].strip():
        raise ValueError("request_id must not be empty")
    if response.get("command") != "RESTART" or response.get("status") != "SUCCESS":
        raise ValueError("invalid command response")
    responded_at = datetime.fromisoformat(response.get("responded_at", ""))
    if responded_at.tzinfo is None:
        raise ValueError("responded_at must include a timezone")
    return agent_id, response


def complete_command(agent_id: str, response: dict, *, now: float | None = None) -> bool:
    current_time = time.monotonic() if now is None else now
    with agents_lock:
        command = agents[agent_id]["_command"]
        if (
            command is None
            or command["request_id"] != response["request_id"]
            or command["status"] != "PENDING"
        ):
            return False
        if current_time >= command["_deadline"]:
            command["status"] = "TIMEOUT"
            return False
        command["status"] = "SUCCESS"
        command["responded_at"] = response["responded_at"]
        return True


def agent_snapshot(now: float | None = None) -> list[dict]:
    current_time = time.monotonic() if now is None else now
    with agents_lock:
        snapshots = []
        for agent in agents.values():
            command = agent["_command"]
            if command and command["status"] == "PENDING" and current_time >= command["_deadline"]:
                command["status"] = "TIMEOUT"

            snapshots.append(
                {
                    key: value
                    for key, value in agent.items()
                    if not key.startswith("_")
                }
                | {
                    "status": "ONLINE"
                    if agent["_last_seen"] is not None
                    and current_time - agent["_last_seen"] < OFFLINE_AFTER_SECONDS
                    else "OFFLINE",
                    "alert_status": "WARNING"
                    if agent["disk_usage"] is not None and agent["disk_usage"] > 90
                    else "NORMAL",
                    "command": command_view(command),
                }
            )
        return snapshots


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        client.subscribe(HEARTBEAT_TOPIC, qos=1)
        client.subscribe(RESPONSE_TOPIC, qos=1)
        print(f"[server] subscribed to {HEARTBEAT_TOPIC} and {RESPONSE_TOPIC}")
    else:
        print(f"[server] MQTT connection failed: {reason_code}")


def on_message(client, userdata, message):
    try:
        if message.topic.endswith("/heartbeat"):
            agent_id, heartbeat = parse_heartbeat(message.topic, message.payload)
            record_heartbeat(agent_id, heartbeat)
            print(f"[server] heartbeat received from {agent_id}")
        elif message.topic.endswith("/responses"):
            agent_id, response = parse_response(message.topic, message.payload)
            if complete_command(agent_id, response):
                print(f"[server] command completed by {agent_id}: {response['request_id']}")
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        print(f"[server] ignored invalid MQTT message: {error}")


mqtt_client = mqtt.Client(
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    client_id="control-server",
)
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.reconnect_delay_set(min_delay=1, max_delay=10)


@asynccontextmanager
async def lifespan(app: FastAPI):
    mqtt_client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    mqtt_client.loop_start()
    try:
        yield
    finally:
        mqtt_client.disconnect()
        mqtt_client.loop_stop()


reset_agents()
app = FastAPI(title="MQTT Edge Control Center", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/agents")
def get_agents():
    return agent_snapshot()


@app.post("/api/agents/{agent_id}/restart", status_code=202)
def restart_agent(agent_id: str):
    if agent_id not in agents:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not mqtt_client.is_connected():
        raise HTTPException(status_code=503, detail="MQTT broker is not connected")

    request_id = str(uuid.uuid4())
    try:
        command = begin_command(agent_id, request_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    result = mqtt_client.publish(
        f"agents/{agent_id}/commands",
        json.dumps(command),
        qos=1,
    )
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        with agents_lock:
            agents[agent_id]["_command"]["status"] = "ERROR"
        raise HTTPException(status_code=503, detail="Failed to publish MQTT command")
    return command
