"""SDK <-> docs/OPENAPI.yaml contract tests.

These tests pin the experiment prediction contract (attemptId is required in
both directions) and keep the SDK models aligned with the published schema.
"""

from __future__ import annotations

import re
import unittest
from dataclasses import fields
from pathlib import Path

from xingshu_bci import SsvepPrediction, SsvepTrialWindow, XingShuClient

try:
    import yaml
except ImportError:  # pragma: no cover - dev extra not installed
    yaml = None


OPENAPI_PATH = Path(__file__).resolve().parents[1] / "docs" / "OPENAPI.yaml"

TRIAL_WINDOW_PAYLOAD = {
    "modelId": "model_1",
    "runId": "run_1",
    "trialId": "trial_0001",
    "attempt": 1,
    "attemptId": "trial_0001_attempt_01",
    "experimentId": "experiment_1",
    "sampleRate": 250,
    "channelLabels": ["O1", "O2"],
    "data": [[1.0, 2.0], [3.0, 4.0]],
    "targets": [
        {"id": "target_8hz", "label": "8 Hz", "actionKey": "left", "frequencyHz": 8, "phasePi": 0}
    ],
}


def camel_case(snake_name: str) -> str:
    return re.sub(r"_(\w)", lambda match: match.group(1).upper(), snake_name)


class BodyCapturingClient(XingShuClient):
    def __init__(self) -> None:  # bypass HTTP client construction
        self.calls = []

    def _json(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return {"ok": True}


@unittest.skipUnless(OPENAPI_PATH.is_file(), "docs/OPENAPI.yaml is not present in this checkout")
@unittest.skipIf(yaml is None, "pyyaml is required for the OpenAPI contract test")
class OpenApiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
        cls.schemas = cls.spec["components"]["schemas"]

    # ── SsvepTrialWindow ────────────────────────────────────────────────

    def test_trial_window_schema_requires_attempt_id(self) -> None:
        required = set(self.schemas["SsvepTrialWindow"]["required"])
        self.assertLessEqual(
            {
                "modelId",
                "runId",
                "trialId",
                "attemptId",
                "experimentId",
                "sampleRate",
                "channelLabels",
                "data",
                "targets",
            },
            required,
        )

    def test_sdk_rejects_windows_missing_required_identifiers(self) -> None:
        # The SDK enforces the same identifier requirements the schema declares.
        for missing in ("modelId", "runId", "trialId", "attemptId"):
            payload = {key: value for key, value in TRIAL_WINDOW_PAYLOAD.items() if key != missing}
            with self.assertRaises(ValueError, msg=f"missing {missing} must be rejected"):
                SsvepTrialWindow.from_dict(payload)
        window = SsvepTrialWindow.from_dict(TRIAL_WINDOW_PAYLOAD)
        self.assertEqual(window.attempt_id, "trial_0001_attempt_01")

    def test_sdk_window_fields_all_exist_in_schema(self) -> None:
        properties = set(self.schemas["SsvepTrialWindow"]["properties"])
        model_fields = {camel_case(item.name) for item in fields(SsvepTrialWindow) if item.name != "raw"}
        self.assertLessEqual(model_fields, properties, "SDK parses a field the schema does not document")

    # ── SsvepPredictionRequest ──────────────────────────────────────────

    def test_prediction_schema_requires_model_and_attempt(self) -> None:
        required = set(self.schemas["SsvepPredictionRequest"]["required"])
        self.assertLessEqual({"modelId", "attemptId"}, required)

    def test_client_submission_body_matches_schema_properties(self) -> None:
        client = BodyCapturingClient()
        client.submit_experiment_prediction(
            "run_1",
            "trial_0001",
            SsvepPrediction(
                target_id="target_8hz",
                scores={"target_8hz": 0.9},
                confidence=0.9,
                metadata={"algorithm": "test"},
            ),
            model_id="model_1",
            attempt_id="trial_0001_attempt_01",
        )
        _method, _path, kwargs = client.calls[0]
        body = kwargs["json"]
        properties = set(self.schemas["SsvepPredictionRequest"]["properties"])
        self.assertLessEqual(set(body), properties, "SDK sends a field the schema does not document")
        self.assertEqual(body["attemptId"], "trial_0001_attempt_01")
        self.assertEqual(body["modelId"], "model_1")

    def test_client_requires_attempt_id_like_the_server(self) -> None:
        client = BodyCapturingClient()
        with self.assertRaisesRegex(ValueError, "attempt_id is required"):
            client.submit_experiment_prediction(
                "run_1",
                "trial_0001",
                {"targetId": "target_8hz"},
                model_id="model_1",
            )

    def test_client_prediction_path_matches_schema_path(self) -> None:
        template = "/v1/experiments/runs/{runId}/trials/{trialId}/prediction"
        self.assertIn(template, self.spec["paths"])
        client = BodyCapturingClient()
        client.submit_experiment_prediction(
            "run_1",
            "trial_0001",
            {"targetId": "target_8hz"},
            model_id="model_1",
            attempt_id="trial_0001_attempt_01",
        )
        _method, path, _kwargs = client.calls[0]
        self.assertEqual(path, template.format(runId="run_1", trialId="trial_0001"))

    # ── WebSocket auth contract ─────────────────────────────────────────

    def test_events_endpoint_documents_header_authentication(self) -> None:
        description = self.spec["paths"]["/v1/events"]["get"]["description"]
        self.assertIn("Authorization", description)
        self.assertIn("URL-encoded", description)


if __name__ == "__main__":
    unittest.main()
