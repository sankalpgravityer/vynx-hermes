"""A fully transparent cut-out must measure without dropping keys the caller
reads (KIL-001841 / CKIL-001682 failed verification with KeyError: 'uniformity')."""
from __future__ import annotations

import io
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import policy  # noqa: E402
from app.imaging import cutouts  # noqa: E402

CFG = cutouts.config(policy())
KEYS = ("width", "height", "transparent", "rgb", "coverage", "uniformity", "stddev",
        "edge_fill", "edges_touched", "box")


def test_fully_transparent_cutout_has_every_key():
    buf = io.BytesIO()
    Image.new("RGBA", (300, 400), (0, 0, 0, 0)).save(buf, "PNG")
    m = cutouts.measure(buf.getvalue(), CFG, (255, 255, 255))
    assert m is not None
    assert all(k in m for k in KEYS)
    assert m["uniformity"] == 0.0
