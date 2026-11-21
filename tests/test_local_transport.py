from __future__ import annotations

import json
import unittest
from unittest import mock

from xingshu_bci import XingShuClient


class FakeWebSocket:
    def __init__(self) -> None:
        self.closed = False

    def recv(self):
        return json.dumps({"type": "status", "payload": {"state": "connected"}})

    def close(self) -> None:
        self.closed = True


class LocalTransportTest(unittest.TestCase):
    def test_http_client_ignores_environment_proxies_by_default(self):
        with mock.patch("xingshu_bci.client.httpx.Client") as client_factory:
            client = XingShuClient("http://127.0.0.1:54321", "xs_live_test")
        self.assertFalse(client_factory.call_args.kwargs["trust_env"])
        client.close()

    def test_websocket_ignores_environment_proxies_by_default(self):
        fake_ws = FakeWebSocket()
        direct_socket = mock.Mock()
        with mock.patch("xingshu_bci.client.httpx.Client"), mock.patch(
            "xingshu_bci.client.socket.create_connection",
            return_value=direct_socket,
        ) as create_socket, mock.patch(
            "xingshu_bci.client.websocket.create_connection",
            return_value=fake_ws,
        ) as create_connection:
            client = XingShuClient("http://127.0.0.1:54321", "xs_live_test")
            events = client.events(types=["status"], timeout=1.0)
            event = next(events)
            events.close()

        self.assertEqual(event.type, "status")
        create_socket.assert_called_once_with(("127.0.0.1", 54321), timeout=1.0)
        self.assertIs(create_connection.call_args.kwargs["socket"], direct_socket)
        self.assertTrue(fake_ws.closed)
        client.close()

    def test_proxy_inheritance_can_be_enabled_explicitly(self):
        with mock.patch("xingshu_bci.client.httpx.Client") as client_factory:
            client = XingShuClient(
                "http://127.0.0.1:54321",
                "xs_live_test",
                trust_env=True,
            )
        self.assertTrue(client_factory.call_args.kwargs["trust_env"])
        client.close()


if __name__ == "__main__":
    unittest.main()
