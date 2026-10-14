"""
Feature-based part model.

A Part is an ordered list of features, like the SolidWorks feature tree.
Everything the app can build is described here as plain data, so the same
list can be saved to JSON, validated, previewed (CadQuery) and built in
SolidWorks without any backend knowing about the others.

Conventions (identical in both backends, all lengths in mm):
  * Planes follow SolidWorks defaults:
        Front : sketch x -> X,  sketch y -> Y,  normal +Z
        Top   : sketch x -> X,  sketch y -> -Z, normal +Y
        Right : sketch x -> -Z, sketch y -> Y,  normal +X
  * A plane can be offset along its normal by `offset` mm.
  * Extrude directions: "one"       = along the plane normal
                        "reverse"   = against the plane normal
                        "symmetric" = half the depth each side
"""
import json
from dataclasses import asdict, dataclass, field
from typing import ClassVar, List, Optional, Tuple, Union

PLANES = ("Front", "Top", "Right")
OPERATIONS = ("boss", "cut")
END_CONDITIONS = ("blind", "through_all")
DIRECTIONS = ("one", "reverse", "symmetric")


# --------------------------------------------------------------------------
# Sketch entities
# --------------------------------------------------------------------------
class _Entity:
    kind: ClassVar[str] = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = self.kind
        return d


@dataclass
class Rect(_Entity):
    """Axis-aligned rectangle centered on (cx, cy)."""
    cx: float = 0.0
    cy: float = 0.0
    width: float = 20.0
    height: float = 10.0
    kind: ClassVar[str] = "rect"


@dataclass
class Circle(_Entity):
    cx: float = 0.0
    cy: float = 0.0
    diameter: float = 10.0
    kind: ClassVar[str] = "circle"


@dataclass
class Polygon(_Entity):
    """Closed polyline through `points` (last point connects back to first)."""
    points: List[Tuple[float, float]] = field(
        default_factory=lambda: [(0.0, 0.0), (20.0, 0.0), (20.0, 10.0), (0.0, 10.0)]
    )
    kind: ClassVar[str] = "polygon"


ENTITY_TYPES = {c.kind: c for c in (Rect, Circle, Polygon)}


def entity_from_dict(data: dict):
    data = dict(data)
    kind = data.pop("kind", data.pop("type", None))
    cls = ENTITY_TYPES[kind]
    if cls is Polygon:
        data["points"] = [(float(x), float(y)) for x, y in data["points"]]
    return cls(**data)


# --------------------------------------------------------------------------
# Features
# --------------------------------------------------------------------------
@dataclass
class Sketch:
    name: str = "Sketch1"
    plane: str = "Front"
    offset: float = 0.0
    entities: list = field(default_factory=list)
    type: ClassVar[str] = "sketch"

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "name": self.name,
            "plane": self.plane,
            "offset": self.offset,
            "entities": [e.to_dict() for e in self.entities],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Sketch":
        return cls(
            name=data["name"],
            plane=data.get("plane", "Front"),
            offset=float(data.get("offset", 0.0)),
            entities=[entity_from_dict(e) for e in data.get("entities", [])],
        )


@dataclass
class Extrude:
    """Extruded boss (adds material) or cut (removes material) from a sketch."""
    name: str = "Extrude1"
    sketch: str = ""
    operation: str = "boss"
    end: str = "blind"
    depth: float = 10.0
    direction: str = "one"
    type: ClassVar[str] = "extrude"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.type
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Extrude":
        data = {k: v for k, v in data.items() if k != "type"}
        if data.get("direction") == "one_direction":
            data["direction"] = "one"
        return cls(**data)


