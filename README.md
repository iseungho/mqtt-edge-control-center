# MQTT Edge Control Center

Docker-based MQTT edge agent monitoring and remote control system using FastAPI and Eclipse Mosquitto.

## Current implementation

- Eclipse Mosquitto MQTT broker
- Three independent Docker agents (`agent-a`, `agent-b`, `agent-c`)
- JSON heartbeat published every 10 seconds
- Virtual CPU, memory, and disk usage values from 0 to 100

## Run the MQTT foundation

```powershell
docker compose up --build
```

Heartbeat topic:

```text
agents/{agent_id}/heartbeat
```

Heartbeat message:

```json
{
  "agent_id": "agent-a",
  "timestamp": "2026-11-01T00:00:00+00:00",
  "cpu_usage": 35.2,
  "memory_usage": 61.7,
  "disk_usage": 92.4
}
```

Subscribe from the broker container:

```powershell
docker compose exec broker mosquitto_sub -t "agents/+/heartbeat" -v
```

> The broker allows anonymous connections for local development only.
