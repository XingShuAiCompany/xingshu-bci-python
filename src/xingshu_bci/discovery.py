"""Auto-discovery of the local API bootstrap file."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

APP_FOLDER = "XingShu BCI"


def _appdata_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_FOLDER
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_FOLDER
    return Path.home() / ".config" / APP_FOLDER


def bootstrap_path() -> Path:
    return _appdata_dir() / "api" / "bootstrap.json"


def tokens_path() -> Path:
    return _appdata_dir() / "api" / "tokens.json"


def load_bootstrap() -> Optional[Dict[str, Any]]:
    path = bootstrap_path()
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
