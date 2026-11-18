import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from mqtt_service import MqttService
from protocol import validate_command
from storage import StateStore


MQTT_HOST = os.getenv("MQTT_HOST", "broker")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "control-server")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
STATE_DB_PATH = os.getenv("STATE_DB_PATH", ":memory:")
OFFLINE_AFTER_SECONDS = 30
COMMAND_TIMEOUT_SECONDS = 10
STATIC_DIR = Path(__file__).parent / "static"

store = StateStore(STATE_DB_PATH)
mqtt_service = MqttService(
    MQTT_HOST, MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD, store
)


class CommandRequest(BaseModel):
    command: str
    payload: dict = Field(default_factory=dict)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not MQTT_PASSWORD:
        raise RuntimeError("MQTT_PASSWORD must not be empty")
    mqtt_service.start()
    try:
        yield
    finally:
        mqtt_service.stop()


app = FastAPI(title="MQTT Edge Control Center", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/agents")
def get_agents():
    return store.agent_snapshot(offline_after=OFFLINE_AFTER_SECONDS)


@app.get("/api/commands")
def get_commands(
    agent_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    return store.list_commands(agent_id=agent_id, limit=limit)


@app.post("/api/agents/{agent_id}/commands", status_code=202)
def issue_command(agent_id: str, request: CommandRequest):
    if not store.agent_exists(agent_id):
        raise HTTPException(status_code=404, detail="Agent not found")
    if not mqtt_service.is_connected():
        raise HTTPException(status_code=503, detail="MQTT broker is not connected")
    try:
        payload = validate_command(request.command, request.payload)
        command = store.begin_command(
            agent_id,
            str(uuid.uuid4()),
            request.command,
            payload,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    if not mqtt_service.publish_command(agent_id, command):
        store.fail_command(command["request_id"], "failed to publish MQTT command")
        raise HTTPException(status_code=503, detail="Failed to publish MQTT command")
    return command


@app.post("/api/agents/{agent_id}/restart", status_code=202)
def restart_agent(agent_id: str):
    return issue_command(agent_id, CommandRequest(command="RESTART"))
