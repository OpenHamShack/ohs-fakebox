"""Reading one file of /sys or /proc under a given root, as the camera
scan, the diagnostics probes and the USB lookups all do. The names
are the diagnostics section's, where they began.
"""

from __future__ import annotations

from pathlib import Path


def diag_read(root: Path, rel: str, limit: int = 65536) -> str:
    with open(root / rel.lstrip("/"), "r", errors="replace") as f:
        return f.read(limit).strip()


def diag_attr(dev: Path, name: str) -> str:
    try:
        return (dev / name).read_text(errors="replace").strip()
    except OSError:
        return ""
