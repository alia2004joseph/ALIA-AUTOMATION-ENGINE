"""
SolidWorks backend: turns a Part (feature list) into native SolidWorks features.

Design points
  * Every COM call goes through `call()`, so a failure names the exact step
    ("FeatureCut4 failed: ...") instead of a bare COM error code.
  * After each extrude/cut the part volume is read back from SolidWorks and
    compared with the CadQuery preview volume. If a single-direction feature
    went the wrong way, it is deleted and rebuilt flipped once; the correction
    is remembered for the rest of the build.
  * All API lengths are meters (mm * 0.001).

Must run on a thread that called pythoncom.CoInitialize() - use `com_thread()`.
"""
import contextlib
import base64
import binascii
import copy
import json
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..core.model import (
    Circle, EdgeModifier, Extrude, Part, Polygon, Rect, Revolve, Sketch, Sketch3D,
    WeldmentMember,
)

try:
    import pythoncom
    import pywintypes
    import win32com.client
    AVAILABLE = True
except ImportError:
    AVAILABLE = False

M = 0.001                       # mm -> m
RAD = 0.0174532925199433        # 1 degree, filler for draft-angle arguments
SW_DEFAULT_TEMPLATE_PART = 8
SW_REFPLANE_DISTANCE = 8
END_BLIND, END_THROUGH_ALL, END_MIDPLANE = 0, 1, 6
CHAMFER_ANGLE_DISTANCE = 1
CHAMFER_TANGENT_PROPAGATION = 4
FILLET_UNIFORM_RADIUS = 2
FILLET_SIMPLE = 0
FILLET_OVERFLOW_DEFAULT = 0
WELDMENT_CONNECTED_SIMPLE_CUT = 1


def _ordered_build_features(features):
    """Order reference sketches, additive solids, cuts, then edge modifiers."""
    sketches = [f for f in features if isinstance(f, (Sketch, Sketch3D))]
    boss_extrudes = [
        f for f in features if isinstance(f, Extrude) and f.operation == "boss"
    ]
    other_additions = [
        f for f in features
        if isinstance(f, WeldmentMember)
        or isinstance(f, Revolve) and f.operation == "boss"
    ]
    cuts = [
        f for f in features
        if isinstance(f, Extrude) and f.operation == "cut"
        or isinstance(f, Revolve) and f.operation == "cut"
    ]
    modifiers = [f for f in features if isinstance(f, EdgeModifier)]
    supported = set(map(id, sketches + boss_extrudes + other_additions + cuts + modifiers))
    unsupported = [f for f in features if id(f) not in supported]
    return sketches + boss_extrudes + other_additions + cuts + modifiers + unsupported


class SWError(Exception):
    pass


@contextlib.contextmanager
def com_thread():
    """Initialise COM for the current (worker) thread."""
    pythoncom.CoInitialize()
    try:
        yield
    finally:
        pythoncom.CoUninitialize()


def _fmt_com(exc) -> str:
    try:
        hr, msg, info, arg = exc.args[:4]
        detail = info[2] if info and len(info) > 2 and info[2] else msg
        return f"{detail} (hr={hr}, bad arg index={arg})"
    except Exception:
        return str(exc)


def _close(actual: float, expected: float) -> bool:
    return abs(actual - expected) <= max(1.0, 0.005 * abs(expected))


@dataclass
class StepReport:
    name: str
    expected_mm3: Optional[float] = None
    actual_mm3: Optional[float] = None
    status: str = "unchecked"     # ok | flipped | mismatch | unchecked
    note: str = ""


@dataclass
class BuildReport:
    steps: List[StepReport] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.status != "mismatch" for s in self.steps)

    def summary(self) -> str:
        lines = []
        for s in self.steps:
            exp = f"{s.expected_mm3:,.0f}" if s.expected_mm3 is not None else "-"
            act = f"{s.actual_mm3:,.0f}" if s.actual_mm3 is not None else "-"
            lines.append(f"  {s.status.upper():9} {s.name}: SolidWorks {act} mm3 vs preview {exp} mm3 {s.note}")
        head = "Build finished, volumes match the preview." if self.ok else \
            "Build finished, but some features differ from the preview:"
        return "\n".join([head] + lines)


