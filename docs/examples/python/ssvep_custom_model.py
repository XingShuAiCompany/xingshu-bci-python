"""Serve a custom SSVEP classifier through the XingShu BCI local API.

Replace ``predict`` with an FBCCA or trained model implementation. The example
chooses the first target so the complete model registration and prediction
flow can be tested without additional dependencies.
"""

from xingshu_bci import SsvepPrediction, SsvepTrialWindow, XingShuClient


def predict(window: SsvepTrialWindow) -> SsvepPrediction:
    print(
        f"trial={window.trial_id} channels={len(window.channel_labels)} "
        f"samples={len(window.data[0]) if window.data else 0}"
    )
    if not window.targets:
        return SsvepPrediction(abstain=True, metadata={"reason": "no_targets"})

    selected = window.targets[0]["id"]
    scores = {str(target["id"]): 0.0 for target in window.targets}
    scores[selected] = 1.0
    return SsvepPrediction(
        target_id=selected,
        scores=scores,
        confidence=1.0,
        metadata={"algorithm": "example-first-target"},
    )


def main() -> None:
    with XingShuClient.auto() as client:
        print("Model online. Select it in an SSVEP experiment; Ctrl+C stops it.")
        client.serve_ssvep_model(
            "ssvep-custom-example",
            predict,
            version="1.0.0",
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Model stopped.")
