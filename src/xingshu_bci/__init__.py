"""Official Python SDK for the XingShu BCI Platform local API."""

from .client import XingShuClient, healthz
from .events import Event
from .exceptions import (
    XingShuError,
    XingShuAuthError,
    XingShuForbiddenError,
    XingShuNotFoundError,
    XingShuConnectionError,
    XingShuTimeoutError,
    XingShuRateLimitError,
)
from .focus import FocusUpdate, SmoothedFocusStream, smoothed_focus
from .models import (
    BatteryStatus,
    ExperimentModelRegistration,
    SsvepPrediction,
    SsvepTrialWindow,
    TokenInfo,
    TokenCreated,
    VersionInfo,
    XingShuCapabilities,
    XingShuTopology,
    ServerStatus,
)

__all__ = [
    "XingShuClient",
    "healthz",
    "Event",
    "XingShuError",
    "XingShuAuthError",
    "XingShuForbiddenError",
    "XingShuNotFoundError",
    "XingShuConnectionError",
    "XingShuTimeoutError",
    "XingShuRateLimitError",
    "FocusUpdate",
    "SmoothedFocusStream",
    "smoothed_focus",
    "BatteryStatus",
    "ExperimentModelRegistration",
    "SsvepPrediction",
    "SsvepTrialWindow",
    "TokenInfo",
    "TokenCreated",
    "VersionInfo",
    "XingShuCapabilities",
    "XingShuTopology",
    "ServerStatus",
]

__version__ = "1.0.0"

_DEVICE_EXPORTS = {"XingShuN1Pro", "ProChannel", "ProChunk", "ProStatus"}


def __getattr__(name: str):
    """Lazy access to the direct-device API (needs numpy) without importing it eagerly."""
    if name in _DEVICE_EXPORTS:
        from . import pro

        return getattr(pro, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
