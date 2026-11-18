import json

import paho.mqtt.client as mqtt

from protocol import parse_heartbeat, parse_response


HEARTBEAT_TOPIC = "agents/+/heartbeat"
RESPONSE_TOPIC = "agents/+/responses"


class MqttService:
    def __init__(self, host: str, port: int, username: str, password: str, store):
        self.host = host
        self.port = port
        self.store = store
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="control-server",
        )
        self.client.username_pw_set(username, password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=10)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            client.subscribe(HEARTBEAT_TOPIC, qos=1)
            client.subscribe(RESPONSE_TOPIC, qos=1)
            print(f"[server] subscribed to {HEARTBEAT_TOPIC} and {RESPONSE_TOPIC}")
        else:
            print(f"[server] MQTT connection failed: {reason_code}")

    @staticmethod
    def _on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
        if reason_code != 0:
            print(f"[server] disconnected unexpectedly; reconnecting: {reason_code}")

    def _on_message(self, client, userdata, message):
        try:
            if message.topic.endswith("/heartbeat"):
                agent_id, heartbeat = parse_heartbeat(message.topic, message.payload)
                self.store.record_heartbeat(agent_id, heartbeat)
                print(f"[server] heartbeat received from {agent_id}")
            elif message.topic.endswith("/responses"):
                agent_id, response = parse_response(message.topic, message.payload)
                if self.store.apply_response(agent_id, response):
                    print(
                        f"[server] command {response['status'].lower()} by "
                        f"{agent_id}: {response['request_id']}"
                    )
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            print(f"[server] ignored invalid MQTT message: {error}")

    def start(self):
        self.client.connect_async(self.host, self.port, keepalive=30)
        self.client.loop_start()

    def stop(self):
        self.client.disconnect()
        self.client.loop_stop()

    def is_connected(self) -> bool:
        return self.client.is_connected()

    def publish_command(self, agent_id: str, command: dict) -> bool:
        message = {
            "request_id": command["request_id"],
            "command": command["command"],
            "payload": command["payload"],
            "status": "PENDING",
            "requested_at": command["requested_at"],
        }
        result = self.client.publish(
            f"agents/{agent_id}/commands", json.dumps(message), qos=1
        )
        return result.rc == mqtt.MQTT_ERR_SUCCESS