class SolidWorksBuilder:
    def __init__(self, log: Optional[Callable[[str], None]] = None):
        self.log = log or (lambda m: None)
        self.app = None
        self.model = None
        self._flip = {"boss": False, "cut": False}
        self._sketch3d_segments = {}

    # ------------------------------------------------------------ plumbing
    def call(self, label, fn, *args):
        try:
            return fn(*args)
        except pywintypes.com_error as exc:
            raise SWError(f"{label} failed: {_fmt_com(exc)}") from exc

    def connect(self):
        if not AVAILABLE:
            raise SWError("pywin32 is not installed (Windows only): pip install pywin32")
        try:
            self.app = win32com.client.gencache.EnsureDispatch("SldWorks.Application")
            self.log("Connected to SolidWorks (early-bound).")
        except Exception:
            self.app = win32com.client.Dispatch("SldWorks.Application")
            self.log("Connected to SolidWorks (late-bound).")
        self.app.Visible = True
        return self.app

    @staticmethod
    def _callout():
        return win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)

    def _new_part(self):
        template = self.call("GetUserPreferenceStringValue",
                             self.app.GetUserPreferenceStringValue, SW_DEFAULT_TEMPLATE_PART)
        if not template:
            raise SWError("No default part template set (SolidWorks: Tools > Options > Default Templates).")
        self.model = self.call("NewDocument", self.app.NewDocument, template, 0, 0, 0)
        if self.model is None:
            self.model = self.app.ActiveDoc
        if self.model is None:
            raise SWError("SolidWorks did not create a part document.")

    def _first_feature(self):
        """Get the first tree feature with late-bound method/property dispatch."""
        try:
            dispatch_id = self.model._oleobj_.GetIDsOfNames(0, "FirstFeature")
            feature = self.model._oleobj_.InvokeTypes(
                dispatch_id,
                0,
                pythoncom.DISPATCH_METHOD,
                (pythoncom.VT_DISPATCH, 0),
                (),
            )
        except (pywintypes.com_error, TypeError, AttributeError) as exc:
            raise SWError(f"IModelDoc2.FirstFeature failed: {_fmt_com(exc)}") from exc
        return win32com.client.Dispatch(feature) if feature is not None else None

    @staticmethod
    def _feature_type_name(feature) -> str:
        """Read GetTypeName2 whether COM exposes a method or a string property."""
        value = feature.GetTypeName2
        return value() if callable(value) else value

    def _last_feature(self, type_name: str):
        """Find the last matching feature using explicit COM dispatch."""
        feat = self._first_feature()
        last = None

        while feat is not None:
            if self._feature_type_name(feat) == type_name:
                last = feat

            try:
                dispatch_id = feat._oleobj_.GetIDsOfNames(
                    0, "GetNextFeature"
                )
                next_feature = feat._oleobj_.InvokeTypes(
                    dispatch_id,
                    0,
                    pythoncom.DISPATCH_METHOD,
                    (pythoncom.VT_DISPATCH, 0),
                    (),
                )
            except (pywintypes.com_error, TypeError, AttributeError) as exc:
                raise SWError(
                    f"Feature-tree traversal failed while searching "
                    f"for '{type_name}': {_fmt_com(exc)}"
                ) from exc

            feat = (
                win32com.client.Dispatch(next_feature)
                if next_feature is not None
                else None
            )

        return last

    def _volume_mm3(self) -> Optional[float]:
        try:
            mp = self.model.Extension.CreateMassProperty()
            return None if mp is None else float(mp.Volume) * 1e9
        except Exception:
            return None

    # ------------------------------------------------------------ planes
    def _select_plane(self, name: str):
        m = self.model
        m.ClearSelection2(True)
        ok = self.call("SelectByID2(plane)", m.Extension.SelectByID2,
                       f"{name} Plane", "PLANE", 0, 0, 0, False, 0, self._callout(), 0)
        if ok:
            return
        # non-English templates: the first three reference planes are Front, Top, Right
        order = ["Front", "Top", "Right"].index(name)
        feat, idx = self._first_feature(), 0
        while feat is not None:
            if self._feature_type_name(feat) == "RefPlane":
                if idx == order:
                    if feat.Select2(False, 0):
                        return
                    break
                idx += 1
            feat = feat.GetNextFeature()
        raise SWError(f"Could not select the {name} plane.")

    # ------------------------------------------------------------ sketch
    def _build_sketch(self, sk: Sketch):
        m = self.model
        self._select_plane(sk.plane)
        if abs(sk.offset) > 1e-9:
            plane = self.call("InsertRefPlane", m.FeatureManager.InsertRefPlane,
                              SW_REFPLANE_DISTANCE, sk.offset * M, 0, 0, 0, 0)
            if plane is None:
                raise SWError(f"{sk.name}: could not create the offset plane.")
            m.ClearSelection2(True)
            plane.Select2(False, 0)
        self.call("InsertSketch", m.SketchManager.InsertSketch, True)
        sm = m.SketchManager
        for e in sk.entities:
            if isinstance(e, Rect):
                self.call("CreateCenterRectangle", sm.CreateCenterRectangle,
                          e.cx * M, e.cy * M, 0, (e.cx + e.width / 2) * M, (e.cy + e.height / 2) * M, 0)
            elif isinstance(e, Circle):
                self.call("CreateCircleByRadius", sm.CreateCircleByRadius,
                          e.cx * M, e.cy * M, 0, e.diameter / 2 * M)
            elif isinstance(e, Polygon):
                n = len(e.points)
                for i in range(n):
                    (x1, y1), (x2, y2) = e.points[i], e.points[(i + 1) % n]
                    self.call("CreateLine", sm.CreateLine, x1 * M, y1 * M, 0, x2 * M, y2 * M, 0)
        self.call("InsertSketch(exit)", m.SketchManager.InsertSketch, True)
        feat = self._last_feature("ProfileFeature")
        if feat is None:
            raise SWError(f"{sk.name}: sketch feature not found after creation.")
        feat.Name = sk.name
        return feat

    def _build_sketch3d(self, sk: Sketch3D):
        m = self.model
        self.model.ClearSelection2(True)
        self.call("Insert3DSketch(start)", m.SketchManager.Insert3DSketch, True)
        segments = []
        for index, (start, end) in enumerate(sk.segments, 1):
            segment = self.call(
                f"{sk.name}: CreateLine #{index}",
                m.SketchManager.CreateLine,
                *(coordinate * M for point in (start, end) for coordinate in point),
            )
            if segment is None:
                raise SWError(f"{sk.name}: SolidWorks did not create 3D line #{index}.")
            segments.append(segment)
        self.call("Insert3DSketch(exit)", m.SketchManager.Insert3DSketch, True)
        feat = self._last_feature("ProfileFeature")
        if feat is None:
            raise SWError(f"{sk.name}: 3D sketch feature not found after creation.")
        feat.Name = sk.name
        self._sketch3d_segments[sk.name] = segments
        return feat

    def _create_revolve(self, r: Revolve, sketch_feat):
        m = self.model
        m.ClearSelection2(True)
        if not sketch_feat.Select2(False, 0):
            raise SWError(f"{r.name}: could not select profile sketch '{r.sketch_name}'.")
        axis_selected = self.call(
            f"{r.name}: select revolve axis",
            m.Extension.SelectByID2,
            r.axis_name, "AXIS", 0, 0, 0, True, 4, self._callout(), 0,
        )
        if not axis_selected:
            axis = self.call("FeatureByName(axis)", m.FeatureByName, r.axis_name)
            if axis is None or not axis.Select2(True, 4):
                raise SWError(
                    f"{r.name}: could not select axis '{r.axis_name}'. "
                    "Use an existing SolidWorks axis/reference-axis name."
                )
        feature_mgr = m.FeatureManager
        feat = self.call(
            "FeatureRevolve2",
            feature_mgr.FeatureRevolve2,
            True, True, False, r.operation == "cut",
            False, False, 0, 0, r.angle * RAD, 0.0,
            False, False, 0.0, 0.0, 0, 0.0, 0.0,
            True, False, True,
        )
        if feat is None:
            raise SWError(f"{r.name}: SolidWorks rejected the revolve operation.")
        feat.Name = r.name
        return feat

    def _persistent_edge(self, token: str):
        try:
            reference = base64.b64decode(token, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise SWError("Edge modifier contains an invalid base64 persistent reference.") from exc
        result = self.call(
            "GetObjectByPersistReference3",
            self.model.Extension.GetObjectByPersistReference3,
            reference,
        )
        if isinstance(result, tuple):
            edge = result[0] if result else None
            error_code = result[1] if len(result) > 1 else 0
            if error_code:
                raise SWError(
                    f"SolidWorks could not resolve a persistent edge reference (code {error_code})."
                )
        else:
            edge = result
        if edge is None:
            raise SWError("SolidWorks could not resolve a persistent edge reference.")
        return edge

    def encode_persistent_edge_reference(self, edge) -> str:
        """Serialize a SolidWorks edge into the base64 token stored by EdgeModifier."""
        raw_reference = self.call(
            "GetPersistReference3",
            self.model.Extension.GetPersistReference3,
            edge,
        )
        if raw_reference is None:
            raise SWError("SolidWorks did not return a persistent reference for the edge.")
        try:
            return base64.b64encode(bytes(raw_reference)).decode("ascii")
        except (TypeError, ValueError) as exc:
            raise SWError("SolidWorks returned an invalid persistent edge reference.") from exc

    def _apply_edge_modifier(self, mod: EdgeModifier):
        m = self.model
        target = self.call("FeatureByName(target)", m.FeatureByName, mod.feature_target)
        if target is None:
            raise SWError(f"{mod.name}: target feature '{mod.feature_target}' was not found.")
        if mod.modifier_type == "fillet":
            return self._apply_fillet(mod.name, mod.radius_mm, mod.target_edges)

        edges = [self._persistent_edge(token) for token in mod.target_edges]
        m.ClearSelection2(True)
        selection_data = m.SelectionManager.CreateSelectData()
        for index, edge in enumerate(edges):
            selected = self.call(
                "Select persistent edge",
                edge.Select4,
                index > 0,
                selection_data,
            )
            if not selected:
                raise SWError(f"{mod.name}: could not select persistent edge #{index + 1}.")

        feature_mgr = m.FeatureManager
        if mod.modifier_type == "chamfer":
            feat = self.call(
                "InsertFeatureChamfer",
                feature_mgr.InsertFeatureChamfer,
                CHAMFER_TANGENT_PROPAGATION,
                CHAMFER_ANGLE_DISTANCE,
                mod.radius_mm * M,
                45 * RAD,
                0.0, 0.0, 0.0, 0.0,
            )
        else:
            raise SWError(f"{mod.name}: unsupported modifier type '{mod.modifier_type}'.")
        if feat is None:
            raise SWError(f"{mod.name}: SolidWorks rejected the edge modifier.")
        feat.Name = mod.name
        return feat

    def _apply_fillet(self, name: str, radius_mm: float, target_edges):
        """Apply a uniform-radius fillet to explicitly referenced persistent edges."""
        if not target_edges:
            raise SWError(f"{name}: at least one persistent edge reference is required.")
        m = self.model
        edges = [self._persistent_edge(token) for token in target_edges]
        m.ClearSelection2(True)
        selection_data = m.SelectionManager.CreateSelectData()
        for index, edge in enumerate(edges):
            selected = self.call(
                "Select persistent edge",
                edge.Select4,
                index > 0,
                selection_data,
            )
            if not selected:
                raise SWError(f"{name}: could not select persistent edge #{index + 1}.")

        feature = self.call(
            "FeatureFillet3",
            m.FeatureManager.FeatureFillet3,
            FILLET_UNIFORM_RADIUS, radius_mm * M, 0.0, 0.5,
            FILLET_SIMPLE, FILLET_OVERFLOW_DEFAULT, 0,
            None, None, None, None, None, None, None,
        )
        if feature is None:
            raise SWError(f"{name}: SolidWorks rejected the fillet.")
        feature.Name = name
        return feature

    def _create_weldment(self, w: WeldmentMember):
        segments = self._sketch3d_segments.get(w.sketch_3d_name)
        if not segments:
            raise SWError(f"{w.name}: 3D sketch '{w.sketch_3d_name}' has no built line segments.")
        feature_mgr = self.model.FeatureManager
        group = self.call(
            "CreateStructuralMemberGroup",
            feature_mgr.CreateStructuralMemberGroup,
        )
        if group is None:
            raise SWError(f"{w.name}: could not create a structural-member group.")
        group.Segments = tuple(segments)
        group.ApplyCornerTreatment = True
        group.CornerTreatmentType = w.corner_treatment
        feat = self.call(
            "InsertStructuralWeldment5",
            feature_mgr.InsertStructuralWeldment5,
            w.profile_path,
            WELDMENT_CONNECTED_SIMPLE_CUT,
            False,
            (group,),
            "",
        )
        if feat is None:
            raise SWError(f"{w.name}: SolidWorks rejected the structural weldment.")
        feat.Name = w.name
        return feat

    # ------------------------------------------------------------ extrude / cut
    def _create_extrude(self, f: Extrude, sketch_feat, flip: bool):
        m = self.model
        depth = f.depth * M
        through = f.end == "through_all"
        
        # Determine strict Boolean and Integer maps for SolidWorks parameters
        if f.direction == "symmetric":
            sd = True
            if through:
                t1, t2 = END_THROUGH_ALL, END_THROUGH_ALL
                depth2 = depth
                if f.operation == "cut":
                    # Through-all on both ends is a double-ended cut in
                    # FeatureCut4; Sd=True would contradict T1/T2.
                    sd = False
            else:
                t1, t2 = END_MIDPLANE, END_BLIND
                depth2 = 0.0
            dir_flag = False
            cut_direction = False
        else:
            # For "one_direction" or "reverse", map standard conditions cleanly
            sd = True
            t1 = END_THROUGH_ALL if through else END_BLIND
            t2 = END_BLIND
            depth2 = 0.0
            
            # Incorporate your volume correction flip tracking logic safely
            base_flip = (f.direction == "reverse")
            dir_flag = not base_flip if flip else base_flip
            # FeatureCut4 defaults to cutting opposite the sketch normal, unlike
            # FeatureExtrusion2, which defaults to adding material along it.
            cut_direction = (not base_flip) != flip

        # Retrieve the explicit feature manager context object
        feature_mgr = m.FeatureManager
        
        # Fire off the correct feature operation based on structural intent
        if f.operation == "boss":
            m.ClearSelection2(True)
            selected = sketch_feat.Select2(False, 0)
            if not selected:
                raise SWError(f"{f.name}: could not select its sketch.")
            feat = self.call(
                "FeatureExtrude2",
                feature_mgr.FeatureExtrusion2,
                sd,        # Sd: Single direction tracking
                dir_flag,  # Flip: Flip direction configuration flag
                False,     # Dir2: Separate direction 2 control
                t1,        # T1: Type configuration selection for direction 1
                t2,        # T2: Type configuration selection for direction 2
                depth,     # D1: Volumetric thickness depth 1
                0,         # D2: Volumetric thickness depth 2
                False, False, False, False, # Draft control toggles 
                RAD, RAD, False, False, False, False, # Visual angles
                True,      # Merge solid result body
                True, True, 
                0, 0, False
            )
        else:
            # Reselect immediately before FeatureCut4 so the active profile is
            # unambiguous in late-bound COM automation.
            m.ClearSelection2(True)
            selected = sketch_feat.Select2(False, 0)
            self.log(
                f"{f.name}: cut end={f.end} (T1={t1}, T2={t2}), "
                f"depth={depth:g} m, direction="
                f"{'along' if cut_direction else 'opposite'} sketch normal "
                f"(Dir={cut_direction}), mode="
                f"{'double-ended' if not sd else 'single-ended'}, "
                f"sketch selected={bool(selected)}."
            )
            if not selected:
                raise SWError(
                    f"{f.name}: could not select cut sketch; "
                    f"end={f.end}, depth={depth:g} m, Dir={cut_direction}."
                )

            feat = self.call(
                "FeatureCut4",
                feature_mgr.FeatureCut4,
                sd, False, cut_direction, t1, t2,
                depth, depth2,
                False, False, False, False,
                0.0, 0.0,
                False, False, False, False, False,
                False, True, False, False, False,
                0, 0.0, False, False,
            )
            self.log(f"{f.name}: FeatureCut4 returned feature={feat is not None}.")
            
        if feat is None:
            if f.operation == "cut":
                raise SWError(
                    f"{f.name}: FeatureCut4 returned no feature "
                    f"(end={f.end}, direction={f.direction}, Sd={sd}, "
                    f"T1={t1}, T2={t2}, D1={depth:g} m, D2={depth2:g} m, "
                    f"Dir={cut_direction}, sketch selected={bool(selected)}). "
                    "Check the SolidWorks cut status and confirm the selected "
                    "profile intersects the existing solid."
                )
            raise SWError(f"{f.name}: creation macro call rejected by SolidWorks engine.")
            
        feat.Name = f.name
        return feat

    def _delete_feature(self, feat):
        self.model.ClearSelection2(True)
        feat.Select2(False, 0)
        self.model.EditDelete()

    def _build_extrude(self, f: Extrude, sketch_feat, expected: Optional[float]) -> StepReport:
        step = StepReport(f.name, expected_mm3=expected)
        flip = self._flip[f.operation]
        attempts = 1 if f.direction == "symmetric" else 2
        for attempt in range(attempts):
            feat = self._create_extrude(f, sketch_feat, flip)
            actual = self._volume_mm3()
            step.actual_mm3 = actual
            if expected is None or actual is None:
                step.status = "unchecked"
                return step
            if _close(actual, expected):
                self._flip[f.operation] = flip
                step.status = "flipped" if attempt == 1 else "ok"
                if attempt == 1:
                    step.note = "(direction auto-corrected)"
                    self.log(f"{f.name}: direction corrected automatically.")
                return step
            if attempt < attempts - 1:
                self.log(f"{f.name}: volume differs from preview, retrying flipped.")
                self._delete_feature(feat)
                flip = not flip
        step.status = "mismatch"
        return step

    # ------------------------------------------------------------ public API
    def build_part(
        self,
        part_data,
        expected: Optional[Dict[str, float]] = None,
    ) -> BuildReport:
        """Build a Part model or JSON-compatible recipe using the feature pipeline."""
        part = part_data if isinstance(part_data, Part) else Part.from_dict(part_data)
        return self.build(part, expected)

    def build_dxf(
        self,
        dxf_path,
        recipe,
        expected: Optional[Dict[str, float]] = None,
    ) -> BuildReport:
        """Compile selected DXF profiles using a sidecar Part recipe.

        Recipe sketches may specify ``dxf_geometry_id`` to bind a parser
        profile to that sketch. Extrude depth/end/direction and edge modifier
        settings remain explicit in the recipe; DXF geometry is not used to
        guess manufacturing intent.
        """
        from ..core.validate import has_errors, parse_universal_dxf, validate

        payload = parse_universal_dxf(os.fspath(dxf_path))
        if payload.get("status") != "ok":
            raise SWError(
                f"DXF ingestion failed: {payload.get('note', 'unknown parser error')}"
            )

        if isinstance(recipe, (str, os.PathLike)):
            try:
                with open(recipe, encoding="utf-8") as recipe_file:
                    recipe_data = json.load(recipe_file)
            except (OSError, json.JSONDecodeError) as exc:
                raise SWError(f"Could not read DXF build recipe: {exc}") from exc
        elif isinstance(recipe, dict):
            recipe_data = copy.deepcopy(recipe)
        else:
            raise SWError("DXF build recipe must be a JSON object or a JSON file path.")
        if not isinstance(recipe_data, dict) or not isinstance(recipe_data.get("features"), list):
            raise SWError("DXF build recipe must contain a 'features' array.")

        profiles = {
            profile["id"]: (role, profile)
            for role in ("boss", "cut")
            for profile in payload[f"{role}_geometry"]
        }
        sketch_roles = {}
        modifier_ids = {modifier["id"] for modifier in payload["modifiers"]}
        for feature in recipe_data["features"]:
            if not isinstance(feature, dict):
                raise SWError("Each DXF recipe feature must be a JSON object.")
            if feature.get("type") == "sketch" and "dxf_geometry_id" in feature:
                geometry_id = feature.pop("dxf_geometry_id")
                match = profiles.get(geometry_id)
                if match is None:
                    raise SWError(
                        f"Recipe sketch '{feature.get('name', '')}' references "
                        f"unknown DXF profile '{geometry_id}'."
                    )
                role, profile = match
                feature["entities"] = copy.deepcopy(profile["entities"])
                feature.setdefault("offset", profile["elevation_mm"])
                sketch_roles[feature.get("name", "")] = role
            elif feature.get("type") == "edge_modifier" and "dxf_modifier_id" in feature:
                modifier_id = feature.pop("dxf_modifier_id")
                if modifier_id not in modifier_ids:
                    raise SWError(f"Recipe references unknown DXF modifier '{modifier_id}'.")

        mapped_boss = False
        for feature in recipe_data["features"]:
            if feature.get("type") != "extrude":
                continue
            sketch_name = feature.get("sketch")
            profile_role = sketch_roles.get(sketch_name)
            if profile_role is None:
                continue
            operation = feature.get("operation", "boss")
            if operation != profile_role:
                raise SWError(
                    f"Recipe extrude '{feature.get('name', '')}' is a '{operation}' "
                    f"but its DXF profile is classified as '{profile_role}'."
                )
            mapped_boss = mapped_boss or operation == "boss"
        if not mapped_boss:
            raise SWError(
                "DXF recipe must bind at least one boss extrude to a boss profile "
                "using a sketch's 'dxf_geometry_id'."
            )

        recipe_data.setdefault("name", payload["part_name"])
        try:
            part = Part.from_dict(recipe_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise SWError(f"Invalid DXF build recipe: {exc}") from exc
        issues = validate(part)
        if has_errors(issues):
            details = "; ".join(str(issue) for issue in issues if issue.level == "error")
            raise SWError(f"DXF build recipe failed validation: {details}")
        return self.build_part(part, expected)

    def build(self, part: Part, expected: Optional[Dict[str, float]] = None) -> BuildReport:
        if not isinstance(part, Part):
            part = Part.from_dict(part)
        expected = expected or {}
        self._flip = {"boss": False, "cut": False}
        self._sketch3d_segments = {}
        if self.app is None:
            self.connect()
        self.log("Creating new part...")
        self._new_part()
        report = BuildReport()
        ordered_features = _ordered_build_features(part.features)
        original_solid_order = [
            f.name for f in part.features
            if isinstance(f, (Extrude, Revolve, EdgeModifier, WeldmentMember))
        ]
        build_solid_order = [
            f.name for f in ordered_features
            if isinstance(f, (Extrude, Revolve, EdgeModifier, WeldmentMember))
        ]
        expected_volumes = expected
        if original_solid_order != build_solid_order:
            self.log(
                "Build phases reordered solid features; preview volumes follow the "
                "original feature tree, so volume comparisons are disabled for this build."
            )
            expected_volumes = {}

        sketch_feats = {}
        for f in ordered_features:
            if isinstance(f, Sketch):
                self.log(f"Sketch {f.name} on {f.plane} plane...")
                sketch_feats[f.name] = self._build_sketch(f)
            elif isinstance(f, Sketch3D):
                self.log(f"3D sketch {f.name}...")
                self._build_sketch3d(f)
            elif isinstance(f, Extrude):
                self.log(f"{f.operation.title()} {f.name}...")
                report.steps.append(
                    self._build_extrude(
                        f, sketch_feats[f.sketch], expected_volumes.get(f.name)
                    )
                )
            elif isinstance(f, Revolve):
                self.log(f"{f.operation.title()} revolve {f.name}...")
                self._create_revolve(f, sketch_feats[f.sketch_name])
                report.steps.append(
                    self._report_non_extrude(f.name, expected_volumes.get(f.name))
                )
            elif isinstance(f, EdgeModifier):
                self.log(f"{f.modifier_type.title()} {f.name}...")
                self._apply_edge_modifier(f)
                report.steps.append(
                    self._report_non_extrude(f.name, expected_volumes.get(f.name))
                )
            elif isinstance(f, WeldmentMember):
                self.log(f"Weldment {f.name}...")
                self._create_weldment(f)
                report.steps.append(
                    self._report_non_extrude(f.name, expected_volumes.get(f.name))
                )
            else:
                raise SWError(f"Unsupported feature type: {type(f).__name__}.")
        self.model.ForceRebuild3(True)
        self.model.ViewZoomtofit2()
        try:
            self.model.ShowNamedView2("*Isometric", 7)
        except Exception:
            pass
        return report

    def _report_non_extrude(self, name: str, expected: Optional[float]) -> StepReport:
        actual = self._volume_mm3()
        step = StepReport(name, expected_mm3=expected, actual_mm3=actual)
        if expected is None or actual is None:
            step.status = "unchecked"
        else:
            step.status = "ok" if _close(actual, expected) else "mismatch"
        return step

    def export(self, path: str) -> str:
        """Save the active SolidWorks part; format chosen by extension (.stl .step .sldprt)."""
        if self.app is None:
            self.connect()
        doc = self.app.ActiveDoc
        if doc is None:
            raise SWError("No active document in SolidWorks. Build a part first.")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        if os.path.exists(path) and not path.lower().endswith(".sldprt"):
            os.remove(path)
        self.call("SaveAs3", doc.SaveAs3, path, 0, 2)
        if not os.path.exists(path):
            raise SWError("SolidWorks did not write the file.")
        return path
