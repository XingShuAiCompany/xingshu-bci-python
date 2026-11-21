from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

from xingshu_bci import (
    ExperimentModelRegistration,
    SsvepPrediction,
    SsvepTrialWindow,
    XingShuClient,
    XingShuError,
)


TRIAL_PAYLOAD = {
    "modelId": "model_1",
    "runId": "run_1",
    "trialId": "trial_1",
    "attempt": 1,
    "attemptId": "trial_1_attempt_01",
    "experimentId": "experiment_1",
    "sampleRate": 250,
    "channelLabels": ["O1", "O2"],
    "data": [[1.0, 2.0], [3.0, 4.0]],
    "targets": [
        {
            "id": "target_8hz",
            "label": "8 Hz",
            "actionKey": "left",
            "frequencyHz": 8,
            "phasePi": 0,
        }
    ],
    "sampleIndexStart": 100,
    "sampleIndexEnd": 102,
    "windowStartTimestampMs": 1000,
    "windowEndTimestampMs": 1008,
    "stimulusFirstFrameTimestampMs": 500,
    "deadlineTimestampMs": 2000,
    "metadata": {"mode": "free"},
}


class RecordingClient(XingShuClient):
    def __init__(self) -> None:
        self.calls = []

    def _json(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path.endswith("/register"):
            return {
                "modelId": "model_1",
                "name": "test",
                "version": "1.0",
                "paradigm": "ssvep",
                "registeredAt": "now",
                "lastSeenAt": "now",
                "status": "online",
            }
        return {"ok": True}


class ServingClient(RecordingClient):
    def __init__(self) -> None:
        super().__init__()
        self.submissions = []
        self.unregistered = []
        self.heartbeats = 0

    def heartbeat_experiment_model(self, model_id):
        self.heartbeats += 1
        return {"ok": True}

    def unregister_experiment_model(self, model_id):
        self.unregistered.append(model_id)
        return {"ok": True}

    def submit_experiment_prediction(self, run_id, trial_id, prediction, *, model_id=None, attempt_id=None):
        self.submissions.append((run_id, trial_id, prediction, model_id, attempt_id))
        return {"ok": True}

    def events(self, **kwargs):
        time.sleep(0.025)
        yield SimpleNamespace(
            type="experiment.trial.window",
            payload={**TRIAL_PAYLOAD, "modelId": "another_model"},
        )
        yield SimpleNamespace(type="experiment.trial.window", payload=TRIAL_PAYLOAD)


class ExperimentModelsTest(unittest.TestCase):
    def test_trial_window_parses_channel_major_payload(self):
        window = SsvepTrialWindow.from_dict(TRIAL_PAYLOAD)
        self.assertEqual(window.model_id, "model_1")
        self.assertEqual(window.modelId, "model_1")
        self.assertEqual(window.attemptId, "trial_1_attempt_01")
        self.assertEqual(window.channel_labels, ["O1", "O2"])
        self.assertEqual(window.data[1][0], 3.0)
        self.assertEqual(window.metadata["mode"], "free")

    def test_trial_window_rejects_malformed_shape_and_values(self):
        for patch in (
            {"sampleRate": 0},
            {"channelLabels": ["O1"], "data": [[1.0], [2.0]]},
            {"channelLabels": ["O1", "O2"], "data": [[1.0], [2.0, 3.0]]},
            {"data": [[float("nan"), 1.0], [2.0, 3.0]]},
        ):
            payload = dict(TRIAL_PAYLOAD)
            payload.update(patch)
            with self.assertRaises(ValueError):
                SsvepTrialWindow.from_dict(payload)

    def test_prediction_normalizes_abstention_for_http(self):
        prediction = SsvepPrediction.from_dict({"abstain": True})
        self.assertEqual(
            prediction.to_dict(),
            {"targetId": None, "abstained": True},
        )
        selected = SsvepPrediction.from_dict(
            {"targetId": "target_8hz", "scores": {"target_8hz": 0.8}}
        )
        self.assertEqual(selected.to_dict()["abstained"], False)

    def test_client_encodes_identifiers_and_prediction(self):
        client = RecordingClient()
        registration = client.register_ssvep_model("test", version="1.0")
        self.assertIsInstance(registration, ExperimentModelRegistration)
        client.heartbeat_ssvep_model("model / one")
        client.submit_ssvep_prediction(
            "run / one",
            "trial / one",
            {"targetId": "target_8hz"},
            model_id="model_1",
            attempt_id="trial_1_attempt_01",
        )
        self.assertIn("model%20%2F%20one", client.calls[1][1])
        method, path, kwargs = client.calls[2]
        self.assertEqual(method, "POST")
        self.assertIn("run%20%2F%20one", path)
        self.assertIn("trial%20%2F%20one", path)
        self.assertEqual(kwargs["json"]["modelId"], "model_1")
        self.assertEqual(kwargs["json"]["abstained"], False)
        self.assertEqual(kwargs["json"]["attemptId"], "trial_1_attempt_01")
        with self.assertRaisesRegex(ValueError, "attempt_id is required"):
            client.submit_ssvep_prediction(
                "run_1",
                "trial_1",
                {"targetId": "target_8hz"},
                model_id="model_1",
            )

    def test_serve_processes_only_matching_model_and_unregisters(self):
        client = ServingClient()
        seen = []

        def predict(window):
            seen.append(window.trial_id)
            return {"targetId": "target_8hz", "confidence": 0.9}

        client.serve_ssvep_model(
            "test",
            predict,
            version="1.0",
            heartbeat_interval=0.01,
        )
        self.assertEqual(seen, ["trial_1"])
        self.assertEqual(len(client.submissions), 1)
        self.assertEqual(client.submissions[0][4], "trial_1_attempt_01")
        self.assertGreaterEqual(client.heartbeats, 1)
        self.assertEqual(client.unregistered, ["model_1"])

    def test_serve_isolates_trial_failures_and_keeps_serving(self):
        """A predict_fn crash, a rejected submission, or a malformed window
        must not take the model offline; later trials are still served."""

        windows = [
            {**TRIAL_PAYLOAD, "attemptId": None},  # malformed: missing attemptId
            {**TRIAL_PAYLOAD, "trialId": "trial_boom", "attemptId": "trial_boom_attempt_01"},
            {**TRIAL_PAYLOAD, "trialId": "trial_reject", "attemptId": "trial_reject_attempt_01"},
            {**TRIAL_PAYLOAD, "trialId": "trial_ok", "attemptId": "trial_ok_attempt_01"},
        ]

        class FaultyServingClient(ServingClient):
            def events(self, **kwargs):
                for window in windows:
                    yield SimpleNamespace(type="experiment.trial.window", payload=window)

            def submit_experiment_prediction(self, run_id, trial_id, prediction, *, model_id=None, attempt_id=None):
                if trial_id == "trial_reject":
                    raise XingShuError("late prediction", code="PREDICTION_ATTEMPT_MISMATCH", status=409)
                return super().submit_experiment_prediction(
                    run_id, trial_id, prediction, model_id=model_id, attempt_id=attempt_id
                )

        client = FaultyServingClient()
        served = []

        def predict(window):
            served.append(window.trial_id)
            if window.trial_id == "trial_boom":
                raise RuntimeError("classifier crashed")
            return {"targetId": "target_8hz"}

        with self.assertLogs("xingshu_bci", level="WARNING") as logs:
            client.serve_ssvep_model("test", predict, heartbeat_interval=0.01)

        # The malformed window never reached predict; the other three did.
        self.assertEqual(served, ["trial_boom", "trial_reject", "trial_ok"])
        self.assertEqual(len(client.submissions), 1)
        self.assertEqual(client.submissions[0][1], "trial_ok")
        self.assertEqual(client.submissions[0][4], "trial_ok_attempt_01")
        self.assertEqual(client.unregistered, ["model_1"])
        joined_logs = "\n".join(logs.output)
        self.assertIn("malformed", joined_logs)
        self.assertIn("trial_boom_attempt_01", joined_logs)
        self.assertIn("trial_reject_attempt_01", joined_logs)


if __name__ == "__main__":
    unittest.main()
