import json
import tempfile
import unittest
from pathlib import Path

from protocol import parse_heartbeat, parse_response, validate_command
from storage import StateStore


HEARTBEAT = {
    "agent_id": "agent-new",
    "timestamp": "2026-11-06T00:00:00+00:00",
    "cpu_usage": 10.0,
    "memory_usage": 20.0,
    "disk_usage": 30.0,
}


class StateStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp_dir.name) / "state.db")
        self.store = StateStore(self.path)

    def tearDown(self):
        self.store.close()
        self.temp_dir.cleanup()

    def test_dynamic_agent_and_command_survive_reopen(self):
        agent_id, heartbeat = parse_heartbeat(
            "agents/agent-new/heartbeat", json.dumps(HEARTBEAT).encode()
        )
        self.store.record_heartbeat(
            agent_id,
            heartbeat,
            now=100,
            received_at=heartbeat["timestamp"],
        )
        command = self.store.begin_command(
            agent_id,
            "request-1",
            "GET_SYSTEM_INFO",
            {},
            timeout=10,
            now=100,
            requested_at="2026-11-06T00:00:01+00:00",
        )
        self.assertEqual(command["status"], "PENDING")

        running = {
            "request_id": "request-1",
            "agent_id": agent_id,
            "command": "GET_SYSTEM_INFO",
            "status": "RUNNING",
            "responded_at": "2026-11-06T00:00:02+00:00",
        }
        self.assertTrue(self.store.apply_response(agent_id, running, now=101))
        completed = running | {
            "status": "SUCCESS",
            "result": {"system": "Linux"},
            "responded_at": "2026-11-06T00:00:03+00:00",
        }
        self.assertTrue(self.store.apply_response(agent_id, completed, now=102))
        self.store.close()

        self.store = StateStore(self.path)
        snapshot = self.store.agent_snapshot(now=102)
        self.assertEqual(snapshot[0]["agent_id"], "agent-new")
        self.assertEqual(snapshot[0]["status"], "ONLINE")
        self.assertEqual(snapshot[0]["command"]["status"], "SUCCESS")
        self.assertEqual(snapshot[0]["command"]["result"], {"system": "Linux"})

    def test_timeout_and_single_active_command(self):
        self.store.record_heartbeat("agent-new", HEARTBEAT, now=200)
        self.store.begin_command(
            "agent-new", "request-1", "RESTART", {}, timeout=10, now=200
        )
        with self.assertRaisesRegex(ValueError, "already running"):
            self.store.begin_command(
                "agent-new", "request-2", "GET_CONFIG", {}, timeout=10, now=201
            )

        command = self.store.agent_snapshot(now=210)[0]["command"]
        self.assertEqual(command["status"], "TIMEOUT")
        late = {
            "request_id": "request-1",
            "agent_id": "agent-new",
            "command": "RESTART",
            "status": "SUCCESS",
            "result": {"message": "restart simulated"},
            "responded_at": "2026-11-06T00:00:20+00:00",
        }
        self.assertFalse(self.store.apply_response("agent-new", late, now=211))

    def test_protocol_rejects_spoofed_agent_and_bad_interval(self):
        with self.assertRaisesRegex(ValueError, "do not match"):
            parse_heartbeat(
                "agents/agent-new/heartbeat",
                json.dumps(HEARTBEAT | {"agent_id": "agent-other"}).encode(),
            )
        with self.assertRaisesRegex(ValueError, "FAILED response requires"):
            parse_response(
                "agents/agent-new/responses",
                json.dumps(
                    {
                        "request_id": "request-1",
                        "agent_id": "agent-new",
                        "command": "RESTART",
                        "status": "FAILED",
                        "responded_at": "2026-11-06T00:00:00+00:00",
                    }
                ).encode(),
            )
        with self.assertRaisesRegex(ValueError, "1 to 3600"):
            validate_command("SET_HEARTBEAT_INTERVAL", {"interval": 0})


if __name__ == "__main__":
    unittest.main()
