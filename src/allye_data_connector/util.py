from __future__ import annotations

import time
from pathlib import Path


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def resolve_secret_dir(secret_dir: str | Path | None) -> Path:
    if secret_dir is not None:
        return Path(secret_dir).expanduser()
    return Path("~/.allye_secrets").expanduser()
