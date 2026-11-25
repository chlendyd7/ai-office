"""Tiny .env loader (no dependency). Never overrides variables already set in the real environment."""
from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: str | Path) -> list[str]:
    """Returns the names loaded (never the values)."""
    p = Path(path)
    if not p.exists():
        return []
    loaded = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v
            loaded.append(k)
    return loaded
