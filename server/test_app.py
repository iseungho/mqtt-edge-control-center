import json
import unittest

from app import (
    agent_snapshot,
    begin_command,
    complete_command,
    parse_heartbeat,
    parse_response,
    record_heartbeat,
    reset_agents,
)


class AgentStateTest(unittest.TestCase):
    def setUp(self):
        reset_agents()

    def test_agent_becomes_offline_30_seconds_after_heartbeat(self):
        heartbeat = {
            "agent_id": "agent-a",
            "timestamp": "2026-11-01T00:00:00+00:00",
            "cpu_usage": 10.0,
            "memory_usage": 20.0,
            "disk_usage": 30.0,
        }
        agent_id, parsed = parse_heartbeat(
            "agents/agent-a/heartbeat",
            b'{"agent_id":"agent-a","timestamp":"2026-11-01T00:00:00+00:00","cpu_usage":10.0,"memory_usage":20.0,"disk_usage":30.0}',
        )
        record_heartbeat(agent_id, parsed, now=100, received_at=heartbeat["timestamp"])

        self.assertEqual(agent_snapshot(now=129)[0]["status"], "ONLINE")
        self.assertEqual(agent_snapshot(now=130)[0]["status"], "OFFLINE")
        self.assertEqual(agent_snapshot(now=130)[0]["alert_status"], "NORMAL")

        heartbeat["disk_usage"] = 90.1
        record_heartbeat("agent-a", heartbeat, now=131)

        self.assertEqual(agent_snapshot(now=131)[0]["alert_status"], "WARNING")

    def test_command_succeeds_or_times_out_by_request_id(self):
        begin_command(
            "agent-a",
            "request-success",
            now=200,
            requested_at="2026-11-01T00:00:00+00:00",
        )
        agent_id, response = parse_response(
            "agents/agent-a/responses",
            b'{"request_id":"request-success","agent_id":"agent-a","command":"RESTART","status":"SUCCESS","responded_at":"2026-11-01T00:00:01+00:00"}',
        )

        self.assertTrue(complete_command(agent_id, response, now=201))
        self.assertEqual(agent_snapshot(now=201)[0]["command"]["status"], "SUCCESS")

        begin_command("agent-a", "request-timeout", now=300)
        self.assertEqual(agent_snapshot(now=309)[0]["command"]["status"], "PENDING")
        self.assertEqual(agent_snapshot(now=310)[0]["command"]["status"], "TIMEOUT")

        response["request_id"] = "request-timeout"
        self.assertFalse(complete_command("agent-a", response, now=311))

    def test_invalid_mqtt_messages_are_rejected(self):
        heartbeat = {
            "agent_id": "agent-a",
            "timestamp": "2026-11-01T00:00:00+00:00",
            "cpu_usage": 10.0,
            "memory_usage": 20.0,
            "disk_usage": 30.0,
        }
        invalid_heartbeats = (
            heartbeat | {"agent_id": "agent-b"},
            heartbeat | {"disk_usage": 100.1},
        )

        for payload in invalid_heartbeats:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    parse_heartbeat(
                        "agents/agent-a/heartbeat",
                        json.dumps(payload).encode(),
                    )

        invalid_response = {
            "request_id": "request-invalid",
            "agent_id": "agent-b",
            "command": "RESTART",
            "status": "SUCCESS",
            "responded_at": "2026-11-01T00:00:01+00:00",
        }
        with self.assertRaises(ValueError):
            parse_response(
                "agents/agent-a/responses",
                json.dumps(invalid_response).encode(),
            )

    def test_second_pending_command_is_rejected(self):
        begin_command("agent-a", "request-first", now=400)

        with self.assertRaisesRegex(ValueError, "already pending"):
            begin_command("agent-a", "request-second", now=401)

        command = agent_snapshot(now=401)[0]["command"]
        self.assertEqual(command["request_id"], "request-first")
        self.assertEqual(command["status"], "PENDING")


if __name__ == "__main__":
    unittest.main()
