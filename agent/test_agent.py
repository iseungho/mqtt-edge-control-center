import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from agent import AGENT_ID, RESPONSE_TOPIC, on_message


class AgentCommandTest(unittest.TestCase):
    def test_valid_command_responds_and_invalid_commands_are_ignored(self):
        client = Mock()
        command = {"request_id": "request-1", "command": "RESTART"}

        on_message(
            client,
            None,
            SimpleNamespace(payload=json.dumps(command).encode()),
        )

        topic, payload = client.publish.call_args.args
        response = json.loads(payload)
        self.assertEqual(topic, RESPONSE_TOPIC)
        self.assertEqual(client.publish.call_args.kwargs["qos"], 1)
        self.assertEqual(response["request_id"], command["request_id"])
        self.assertEqual(response["agent_id"], AGENT_ID)
        self.assertEqual(response["status"], "SUCCESS")

        for payload in (
            b"not-json",
            b'{"request_id":"","command":"RESTART"}',
            b'{"request_id":"request-2","command":"STOP"}',
        ):
            with self.subTest(payload=payload):
                client.reset_mock()
                on_message(client, None, SimpleNamespace(payload=payload))
                client.publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
