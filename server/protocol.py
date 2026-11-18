import json
import re
from datetime import datetime


COMMANDS = {
    "RESTART",
    "GET_SYSTEM_INFO",
    "GET_CONFIG",
    "SET_HEARTBEAT_INTERVAL",
}
RESPONSE_STATUSES = {"RUNNING", "SUCCESS", "FAILED"}
AGENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _topic_agent(topic: str, suffix: str) -> str:
    parts = topic.split("/")
    if len(parts) != 3 or parts[0] != "agents" or parts[2] != suffix:
        raise ValueError(f"invalid {suffix} topic")
    if not AGENT_ID_PATTERN.fullmatch(parts[1]):
        raise ValueError("invalid agent_id")
    return parts[1]


def _timezone_datetime(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return value


def parse_heartbeat(topic: str, payload: bytes) -> tuple[str, dict]:
    agent_id = _topic_agent(topic, "heartbeat")
    heartbeat = json.loads(payload)
    if not isinstance(heartbeat, dict):
        raise ValueError("heartbeat must be a JSON object")
    if heartbeat.get("agent_id") != agent_id:
        raise ValueError("topic agent ID and payload agent ID do not match")

    _timezone_datetime(heartbeat.get("timestamp"), "timestamp")
    for field in ("cpu_usage", "memory_usage", "disk_usage"):
        value = heartbeat.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
            raise ValueError(f"{field} must be a number from 0 to 100")
    return agent_id, heartbeat


def parse_response(topic: str, payload: bytes) -> tuple[str, dict]:
    agent_id = _topic_agent(topic, "responses")
    response = json.loads(payload)
    if not isinstance(response, dict):
        raise ValueError("response must be a JSON object")
    if response.get("agent_id") != agent_id:
        raise ValueError("topic agent ID and payload agent ID do not match")
    if not isinstance(response.get("request_id"), str) or not response["request_id"].strip():
        raise ValueError("request_id must not be empty")
    if response.get("command") not in COMMANDS:
        raise ValueError("unsupported command")
    if response.get("status") not in RESPONSE_STATUSES:
        raise ValueError("invalid response status")
    _timezone_datetime(response.get("responded_at"), "responded_at")
    if response.get("result") is not None and not isinstance(response["result"], dict):
        raise ValueError("result must be a JSON object")
    if response.get("error") is not None and not isinstance(response["error"], str):
        raise ValueError("error must be a string")
    if response["status"] == "FAILED" and not response.get("error"):
        raise ValueError("FAILED response requires an error")
    return agent_id, response


def validate_command(command: str, payload: object) -> dict:
    if command not in COMMANDS:
        raise ValueError("unsupported command")
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    if command == "SET_HEARTBEAT_INTERVAL":
        interval = payload.get("interval")
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 1 <= interval <= 3600:
            raise ValueError("interval must be a number from 1 to 3600")
        return {"interval": float(interval)}
    if payload:
        raise ValueError(f"{command} does not accept payload fields")
    return {}
