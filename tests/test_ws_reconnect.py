"""WebSocket authentication, reconnection and backoff behaviour."""

from __future__ import annotations

import json
import threading
import unittest
from unittest import mock

import websocket

from xingshu_bci import XingShuClient, XingShuConnectionError


def make_client() -> XingShuClient:
    with mock.patch("xingshu_bci.client.httpx.Client"):
        return XingShuClient("http://127.0.0.1:54321", "xs_live_test")


class ScriptedWebSocket:
    """recv() plays back a script of ("event", dict) / ("close",) / ("drop",) steps."""

    def __init__(self, script):
        self.script = list(script)
        self.closed = False

    def recv(self):
        if not self.script:
            raise websocket.WebSocketConnectionClosedException("script exhausted")
        action = self.script.pop(0)
        if action[0] == "event":
            return json.dumps(action[1])
        if action[0] == "close":
            raise websocket.WebSocketConnectionClosedException("closed by script")
        if action[0] == "drop":
            raise OSError("socket reset by script")
        raise AssertionError(f"unknown script action: {action}")

    def close(self):
        self.closed = True


def patched_environment(connections):
    """Patch socket + websocket creation; returns the create_connection mock."""
    return (
        mock.patch("xingshu_bci.client.socket.create_connection", return_value=mock.Mock()),
        mock.patch("xingshu_bci.client.websocket.create_connection", side_effect=connections),
        mock.patch.object(XingShuClient, "_wait_before_reconnect"),
    )


class WebSocketAuthTest(unittest.TestCase):
    def test_derived_websocket_url_preserves_api_path_prefix(self):
        self.assertEqual(
            XingShuClient._derive_ws_url("https://api.example.test/platform/"),
            "wss://api.example.test/platform/v1/events",
        )

    def test_token_travels_in_authorization_header_not_url(self):
        ws = ScriptedWebSocket([("event", {"type": "status", "payload": {}})])
        socket_patch, connect_patch, wait_patch = patched_environment([ws])
        with socket_patch, connect_patch as create_connection, wait_patch:
            client = make_client()
            events = client.events(types=["status"], timeout=1.0)
            event = next(events)
            events.close()
        self.assertEqual(event.type, "status")
        url = create_connection.call_args.args[0]
        self.assertNotIn("token=", url)
        self.assertIn("events=status", url)
        self.assertEqual(
            create_connection.call_args.kwargs["header"],
            ["Authorization: Bearer xs_live_test"],
        )

    def test_query_token_fallback_is_url_encoded(self):
        ws = ScriptedWebSocket([("event", {"type": "status", "payload": {}})])
        socket_patch, connect_patch, wait_patch = patched_environment([ws])
        with socket_patch, connect_patch as create_connection, wait_patch:
            with mock.patch("xingshu_bci.client.httpx.Client"):
                client = XingShuClient("http://127.0.0.1:54321", "xs live/+token")
            events = client.events(types=["status"], timeout=1.0, token_in_query=True)
            next(events)
            events.close()
        url = create_connection.call_args.args[0]
        self.assertIn("token=xs%20live%2F%2Btoken", url)
        self.assertNotIn("header", create_connection.call_args.kwargs)


