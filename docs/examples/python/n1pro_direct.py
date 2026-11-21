"""Record XingShuN1 Pro data directly, without the desktop platform.

Usage:
    python -m pip install ".[device]"  # from the extracted SDK repository root
    python docs/examples/python/n1pro_direct.py --rate 1000 --seconds 30 --channels 1-8 --out eeg.npz

The device must be on, cabled to this PC (169.254.x.x adapter), and Python must
be allowed through the Windows firewall for UDP 9000. The desktop platform
should not be acquiring from the same device at the same time.
"""

from __future__ import annotations

import argparse
import time

import numpy as np

from xingshu_bci.pro import CHANNEL_NAMES, XingShuN1Pro


def parse_channels(text: str) -> list[int]:
    channels: list[int] = []
    for part in text.split(","):
        if "-" in part:
            start, end = part.split("-")
            channels.extend(range(int(start), int(end) + 1))
        elif part:
            channels.append(int(part))
    return channels


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rate", type=int, default=1000, choices=XingShuN1Pro.SAMPLE_RATES)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--channels", default="1-32", help="electrode channels to enable, e.g. 1-8 or 1,2,9")
    parser.add_argument("--gain", type=int, default=24, choices=XingShuN1Pro.GAINS)
    parser.add_argument("--out", default="xingshu_n1pro.npz")
    args = parser.parse_args()

    active = parse_channels(args.channels)
    with XingShuN1Pro(sample_rate=args.rate) as dev:
        print(f"Connected to {dev.device_address}, {dev.sample_rate} Hz, chips {dev.online_chips}")
        # Only electrode channels on, all of them feeding the right-leg drive.
        dev.configure_channels(enabled=False, bias=False)
        dev.configure_channels(active, enabled=True, gain=args.gain, input_type="normal", bias=True)
        print(f"BIAS amplifier: {'on' if dev.bias_amplifier_on else 'off'}; battery: {dev.battery}")

        blocks = []
        end = time.monotonic() + args.seconds
        while time.monotonic() < end:
            block = dev.read(seconds=1)
            blocks.append(block)
            rms = block[[c - 1 for c in active]].std(axis=1)
            print(f"{len(blocks):4d}s  median {np.median(rms):7.2f} µVrms  "
                  f"lost {dev.stats['missingSamples']}  overflow {dev.stats['overflowSamples']}")

        data = np.concatenate(blocks, axis=1)
        np.savez(args.out, data=data, sample_rate=dev.sample_rate, channel_names=np.array(CHANNEL_NAMES),
                 active=np.array(active))
        print(f"Saved {data.shape[1]} samples x {data.shape[0]} channels to {args.out}")


if __name__ == "__main__":
    main()
