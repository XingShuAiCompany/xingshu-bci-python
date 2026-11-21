from xingshu_bci.models import ServerStatus, XingShuTopology


def test_unknown_topology_never_infers_eight_channels() -> None:
    status = ServerStatus.from_dict(
        {
            "state": "idle",
            "board": "xingshu_n1",
            "capabilities": {
                "topology": {
                    "state": "ambiguous",
                    "detectedBoards": 2,
                    "boardCount": 2,
                    "channelCount": 16,
                    "detectionRaw": 2048,
                    "adsVerified": False,
                }
            },
        }
    )

    assert status.capabilities is not None
    topology = status.capabilities.topology
    assert topology.state == "ambiguous"
    assert topology.detectedBoards == 2
    assert topology.boardCount is None
    assert topology.channelCount is None
    assert not topology.ready


def test_ready_topology_exposes_validated_channel_count() -> None:
    topology = XingShuTopology.from_dict(
        {
            "state": "ready-auto",
            "source": "auto",
            "configuredBoardCount": 2,
            "verifiedBoardCount": 2,
            "boardCount": 2,
            "channelCount": 16,
            "detectionRaw": 3270,
            "detectionValid": True,
            "adsVerified": True,
            "locked": True,
            "epoch": 7,
        }
    )

    assert topology.ready
    assert topology.state == "ready"
    assert topology.configuredBoardCount == 2
    assert topology.verifiedBoardCount == 2
    assert topology.boardCount == 2
    assert topology.channelCount == 16
    assert topology.epoch == 7


def test_manual_topology_accepts_one_or_two_boards_without_adc_detection() -> None:
    topology = XingShuTopology.from_dict(
        {
            "state": "ready-manual",
            "source": "manual",
            "detectedBoardCount": 1,
            "boardCount": 1,
            "channelCount": 8,
            "detectionValid": False,
            "adsVerified": True,
            "locked": True,
            "stale": False,
        }
    )

    assert topology.ready
    assert topology.boardCount == 1
    assert topology.channelCount == 8

    topology_16 = XingShuTopology.from_dict(
        {
            **topology.raw,
            "configuredBoardCount": 2,
            "verifiedBoardCount": 2,
            "boardCount": 2,
            "channelCount": 16,
        }
    )
    assert topology_16.ready
    assert topology_16.boardCount == 2
    assert topology_16.channelCount == 16


def test_recovery_diagnostics_preserve_configured_topology() -> None:
    topology = XingShuTopology.from_dict(
        {
            "state": "recovering",
            "source": "manual",
            "configuredBoardCount": 2,
            "verifiedBoardCount": 0,
            "boardCount": None,
            "channelCount": None,
            "adsVerified": False,
            "locked": True,
            "recoveryAttempts": 2,
            "closeReason": "topology-changed",
            "epoch": 8,
        }
    )

    assert not topology.ready
    assert topology.configuredBoardCount == 2
    assert topology.verifiedBoardCount == 0
    assert topology.boardCount is None
    assert topology.channelCount is None
    assert topology.recoveryAttempts == 2
    assert topology.closeReason == "topology-changed"


def test_ready_requires_valid_locked_fresh_topology() -> None:
    base = {
        "state": "ready",
        "source": "auto",
        "configuredBoardCount": 2,
        "verifiedBoardCount": 2,
        "boardCount": 2,
        "channelCount": 16,
        "detectionValid": True,
        "adsVerified": True,
        "locked": True,
        "stale": False,
    }
    assert XingShuTopology.from_dict(base).ready
    for patch in ({"detectionValid": False}, {"adsVerified": False},
                  {"locked": False}, {"stale": True}):
        topology = XingShuTopology.from_dict({**base, **patch})
        assert not topology.ready
        assert topology.boardCount is None
        assert topology.channelCount is None


def test_n1_pro_fixed_topology_accepts_four_ads1299_boards() -> None:
    topology = XingShuTopology.from_dict(
        {
            "state": "ready",
            "source": "fixed",
            "boardCount": 4,
            "channelCount": 32,
            "adsVerified": True,
            "locked": True,
            "stale": False,
        },
        board_kind="xingshu_n1_pro",
    )

    assert topology.ready
    assert topology.boardKind == "xingshu_n1_pro"
    assert topology.boardCount == 4
    assert topology.channelCount == 32


def test_server_status_propagates_n1_pro_model_into_topology() -> None:
    status = ServerStatus.from_dict(
        {
            "state": "connected",
            "board": "xingshu_n1_pro",
            "sampleRate": 16000,
            "channelCount": 32,
            "capabilities": {
                "model": "XingShuN1Pro",
                "supportedSampleRates": [16000],
                "topology": {
                    "state": "ready",
                    "source": "fixed",
                    "boardCount": 4,
                    "channelCount": 32,
                    "adsVerified": True,
                    "locked": True,
                    "stale": False,
                },
            },
        }
    )

    assert status.capabilities is not None
    assert status.capabilities.board == "xingshu_n1_pro"
    assert status.capabilities.topology.ready
    assert status.capabilities.topology.channelCount == 32
    assert status.capabilities.sampleRates == [16000]