class WebSocketReconnectTest(unittest.TestCase):
    def test_reconnects_after_drop_and_reports_disconnect_cause(self):
        first = ScriptedWebSocket([("event", {"type": "status", "seq": 1, "payload": {}}), ("drop",)])
        second = ScriptedWebSocket([("event", {"type": "status", "seq": 1, "payload": {}}), ("close",)])
        disconnects = []
        socket_patch, connect_patch, wait_patch = patched_environment([first, second, StopIteration])
        with socket_patch, connect_patch, wait_patch as wait_mock:
            client = make_client()
            events = client.events(
                types=["status"],
                timeout=1.0,
                on_disconnect=disconnects.append,
                max_reconnect_attempts=1,
            )
            received = [next(events), next(events)]
            events.close()
        self.assertEqual(len(received), 2)
        self.assertEqual(len(disconnects), 1)
        self.assertIsInstance(disconnects[0], OSError)
        self.assertTrue(first.closed)
        # The reconnect waited exactly once, with the initial delay.
        wait_mock.assert_called_once()
        self.assertEqual(wait_mock.call_args.args[0], 0.5)

    def test_backoff_doubles_until_exhaustion(self):
        first = ScriptedWebSocket([("close",)])
        failures = [OSError("refused")] * 3
        socket_patch, connect_patch, wait_patch = patched_environment([first, *failures])
        with socket_patch, connect_patch, wait_patch as wait_mock:
            client = make_client()
            events = client.events(timeout=1.0, max_reconnect_attempts=3)
            with self.assertRaises(XingShuConnectionError) as raised:
                list(events)
        self.assertEqual(raised.exception.code, "WS_RECONNECT_EXHAUSTED")
        delays = [call.args[0] for call in wait_mock.call_args_list]
        self.assertEqual(delays, [0.5, 1.0, 2.0])

    def test_backoff_delay_is_capped(self):
        first = ScriptedWebSocket([("close",)])
        failures = [OSError("refused")] * 7
        socket_patch, connect_patch, wait_patch = patched_environment([first, *failures])
        with socket_patch, connect_patch, wait_patch as wait_mock:
            client = make_client()
            events = client.events(timeout=1.0, max_reconnect_attempts=7, reconnect_max_delay_s=2.0)
            with self.assertRaises(XingShuConnectionError):
                list(events)
        delays = [call.args[0] for call in wait_mock.call_args_list]
        self.assertEqual(max(delays), 2.0)

    def test_first_connection_failure_raises_immediately(self):
        socket_patch, connect_patch, wait_patch = patched_environment([OSError("refused")])
        with socket_patch, connect_patch, wait_patch as wait_mock:
            client = make_client()
            with self.assertRaises(XingShuConnectionError) as raised:
                next(client.events(timeout=1.0))
        self.assertEqual(raised.exception.code, "WS_CONNECT_FAILED")
        wait_mock.assert_not_called()

    def test_reconnect_disabled_restores_old_terminating_behaviour(self):
        ws = ScriptedWebSocket([("event", {"type": "status", "payload": {}}), ("close",)])
        socket_patch, connect_patch, wait_patch = patched_environment([ws, StopIteration])
        with socket_patch, connect_patch as create_connection, wait_patch:
            client = make_client()
            received = list(client.events(timeout=1.0, reconnect=False))
        self.assertEqual(len(received), 1)
        self.assertEqual(create_connection.call_count, 1)

    def test_stop_event_stops_across_reconnects(self):
        stop = threading.Event()

        class StoppingWebSocket(ScriptedWebSocket):
            def recv(self):
                result = super().recv()
                stop.set()
                return result

        ws = StoppingWebSocket([("event", {"type": "status", "payload": {}})])
        socket_patch, connect_patch, wait_patch = patched_environment([ws, StopIteration])
        with socket_patch, connect_patch, wait_patch:
            client = make_client()
            received = list(client.events(timeout=1.0, stop_event=stop))
        self.assertEqual(len(received), 1)
        self.assertTrue(ws.closed)

    def test_successful_reconnect_resets_the_failure_budget(self):
        first = ScriptedWebSocket([("close",)])
        second = ScriptedWebSocket([("event", {"type": "status", "payload": {}}), ("close",)])
        third = ScriptedWebSocket([("event", {"type": "status", "payload": {}}), ("close",)])
        socket_patch, connect_patch, wait_patch = patched_environment(
            [first, OSError("refused"), second, OSError("refused"), third, StopIteration]
        )
        with socket_patch, connect_patch, wait_patch as wait_mock:
            client = make_client()
            events = client.events(timeout=1.0, max_reconnect_attempts=2)
            received = [next(events), next(events)]
            events.close()
        self.assertEqual(len(received), 2)
        delays = [call.args[0] for call in wait_mock.call_args_list]
        # Each drop restarts the backoff at the initial delay.
        self.assertEqual(delays, [0.5, 1.0, 0.5, 1.0])


if __name__ == "__main__":
    unittest.main()
