import json
import sqlite3
import threading
import time
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, path: str):
        self._lock = threading.RLock()
        self._last_seen_monotonic = {}
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    last_heartbeat TEXT,
                    cpu_usage REAL,
                    memory_usage REAL,
                    disk_usage REAL,
                    last_seen REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS commands (
                    request_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    command TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    error TEXT,
                    requested_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deadline REAL NOT NULL,
                    FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
                );
                CREATE INDEX IF NOT EXISTS commands_agent_requested
                ON commands(agent_id, requested_at DESC);
                """
            )

    def reset(self):
        with self._lock, self._db:
            self._last_seen_monotonic.clear()
            self._db.execute("DELETE FROM commands")
            self._db.execute("DELETE FROM agents")

    def record_heartbeat(
        self,
        agent_id: str,
        heartbeat: dict,
        *,
        now: float | None = None,
        received_at: str | None = None,
    ):
        received_monotonic = time.monotonic() if now is None else now
        received_epoch = time.time() if now is None else now
        with self._lock, self._db:
            self._last_seen_monotonic[agent_id] = received_monotonic
            self._db.execute(
                """
                INSERT INTO agents (
                    agent_id, last_heartbeat, cpu_usage, memory_usage, disk_usage, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    last_heartbeat=excluded.last_heartbeat,
                    cpu_usage=excluded.cpu_usage,
                    memory_usage=excluded.memory_usage,
                    disk_usage=excluded.disk_usage,
                    last_seen=excluded.last_seen
                """,
                (
                    agent_id,
                    received_at or utc_now(),
                    heartbeat["cpu_usage"],
                    heartbeat["memory_usage"],
                    heartbeat["disk_usage"],
                    received_epoch,
                ),
            )

    def agent_exists(self, agent_id: str) -> bool:
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM agents WHERE agent_id=?", (agent_id,)
            ).fetchone() is not None

    def _timeout_expired(self, now: float):
        self._db.execute(
            """
            UPDATE commands
            SET status='TIMEOUT', updated_at=?
            WHERE status IN ('PENDING', 'RUNNING') AND deadline<=?
            """,
            (utc_now(), now),
        )

    def begin_command(
        self,
        agent_id: str,
        request_id: str,
        command: str,
        payload: dict,
        *,
        timeout: float,
        now: float | None = None,
        requested_at: str | None = None,
    ) -> dict:
        current_time = time.time() if now is None else now
        requested = requested_at or utc_now()
        with self._lock, self._db:
            self._timeout_expired(current_time)
            active = self._db.execute(
                """
                SELECT 1 FROM commands
                WHERE agent_id=? AND status IN ('PENDING', 'RUNNING')
                """,
                (agent_id,),
            ).fetchone()
            if active:
                raise ValueError("a command is already running")
            self._db.execute(
                """
                INSERT INTO commands (
                    request_id, agent_id, command, payload, status, result, error,
                    requested_at, updated_at, deadline
                ) VALUES (?, ?, ?, ?, 'PENDING', NULL, NULL, ?, ?, ?)
                """,
                (
                    request_id,
                    agent_id,
                    command,
                    json.dumps(payload),
                    requested,
                    requested,
                    current_time + timeout,
                ),
            )
        return self.get_command(request_id)

    def apply_response(self, agent_id: str, response: dict, *, now: float | None = None) -> bool:
        current_time = time.time() if now is None else now
        with self._lock, self._db:
            self._timeout_expired(current_time)
            current = self._db.execute(
                "SELECT * FROM commands WHERE request_id=? AND agent_id=?",
                (response["request_id"], agent_id),
            ).fetchone()
            if current is None or current["command"] != response["command"]:
                return False
            if current["status"] not in {"PENDING", "RUNNING"}:
                return False
            if response["status"] == "RUNNING" and current["status"] == "RUNNING":
                return True
            self._db.execute(
                """
                UPDATE commands
                SET status=?, result=?, error=?, updated_at=?
                WHERE request_id=?
                """,
                (
                    response["status"],
                    json.dumps(response.get("result")) if response.get("result") is not None else None,
                    response.get("error"),
                    response["responded_at"],
                    response["request_id"],
                ),
            )
            return True

    def fail_command(self, request_id: str, error: str):
        with self._lock, self._db:
            self._db.execute(
                """
                UPDATE commands SET status='FAILED', error=?, updated_at=?
                WHERE request_id=? AND status='PENDING'
                """,
                (error, utc_now(), request_id),
            )

    @staticmethod
    def _command_view(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {
            "request_id": row["request_id"],
            "agent_id": row["agent_id"],
            "command": row["command"],
            "payload": json.loads(row["payload"]),
            "status": row["status"],
            "result": json.loads(row["result"]) if row["result"] else None,
            "error": row["error"],
            "requested_at": row["requested_at"],
            "updated_at": row["updated_at"],
        }

    def get_command(self, request_id: str) -> dict | None:
        with self._lock:
            return self._command_view(
                self._db.execute(
                    "SELECT * FROM commands WHERE request_id=?", (request_id,)
                ).fetchone()
            )

    def list_commands(self, agent_id: str | None = None, limit: int = 100) -> list[dict]:
        with self._lock, self._db:
            self._timeout_expired(time.time())
            if agent_id:
                rows = self._db.execute(
                    """
                    SELECT * FROM commands WHERE agent_id=?
                    ORDER BY requested_at DESC LIMIT ?
                    """,
                    (agent_id, limit),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM commands ORDER BY requested_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [self._command_view(row) for row in rows]

    def agent_snapshot(self, *, now: float | None = None, offline_after: float = 30) -> list[dict]:
        current_time = time.time() if now is None else now
        with self._lock, self._db:
            self._timeout_expired(current_time)
            agents = self._db.execute("SELECT * FROM agents ORDER BY agent_id").fetchall()
            snapshots = []
            for agent in agents:
                if agent["agent_id"] in self._last_seen_monotonic:
                    clock = time.monotonic() if now is None else now
                    seconds_since_heartbeat = (
                        clock - self._last_seen_monotonic[agent["agent_id"]]
                    )
                else:
                    clock = time.time() if now is None else now
                    seconds_since_heartbeat = clock - agent["last_seen"]
                command = self._db.execute(
                    """
                    SELECT * FROM commands WHERE agent_id=?
                    ORDER BY requested_at DESC LIMIT 1
                    """,
                    (agent["agent_id"],),
                ).fetchone()
                snapshots.append(
                    {
                        "agent_id": agent["agent_id"],
                        "last_heartbeat": agent["last_heartbeat"],
                        "cpu_usage": agent["cpu_usage"],
                        "memory_usage": agent["memory_usage"],
                        "disk_usage": agent["disk_usage"],
                        "status": "ONLINE"
                        if seconds_since_heartbeat < offline_after
                        else "OFFLINE",
                        "alert_status": "WARNING"
                        if agent["disk_usage"] > 90
                        else "NORMAL",
                        "command": self._command_view(command),
                    }
                )
            return snapshots

    def close(self):
        with self._lock:
            self._db.close()