@dataclass
class Sketch3D:
    """3D wireframe path made from line segments; coordinates are in mm."""
    name: str = "Sketch3D1"
    segments: List[Tuple[Tuple[float, float, float], Tuple[float, float, float]]] = field(
        default_factory=list
    )
    type: ClassVar[str] = "sketch3d"

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "name": self.name,
            "segments": [[list(start), list(end)] for start, end in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Sketch3D":
        return cls(
            name=data["name"],
            segments=[
                (tuple(float(v) for v in segment[0]), tuple(float(v) for v in segment[1]))
                for segment in data.get("segments", [])
            ],
        )


@dataclass
class Revolve:
    """Revolved boss or cut; angles are in degrees and lengths in mm."""
    name: str
    sketch_name: str
    axis_name: str
    angle: float = 360.0
    operation: str = "boss"
    type: ClassVar[str] = "revolve"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["type"] = self.type
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Revolve":
        values = {k: v for k, v in data.items() if k != "type"}
        return cls(**values)


@dataclass
class EdgeModifier:
    """Fillet/chamfer using base64 SolidWorks persistent-reference tokens."""
    name: str
    feature_target: str
    modifier_type: str
    radius_mm: float
    target_edges: List[str] = field(default_factory=list)
    type: ClassVar[str] = "edge_modifier"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["type"] = self.type
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "EdgeModifier":
        values = {k: v for k, v in data.items() if k != "type"}
        return cls(**values)


@dataclass
class WeldmentMember:
    """Weldment profile applied to a 3D sketch path."""
    name: str
    sketch_3d_name: str
    profile_path: str
    corner_treatment: int
    type: ClassVar[str] = "weldment_member"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["type"] = self.type
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "WeldmentMember":
        values = {k: v for k, v in data.items() if k != "type"}
        return cls(**values)


FEATURE_TYPES = {
    "sketch": Sketch,
    "extrude": Extrude,
    "sketch3d": Sketch3D,
    "revolve": Revolve,
    "edge_modifier": EdgeModifier,
    "weldment_member": WeldmentMember,
}


def feature_from_dict(data: dict):
    return FEATURE_TYPES[data["type"]].from_dict(data)


# --------------------------------------------------------------------------
# Part
# --------------------------------------------------------------------------
@dataclass
class Part:
    name: str = "Part"
    features: List[
        Union[Sketch, Extrude, Sketch3D, Revolve, EdgeModifier, WeldmentMember]
    ] = field(default_factory=list)

    # -- construction -------------------------------------------------------
    @classmethod
    def default(cls) -> "Part":
        return cls(
            name="Part",
            features=[
                Sketch(name="Sketch1", plane="Front", entities=[Rect(width=60, height=40)]),
                Extrude(name="Extrude1", sketch="Sketch1", depth=20),
            ],
        )

    # -- lookups ------------------------------------------------------------
    def find(self, name: str) -> Optional[object]:
        for f in self.features:
            if f.name == name:
                return f
        return None

    def sketches_before(self, feature) -> List[Sketch]:
        out = []
        for f in self.features:
            if f is feature:
                break
            if isinstance(f, Sketch):
                out.append(f)
        return out

    def unique_name(self, prefix: str) -> str:
        used = {f.name for f in self.features}
        i = 1
        while f"{prefix}{i}" in used:
            i += 1
        return f"{prefix}{i}"

    def rename_feature(self, feature, new_name: str) -> bool:
        new_name = new_name.strip()
        if not new_name or any(f.name == new_name and f is not feature for f in self.features):
            return False
        old = feature.name
        feature.name = new_name
        if isinstance(feature, (Sketch, Sketch3D)):
            for f in self.features:
                if isinstance(f, Extrude) and f.sketch == old:
                    f.sketch = new_name
                elif isinstance(f, Revolve) and f.sketch_name == old:
                    f.sketch_name = new_name
                elif isinstance(f, WeldmentMember) and f.sketch_3d_name == old:
                    f.sketch_3d_name = new_name
        return True

    # -- persistence --------------------------------------------------------
    def to_dict(self) -> dict:
        return {"name": self.name, "units": "mm", "features": [f.to_dict() for f in self.features]}

    @classmethod
    def from_dict(cls, data: dict) -> "Part":
        raw_features = data.get("features", [])
        normalized_features = []
        used_names = {
            feature.get("name", "")
            for feature in raw_features
            if isinstance(feature, dict)
        }
        for feature_data in raw_features:
            if not isinstance(feature_data, dict):
                raise ValueError("Each part feature must be a JSON object.")
            feature_data = dict(feature_data)
            if (
                feature_data.get("type") == "extrude"
                and isinstance(feature_data.get("sketch"), dict)
            ):
                embedded_sketch = dict(feature_data.pop("sketch"))
                feature_name = str(feature_data.get("name", "Extrude"))
                sketch_name = f"{feature_name}_Sketch"
                suffix = 2
                while sketch_name in used_names:
                    sketch_name = f"{feature_name}_Sketch{suffix}"
                    suffix += 1
                used_names.add(sketch_name)
                embedded_sketch["name"] = sketch_name
                normalized_features.append(Sketch.from_dict(embedded_sketch))
                feature_data["sketch"] = sketch_name
            normalized_features.append(feature_from_dict(feature_data))
        return cls(
            name=data.get("name", "Part"),
            features=normalized_features,
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Part":
        return cls.from_dict(json.loads(text))

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.to_json())

    @classmethod
    def load(cls, path: str) -> "Part":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_json(fh.read())
