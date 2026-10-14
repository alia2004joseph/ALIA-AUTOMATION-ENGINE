"""
CadQuery backend: builds the real solid from a Part so the preview shows the
actual geometry, not a drawing. Also used to compute the volume after each
feature, which the SolidWorks backend compares against to verify its build.
"""
from dataclasses import dataclass, field
from typing import Dict, List

try:
    import cadquery as cq
    import numpy as np
    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False

from ..core.model import Circle, Extrude, Part, Polygon, Rect, Sketch

BIG = 100000.0  # mm, "through all"
EDGE_SAMPLES = 24

# (origin, x direction, normal) - same as SolidWorks default planes
_PLANE_DEFS = {
    "Front": ((0, 0, 0), (1, 0, 0), (0, 0, 1)),
    "Top": ((0, 0, 0), (1, 0, 0), (0, 1, 0)),
    "Right": ((0, 0, 0), (0, 0, -1), (1, 0, 0)),
}


class BuildError(Exception):
    pass


@dataclass
class PreviewResult:
    body: object = None                       # cq.Workplane
    volumes: Dict[str, float] = field(default_factory=dict)   # feature name -> mm^3 after it
    warnings: List[str] = field(default_factory=list)
    vertices: object = None                   # (N, 3) float array
    triangles: object = None                  # (M, 3) int array
    edges: list = field(default_factory=list) # list of (K, 3) float arrays


def make_plane(name: str, offset: float):
    origin, xdir, normal = _PLANE_DEFS[name]
    o = tuple(c + n * offset for c, n in zip(origin, normal))
    return cq.Plane(origin=o, xDir=xdir, normal=normal)


def _profile(sketch: Sketch):
    wp = cq.Workplane(make_plane(sketch.plane, sketch.offset))
    for e in sketch.entities:
        if isinstance(e, Rect):
            wp = wp.pushPoints([(e.cx, e.cy)]).rect(e.width, e.height)
        elif isinstance(e, Circle):
            wp = wp.pushPoints([(e.cx, e.cy)]).circle(e.diameter / 2.0)
        elif isinstance(e, Polygon):
            pts = e.points
            wp = wp.moveTo(*pts[0])
            for p in pts[1:]:
                wp = wp.lineTo(*p)
            wp = wp.close()
    return wp


def _tool(sketch: Sketch, f: Extrude):
    wp = _profile(sketch)
    d = BIG if f.end == "through_all" else f.depth
    if f.direction == "symmetric":
        return wp.extrude(d / 2.0, both=True, combine=False)
    if f.direction == "reverse":
        return wp.extrude(-d, combine=False)
    return wp.extrude(d, combine=False)


def _volume(wp) -> float:
    return float(wp.val().Volume())


def build_preview(part: Part, tessellate: bool = True) -> PreviewResult:
    if not AVAILABLE:
        raise BuildError("CadQuery is not installed (pip install cadquery).")
    unsupported = next(
        (feature for feature in part.features if not isinstance(feature, (Sketch, Extrude))),
        None,
    )
    if unsupported is not None:
        raise BuildError(
            f"CadQuery preview does not yet model {type(unsupported).__name__} "
            f"'{unsupported.name}'. Build this feature in SolidWorks instead."
        )
    result = PreviewResult()
    sketches: Dict[str, Sketch] = {}
    body = None
    try:
        for f in part.features:
            if isinstance(f, Sketch):
                sketches[f.name] = f
            elif isinstance(f, Extrude):
                tool = _tool(sketches[f.sketch], f)
                before = _volume(body) if body is not None else 0.0
                if f.operation == "boss":
                    body = tool if body is None else body.union(tool)
                else:
                    body = body.cut(tool)
                after = _volume(body)
                result.volumes[f.name] = after
                if after < 1e-6:
                    result.warnings.append(f"{f.name} removes the entire part.")
                elif abs(after - before) < 1e-6:
                    result.warnings.append(f"{f.name} did not change the part (check plane, depth and direction).")
    except BuildError:
        raise
    except Exception as exc:
        raise BuildError(f"{type(exc).__name__}: {exc}") from exc

    result.body = body
    if body is not None and tessellate:
        _tessellate(result)
    return result


def _tessellate(result: PreviewResult, tol: float = 0.2) -> None:
    shape = result.body.val()
    verts, tris = shape.tessellate(tol, 0.3)
    if not verts:          # empty solid (everything was cut away)
        return
    result.vertices = np.array([(v.x, v.y, v.z) for v in verts], dtype=float)
    result.triangles = np.array(tris, dtype=int)
    for edge in shape.Edges():
        n = EDGE_SAMPLES  # same count for every edge: matplotlib needs a uniform array
        pts = [edge.positionAt(i / (n - 1)).toTuple() for i in range(n)]
        result.edges.append(np.array(pts, dtype=float))


def export(part: Part, path: str) -> None:
    """Export the preview solid; format chosen by extension (.stl / .step)."""
    res = build_preview(part, tessellate=False)
    if res.body is None:
        raise BuildError("Nothing to export yet.")
    cq.exporters.export(res.body, path)
