import json
import tempfile
import unittest
from pathlib import Path

from commands import CommandProcessor, CommandStore


class AgentCommandTest(unittest.TestCase):
    def setUp(self):
        self.store = CommandStore(":memory:", 10)
        self.processor = CommandProcessor("agent-a", self.store)

    def tearDown(self):
        self.store.close()

    def test_commands_report_running_then_success(self):
        for name, payload in (
            ("RESTART", {}),
            ("GET_SYSTEM_INFO", {}),
            ("GET_CONFIG", {}),
            ("SET_HEARTBEAT_INTERVAL", {"interval": 15}),
        ):
            with self.subTest(command=name):
                responses = self.processor.handle(
                    json.dumps(
                        {
                            "request_id": f"request-{name}",
                            "command": name,
                            "payload": payload,
                        }
                    ).encode()
                )
                self.assertEqual([item["status"] for item in responses], ["RUNNING", "SUCCESS"])
                self.assertEqual(responses[-1]["agent_id"], "agent-a")
                self.assertIsInstance(responses[-1]["result"], dict)
        self.assertEqual(self.store.get_interval(), 15)

    def test_duplicate_request_returns_saved_result_without_execution(self):
        payload = json.dumps(
            {
                "request_id": "request-duplicate",
                "command": "SET_HEARTBEAT_INTERVAL",
                "payload": {"interval": 20},
            }
        ).encode()
        first = self.processor.handle(payload)
        duplicate = self.processor.handle(payload)

        self.assertEqual(first[-1], duplicate[0])
        self.assertEqual(len(duplicate), 1)
        self.assertEqual(self.store.get_interval(), 20)

    def test_invalid_command_is_rejected(self):
        for payload in (
            b"not-json",
            b'{"request_id":"","command":"RESTART"}',
            b'{"request_id":"request-1","command":"STOP"}',
        ):
            with self.subTest(payload=payload), self.assertRaises((ValueError, json.JSONDecodeError)):
                self.processor.handle(payload)

        failed = self.processor.handle(
            b'{"request_id":"request-2","command":"SET_HEARTBEAT_INTERVAL","payload":{"interval":0}}'
        )
        self.assertEqual(failed[-1]["status"], "FAILED")
        self.assertIn("1 to 3600", failed[-1]["error"])

    def test_interrupted_running_command_becomes_failed_after_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "agent.db")
            first = CommandStore(path, 10)
            first.start("request-running", "RESTART")
            first.close()

            reopened = CommandStore(path, 10)
            recovered = reopened.get("request-running")
            reopened.close()

        self.assertEqual(recovered["status"], "FAILED")
        self.assertIn("restarted", recovered["error"])


if __name__ == "__main__":
    unittest.main()
