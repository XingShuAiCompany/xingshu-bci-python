from xingshu_bci import Event, SmoothedFocusStream


class FocusClient:
    def __init__(self, revisions):
        self.revisions = revisions

    def events(self, types):
        assert types == ["analysis"]
        for revision in self.revisions:
            payload = {"focus": {"state": "ready", "prediction": 1.0}}
            if revision is not None:
                session, epoch, focus = revision
                payload["runtime"] = {
                    "sessionEpoch": session, "analysisEpoch": epoch,
                    "revisions": {"focus": focus},
                }
            yield Event(type="analysis", payload=payload)

    def get_analysis_config(self):
        return {
            "source": "filtered",
            "fft": {"maxFreqHz": 80},
            "focus": {"metric": "relaxation", "windowSec": 10},
        }

    def set_analysis_config(self, **payload):
        self.updated_config = payload
        return payload


def test_focus_smoothing_ignores_republished_results():
    stream = SmoothedFocusStream(
        FocusClient([(1, 1, 1), (1, 1, 1), (1, 1, 2)]),
        smoothing=0.5, min_interval_s=0, auto_configure=False,
    )
    assert [update.smoothed for update in stream] == [0.5, 0.75]


def test_focus_revisions_restart_with_session_or_configuration():
    stream = SmoothedFocusStream(
        FocusClient([(1, 1, 1), (1, 2, 1), (2, 1, 1)]),
        smoothing=0.5, min_interval_s=0, auto_configure=False,
    )
    assert [update.smoothed for update in stream] == [0.5, 0.5, 0.5]


def test_focus_accepts_older_servers_without_revision_metadata():
    stream = SmoothedFocusStream(
        FocusClient([None, None]),
        smoothing=0.5, min_interval_s=0, auto_configure=False,
    )
    assert [update.smoothed for update in stream] == [0.5, 0.75]


def test_focus_auto_configure_preserves_the_full_analysis_config():
    client = FocusClient([])
    list(SmoothedFocusStream(client, metric="concentration", auto_configure=True))
    assert client.updated_config["source"] == "filtered"
    assert client.updated_config["fft"] == {"maxFreqHz": 80}
    assert client.updated_config["focus"]["metric"] == "concentration"
