"""Sanitized submission stub for floorplan parsing.

The production OpenCV-based parsing implementation is excluded in this branch
for IP protection. This module keeps the public interface only.
"""

from __future__ import annotations


def parse_image(image_bytes: bytes) -> dict:
    """Return a minimal parse result shape used by backend integration.

    Real detection logic is intentionally removed in the submission build.
    """
    return {
        "image_width": 0,
        "image_height": 0,
        "fixtures": [],
    }
