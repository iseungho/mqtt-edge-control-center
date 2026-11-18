import json
import platform
import sqlite3
import threading
from datetime import datetime, timezone


SUPPORTED_COMMANDS = {
    "RESTART",
    "GET_SYSTEM_INFO",
    "GET_CONFIG",
    "SET_HEARTBEAT_INTERVAL",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CommandStore:
    def __init__(self, path: str, default_interval: float):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS processed_commands (
                    request_id TEXT PRIMARY KEY,
                    command TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    error TEXT,
                    responded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            self._db.execute(
                "INSERT OR IGNORE INTO config(key, value) VALUES ('heartbeat_interval', ?)",
                (str(default_interval),),
            )
            self._db.execute(
                """
                UPDATE processed_commands
                SET status='FAILED', error='agent restarted before command completion',
                    responded_at=?
                WHERE status='RUNNING'
                """,
                (utc_now(),),
            )

    @staticmethod
    def _view(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {
            "request_id": row["request_id"],
            "command": row["command"],
            "status": row["status"],
            "result": json.loads(row["result"]) if row["result"] else None,
            "error": row["error"],
            "responded_at": row["responded_at"],
        }

    def get(self, request_id: str) -> dict | None:
        with self._lock:
            return self._view(
                self._db.execute(
                    "SELECT * FROM processed_commands WHERE request_id=?", (request_id,)
                ).fetchone()
            )

    def start(self, request_id: str, command: str) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute(
                """
                INSERT OR IGNORE INTO processed_commands (
                    request_id, command, status, result, error, responded_at
                ) VALUES (?, ?, 'RUNNING', NULL, NULL, ?)
                """,
                (request_id, command, utc_now()),
            )
            return cursor.rowcount == 1

    def finish(
        self,
        request_id: str,
        status: str,
        *,
        result: dict | None = None,
        error: str | None = None,
    ) -> dict:
        with self._lock, self._db:
            self._db.execute(
                """
                UPDATE processed_commands
                SET status=?, result=?, error=?, responded_at=?
                WHERE request_id=?
                """,
                (
                    status,
                    json.dumps(result) if result is not None else None,
                    error,
                    utc_now(),
                    request_id,
                ),
            )
        return self.get(request_id)

    def completed(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT * FROM processed_commands
                WHERE status IN ('SUCCESS', 'FAILED')
                ORDER BY responded_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._view(row) for row in rows]

    def get_interval(self) -> float:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM config WHERE key='heartbeat_interval'"
            ).fetchone()
            return float(row["value"])

    def set_interval(self, interval: float):
        with self._lock, self._db:
            self._db.execute(
                "UPDATE config SET value=? WHERE key='heartbeat_interval'",
                (str(interval),),
            )

    def close(self):
        with self._lock:
            self._db.close()


class CommandProcessor:
    def __init__(self, agent_id: str, store: CommandStore):
        self.agent_id = agent_id
        self.store = store

    def _response(self, response: dict) -> dict:
        return {"agent_id": self.agent_id, **response}

    def replay_responses(self) -> list[dict]:
        return [self._response(response) for response in self.store.completed()]

    def handle(self, payload: bytes) -> list[dict]:
        command = json.loads(payload)
        if not isinstance(command, dict):
            raise ValueError("command must be a JSON object")
        request_id = command.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must not be empty")
        command_name = command.get("command")
        if command_name not in SUPPORTED_COMMANDS:
            raise ValueError("unsupported command")
        command_payload = command.get("payload", {})
        if not isinstance(command_payload, dict):
            raise ValueError("payload must be a JSON object")

        existing = self.store.get(request_id)
        if existing is not None:
            return [self._response(existing)]
        if not self.store.start(request_id, command_name):
            return [self._response(self.store.get(request_id))]

        running = self._response(self.store.get(request_id))
        try:
            result = self._execute(command_name, command_payload)
            completed = self.store.finish(request_id, "SUCCESS", result=result)
        except (TypeError, ValueError) as error:
            completed = self.store.finish(request_id, "FAILED", error=str(error))
        return [running, self._response(completed)]

    def _execute(self, command: str, payload: dict) -> dict:
        if command == "RESTART":
            if payload:
                raise ValueError("RESTART does not accept payload fields")
            return {"message": "restart simulated"}
        if command == "GET_SYSTEM_INFO":
            if payload:
                raise ValueError("GET_SYSTEM_INFO does not accept payload fields")
            return {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "python": platform.python_version(),
            }
        if command == "GET_CONFIG":
            if payload:
                raise ValueError("GET_CONFIG does not accept payload fields")
            return {"heartbeat_interval": self.store.get_interval()}

        interval = payload.get("interval")
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 1 <= interval <= 3600:
            raise ValueError("interval must be a number from 1 to 3600")
        self.store.set_interval(float(interval))
        return {"heartbeat_interval": float(interval)}
