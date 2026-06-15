"""Classify how risky an update is, from the image tag's semver."""
from __future__ import annotations

import re

# major | minor | patch | unknown   (unknown = non-semver tag like "latest" -> treat as risky)
SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def _parse(tag: str) -> tuple[int, int, int] | None:
    m = SEMVER.match(tag or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def classify(current_tag: str, new_tag: str) -> str:
    """Return 'major' | 'minor' | 'patch' | 'unknown'."""
    a, b = _parse(current_tag), _parse(new_tag)
    if a is None or b is None:
        return "unknown"
    if b[0] != a[0]:
        return "major"
    if b[1] != a[1]:
        return "minor"
    return "patch"


def is_auto_safe(level: str) -> bool:
    """In 'auto-safe' mode, only patch/minor apply automatically; major/unknown need approval."""
    return level in ("patch", "minor")
