import math
import os

import pytest

pytest.importorskip("cadquery")

from partbuilder.backends.preview_cq import build_preview
from partbuilder.core.model import Part

EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "examples")


def test_enclosure_volume_matches_hand_calculation():
    part = Part.load(os.path.join(EXAMPLES, "enclosure.json"))
    res = build_preview(part)
    box = 100 * 80 * 50
    pocket = 95 * 75 * 47.5
    holes = 4 * math.pi * 2.5 ** 2 * 2.5  # through the 2.5 mm floor only
    assert res.volumes["Box"] == pytest.approx(box, rel=1e-6)
    assert res.volumes["Pocket"] == pytest.approx(box - pocket, rel=1e-6)
    assert res.volumes["HoleCut"] == pytest.approx(box - pocket - holes, rel=1e-4)
    assert not res.warnings
    assert len(res.triangles) > 0 and len(res.edges) > 0


def test_l_bracket_volume_and_orientation():
    part = Part.load(os.path.join(EXAMPLES, "l_bracket.json"))
    res = build_preview(part)
    l_area = 60 * 5 + (50 - 5) * 5          # base + wall
    expected = l_area * 40 - 4 * math.pi * 3 ** 2 * 5
    assert res.volumes["BaseHoleCut"] == pytest.approx(expected, rel=1e-4)
    bb = res.body.val().BoundingBox()
    # Right plane: sketch x -> -Z, sketch y -> Y; symmetric 40 along X
    assert (bb.xmin, bb.xmax) == pytest.approx((-20, 20), abs=0.05)
    assert (bb.ymin, bb.ymax) == pytest.approx((0, 50), abs=0.05)
    assert (bb.zmin, bb.zmax) == pytest.approx((-60, 0), abs=0.05)


def test_cut_that_misses_gives_warning():
    part = Part.load(os.path.join(EXAMPLES, "enclosure.json"))
    pocket = part.find("Pocket")
    pocket.direction = "one"   # cuts upward into empty space
    res = build_preview(part)
    assert any("Pocket" in w for w in res.warnings)
