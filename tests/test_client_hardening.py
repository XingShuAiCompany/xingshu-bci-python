"""HTTP error-code mapping, healthz discovery, and event payload hygiene."""

from __future__ import annotations

import unittest
from unittest import mock

import httpx

from xingshu_bci import (
    Event,
    XingShuAuthError,
    XingShuConnectionError,
    XingShuError,
    XingShuForbiddenError,
    XingShuNotFoundError,
    XingShuRateLimitError,
    XingShuTimeoutError,
    healthz,
)
from xingshu_bci.client import XingShuClient
from xingshu_bci.events import Event


def make_client() -> XingShuClient:
    with mock.patch("xingshu_bci.client.httpx.Client"):
        return XingShuClient("http://127.0.0.1:54321", "xs_live_test")


def make_response(status: int, code: str = "ERROR") -> httpx.Response:
    return httpx.Response(
        status,
        json={"ok": False, "error": {"code": code, "message": "boom"}, "requestId": "req_1"},
        request=httpx.Request("GET", "http://127.0.0.1:54321/v1/status"),
    )


class ErrorMappingTest(unittest.TestCase):
    def test_http_requests_keep_a_reverse_proxy_path_prefix(self):
        with mock.patch("xingshu_bci.client.httpx.Client") as client_factory:
            client = XingShuClient("https://api.example.test/platform/", "xs_live_test")
        client._http.request.return_value = httpx.Response(
            200,
            json={"ok": True},
            request=httpx.Request("GET", "https://api.example.test/platform/v1/status"),
        )
        client.request("GET", "/v1/status")
        self.assertEqual(client_factory.call_args.kwargs["base_url"], "https://api.example.test/platform/")
        self.assertEqual(client._http.request.call_args.args[1], "v1/status")

    def test_http_statuses_map_to_dedicated_exceptions(self):
        client = make_client()
        expectations = [
            (401, XingShuAuthError),
            (403, XingShuForbiddenError),
            (404, XingShuNotFoundError),
            (408, XingShuTimeoutError),
            (429, XingShuRateLimitError),
            (500, XingShuError),
        ]
        for status, exception_class in expectations:
            with self.assertRaises(exception_class, msg=f"status {status}") as raised:
                client._raise_for_response(make_response(status))
            self.assertEqual(raised.exception.status, status)
            self.assertEqual(raised.exception.request_id, "req_1")

    def test_client_side_timeout_maps_to_timeout_error(self):
        client = make_client()
        client._http.request.side_effect = httpx.ConnectTimeout("timed out")
        with self.assertRaises(XingShuTimeoutError) as raised:
            client.request("GET", "/v1/status")
        self.assertEqual(raised.exception.code, "TIMEOUT")
        # Backwards compatibility: existing connection-error handlers still work.
        self.assertIsInstance(raised.exception, XingShuConnectionError)

    def test_generic_network_failure_still_maps_to_connection_error(self):
        client = make_client()
        client._http.request.side_effect = httpx.ConnectError("refused")
        with self.assertRaises(XingShuConnectionError) as raised:
            client.request("GET", "/v1/status")
        self.assertEqual(raised.exception.code, "CONNECTION_ERROR")


class EventParsingTest(unittest.TestCase):
    def test_malformed_sequence_fields_do_not_break_the_event_stream(self):
        event = Event.from_dict({"type": 42, "seq": "not-a-number", "timestampMs": None, "payload": []})
        self.assertEqual(event.type, "42")
        self.assertEqual(event.seq, 0)
        self.assertEqual(event.timestampMs, 0)
        self.assertEqual(event.payload, {})


class HealthzTest(unittest.TestCase):
    def test_healthz_true_on_200(self):
        with mock.patch("xingshu_bci.client.httpx.get", return_value=mock.Mock(status_code=200)) as get:
            self.assertTrue(healthz("http://127.0.0.1:54321"))
        self.assertEqual(get.call_args.args[0], "http://127.0.0.1:54321/healthz")

    def test_healthz_false_on_network_error(self):
        with mock.patch("xingshu_bci.client.httpx.get", side_effect=httpx.ConnectError("refused")):
            self.assertFalse(healthz("http://127.0.0.1:54321"))

    def test_healthz_uses_bootstrap_when_url_omitted(self):
        with mock.patch(
            "xingshu_bci.client.load_bootstrap",
            return_value={"baseUrl": "http://127.0.0.1:6001"},
        ), mock.patch("xingshu_bci.client.httpx.get", return_value=mock.Mock(status_code=200)) as get:
            self.assertTrue(healthz())
        self.assertEqual(get.call_args.args[0], "http://127.0.0.1:6001/healthz")

    def test_healthz_false_without_bootstrap(self):
        with mock.patch("xingshu_bci.client.load_bootstrap", return_value=None):
            self.assertFalse(healthz())


class EventPayloadTest(unittest.TestCase):
    def test_non_mapping_payloads_normalize_to_empty_dict(self):
        for bad_payload in ([1, 2], "text", 7, None, True):
            event = Event.from_dict({"type": "status", "seq": 1, "payload": bad_payload})
            self.assertEqual(event.payload, {}, f"payload {bad_payload!r} must normalize to {{}}")

    def test_mapping_payload_is_preserved(self):
        event = Event.from_dict({"type": "status", "payload": {"state": "connected"}})
        self.assertEqual(event.payload, {"state": "connected"})


if __name__ == "__main__":
    unittest.main()
