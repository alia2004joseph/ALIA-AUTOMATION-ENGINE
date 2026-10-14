import math
import os

import pytest

from partbuilder.core.model import (
    Circle, EdgeModifier, Extrude, Part, Polygon, Rect, Revolve, Sketch, Sketch3D,
    WeldmentMember,
)
from partbuilder.core.validate import (
    _closed_path_shapes, has_errors, parse_blueprint_vectors,
    parse_universal_dxf, segment_and_flatten_blueprint, validate,
)

EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "examples")


def test_json_roundtrip():
    p = Part.load(os.path.join(EXAMPLES, "l_bracket.json"))
    assert Part.from_json(p.to_json()).to_dict() == p.to_dict()


def test_inline_sketch_recipe_normalizes_to_ordered_named_sketches():
    recipe = {
        "name": "MultiFeaturePart",
        "features": [
            {
                "type": "extrude",
                "name": "BaseBlock",
                "operation": "boss",
                "direction": "one_direction",
                "end": "blind",
                "depth": 30.0,
                "sketch": {
                    "plane": "Top",
                    "offset": 0.0,
                    "entities": [
                        {
                            "type": "rect",
                            "cx": 0,
                            "cy": 0,
                            "width": 100,
                            "height": 60,
                        }
                    ],
                },
            },
            {
                "type": "extrude",
                "name": "BoltHole",
                "operation": "cut",
                "direction": "one_direction",
                "end": "through_all",
                "depth": 0.0,
                "sketch": {
                    "plane": "Top",
                    "offset": 0.0,
                    "entities": [
                        {"type": "circle", "cx": 35, "cy": 15, "diameter": 10}
                    ],
                },
            },
        ],
    }

    part = Part.from_dict(recipe)
    assert [type(feature) for feature in part.features] == [
        Sketch, Extrude, Sketch, Extrude,
    ]
    assert part.features[1].sketch == "BaseBlock_Sketch"
    assert part.features[3].sketch == "BoltHole_Sketch"
    assert part.features[1].direction == "one"
    assert part.features[3].depth == 0
    assert part.features[0].entities[0] == Rect(
        cx=0, cy=0, width=100, height=60,
    )
    assert part.features[2].entities[0] == Circle(
        cx=35, cy=15, diameter=10,
    )
    assert not has_errors(validate(part))
    assert Part.from_json(part.to_json()).to_dict() == part.to_dict()


def test_examples_validate_clean():
    for name in ("enclosure.json", "l_bracket.json"):
        issues = validate(Part.load(os.path.join(EXAMPLES, name)))
        assert not has_errors(issues), [str(i) for i in issues]


def test_cut_before_boss_is_error():
    p = Part(features=[
        Sketch(name="S", entities=[Rect()]),
        Extrude(name="E", sketch="S", operation="cut"),
    ])
    assert has_errors(validate(p))


def test_overlapping_contours_error():
    p = Part(features=[
        Sketch(name="S", entities=[Rect(width=20, height=20), Circle(cx=10, diameter=10)]),
        Extrude(name="E", sketch="S"),
    ])
    assert any("overlap" in i.message for i in validate(p))


def test_self_intersecting_polygon_error():
    bowtie = Polygon(points=[(0, 0), (10, 10), (10, 0), (0, 10)])
    p = Part(features=[Sketch(name="S", entities=[bowtie]), Extrude(name="E", sketch="S")])
    assert any("cross" in i.message for i in validate(p))


def test_rename_updates_references():
    p = Part.default()
    assert p.rename_feature(p.features[0], "Profile")
    assert p.features[1].sketch == "Profile"
    assert not p.rename_feature(p.features[1], "Profile")  # duplicate name


def test_advanced_features_json_roundtrip():
    part = Part(features=[
        Sketch(name="Profile", entities=[Rect(width=30, height=20)]),
        Extrude(name="Base", sketch="Profile", depth=12),
        Revolve(name="Turned", sketch_name="Profile", axis_name="Axis1", angle=180),
        EdgeModifier(
            name="Round", feature_target="Base", modifier_type="fillet",
            radius_mm=2, target_edges=["ZWRnZS1yZWY="],
        ),
        Sketch3D(name="FramePath", segments=[((0, 0, 0), (0, 0, 40))]),
        WeldmentMember(
            name="Frame", sketch_3d_name="FramePath", profile_path=r"C:\profiles\frame.sldlfp",
            corner_treatment=1,
        ),
    ])
    assert Part.from_json(part.to_json()).to_dict() == part.to_dict()


def test_edge_modifier_uses_feature_bounds_for_radius_preflight():
    part = Part(features=[
        Sketch(name="Profile", entities=[Rect(width=20, height=20)]),
        Extrude(name="Base", sketch="Profile", depth=10),
        EdgeModifier(
            name="TooLarge", feature_target="Base", modifier_type="fillet",
            radius_mm=6, target_edges=["ZWRnZS1yZWY="],
        ),
    ])
    assert any("exceeds half the smallest bounding dimension" in i.message for i in validate(part))


def test_open_intersecting_3d_wireframe_is_flagged_as_weldment_path():
    part = Part(features=[Sketch3D(
        name="Wire",
        segments=[
            ((0, 0, 0), (10, 10, 0)),
            ((0, 10, 0), (10, 0, 0)),
        ],
    )])
    assert any("likely weldment path" in i.message for i in validate(part))


def test_weldment_validation_checks_profile_file_and_corner_type(tmp_path):
    profile = tmp_path / "tube.sldlfp"
    profile.write_bytes(b"")
    part = Part(features=[
        Sketch3D(name="Path", segments=[((0, 0, 0), (0, 0, 50))]),
        WeldmentMember(
            name="Tube", sketch_3d_name="Path", profile_path=str(profile),
            corner_treatment=2,
        ),
    ])
    assert not has_errors(validate(part))


def test_closed_vector_lines_are_parsed_as_rectangle():
    shapes = _closed_path_shapes([
        ((0.0, 0.0), (60.0, 0.0)),
        ((60.0, 0.0), (60.0, 40.0)),
        ((60.0, 40.0), (0.0, 40.0)),
        ((0.0, 40.0), (0.0, 0.0)),
    ])
    assert shapes == [{
        "type": "rect", "cx": 30.0, "cy": 20.0,
        "width": 60.0, "height": 40.0,
    }]


def test_vector_parser_returns_empty_for_unsupported_or_missing_file(tmp_path):
    assert parse_blueprint_vectors(str(tmp_path / "drawing.svg")) == []
    assert parse_blueprint_vectors(str(tmp_path / "missing.pdf")) == []


def test_dxf_closed_polyline_is_converted_to_mm_rectangle(tmp_path):
    ezdxf = pytest.importorskip("ezdxf")
    from ezdxf import units

    drawing = ezdxf.new()
    drawing.units = units.MM
    drawing.modelspace().add_lwpolyline(
        [(0, 0), (60, 0), (60, 40), (0, 40)],
        close=True,
    )
    path = tmp_path / "outline.dxf"
    drawing.saveas(path)

    assert parse_blueprint_vectors(str(path)) == [{
        "type": "rect", "cx": 30.0, "cy": 20.0,
        "width": 60.0, "height": 40.0,
    }]


def test_universal_dxf_groups_outer_profile_and_nested_hole_and_ignores_text(tmp_path):
    ezdxf = pytest.importorskip("ezdxf")
    from ezdxf import units

    drawing = ezdxf.new()
    drawing.units = units.MM
    modelspace = drawing.modelspace()
    modelspace.add_lwpolyline(
        [(0.24, 0.24), (60.26, 0.24), (60.26, 40.26), (0.24, 40.26)],
        close=True,
    )
    modelspace.add_circle((20.24, 20.24), radius=5.24)
    modelspace.add_text("60", dxfattribs={"height": 2})
    drawing.layers.new("ALIA_FILLET")
    modelspace.add_arc(
        (50, 30), radius=2.26, start_angle=0, end_angle=90,
        dxfattribs={"layer": "ALIA_FILLET"},
    )
    path = tmp_path / "profiles.dxf"
    drawing.saveas(path)

    parsed = parse_universal_dxf(str(path))

    assert parsed["status"] == "ok"
    assert parsed["units"] == "mm"
    assert len(parsed["boss_geometry"]) == 1
    assert len(parsed["cut_geometry"]) == 1
    assert parsed["boss_geometry"][0]["entities"][0]["type"] == "rect"
    assert parsed["boss_geometry"][0]["entities"][0]["width"] == 60.0
    assert parsed["cut_geometry"][0]["entities"][0] == {
        "type": "circle", "cx": 20.0, "cy": 20.0, "diameter": 10.5,
    }
    assert parsed["modifiers"][0]["type"] == "fillet"
    assert parsed["modifiers"][0]["geometry"]["radius_mm"] == 2.5
    assert parsed["ignored_entities"] == 1


def test_universal_dxf_requires_declared_units_and_closed_boss(tmp_path):
    ezdxf = pytest.importorskip("ezdxf")
    drawing = ezdxf.new()
    path = tmp_path / "unitless.dxf"
    drawing.saveas(path)

    assert parse_universal_dxf(str(path))["status"] == "error"


def test_raster_parser_returns_local_closed_profile_and_circle():
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    from partbuilder.core.raster_parser import convert_raster_array_to_vectors

    image = np.full((800, 1000), 255, dtype=np.uint8)
    cv2.rectangle(image, (100, 400), (500, 700), 0, 4)
    cv2.circle(image, (300, 550), 40, 0, 4)
    cv2.rectangle(image, (650, 40), (950, 300), 0, 5)

    shapes = convert_raster_array_to_vectors(image, target_dpi=300)

    assert shapes
    profile = next(shape for shape in shapes if shape["type"] == "polygon")
    xs = [point[0] for point in profile["points"]]
    ys = [point[1] for point in profile["points"]]
    assert min(xs) >= -0.3
    assert min(ys) >= -0.3
    assert max(xs) == pytest.approx(34.0, abs=0.3)
    assert max(ys) == pytest.approx(25.5, abs=0.3)
    for index, (x, y) in enumerate(profile["points"]):
        next_x, next_y = profile["points"][(index + 1) % len(profile["points"])]
        assert x == pytest.approx(next_x) or y == pytest.approx(next_y)
    circle = next(shape for shape in shapes if shape["type"] == "circle")
    assert circle["diameter"] == pytest.approx(6.77, abs=0.5)
    for shape in shapes:
        values = (
            [coordinate for point in shape["points"] for coordinate in point]
            if shape["type"] == "polygon"
            else [shape["cx"], shape["cy"], shape["diameter"]]
        )
        assert all(value * 2 == round(value * 2) for value in values)


def test_raster_parser_applies_uniform_target_bounds_calibration():
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    from partbuilder.core.raster_parser import convert_raster_array_to_vectors

    image = np.full((800, 1000), 255, dtype=np.uint8)
    cv2.rectangle(image, (100, 400), (500, 700), 0, 4)
    shapes = convert_raster_array_to_vectors(
        image,
        target_dpi=300,
        target_bounds_mm=(60, 45),
    )

    profile = next(shape for shape in shapes if shape["type"] == "polygon")
    xs = [point[0] for point in profile["points"]]
    ys = [point[1] for point in profile["points"]]
    assert max(xs) - min(xs) == pytest.approx(60, abs=0.5)
    assert max(ys) - min(ys) == pytest.approx(45, abs=0.5)

    with pytest.raises(ValueError, match="different aspect ratio"):
        convert_raster_array_to_vectors(
            image,
            target_dpi=300,
            target_bounds_mm=(60, 75),
        )


def test_raster_pdf_fallback_logs_and_extracts_closed_contours(tmp_path):
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    from PIL import Image
    from reportlab.pdfgen import canvas

    image = np.full((800, 1000), 255, dtype=np.uint8)
    cv2.rectangle(image, (100, 400), (500, 700), 0, 4)
    cv2.circle(image, (300, 550), 40, 0, 4)
    source_image = tmp_path / "scan.png"
    Image.fromarray(image).save(source_image)

    pdf_path = tmp_path / "scanned_drawing.pdf"
    document = canvas.Canvas(str(pdf_path), pagesize=(720, 576))
    document.drawImage(str(source_image), 0, 0, width=720, height=576)
    document.save()

    messages = []
    shapes = parse_blueprint_vectors(str(pdf_path), log=messages.append)

    assert any("Scanned drawing image sheet detected" in item for item in messages)
    assert any(shape["type"] == "polygon" for shape in shapes)
    assert any(shape["type"] == "circle" for shape in shapes)


def test_mixed_text_and_raster_pdf_is_not_imported_as_a_sketch(tmp_path):
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    from PIL import Image
    from reportlab.pdfgen import canvas

    image = np.full((800, 1000), 255, dtype=np.uint8)
    cv2.rectangle(image, (100, 400), (500, 700), 0, 4)
    source_image = tmp_path / "mixed_scan.png"
    Image.fromarray(image).save(source_image)

    pdf_path = tmp_path / "mixed_drawing.pdf"
    document = canvas.Canvas(str(pdf_path), pagesize=(720, 576))
    document.drawString(30, 540, "Orthographic drawing - dimensions in mm")
    document.drawImage(str(source_image), 0, 0, width=720, height=576)
    document.save()

    messages = []
    assert parse_blueprint_vectors(str(pdf_path), log=messages.append) == []
    assert any(
        "Automatic contour tracing is unsafe" in message
        and "no geometry was imported" in message
        for message in messages
    )


def test_segment_and_flatten_blueprint_crops_roi_and_snaps_orthogonal_lines(tmp_path):
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    image = np.full((800, 1000), 255, dtype=np.uint8)
    cv2.rectangle(image, (50, 420), (420, 700), 0, 4)
    cv2.rectangle(image, (700, 40), (950, 280), 0, 5)
    path = tmp_path / "orthographic.png"
    assert cv2.imwrite(str(path), image)

    report = segment_and_flatten_blueprint(str(path))

    assert report["status"] == "ok"
    assert report["detected_view"] == "front"
    assert report["calibration"]["baseline_assumed_mm"] == 60.0
    assert report["features"]
    for line in report["features"]:
        assert line["type"] == "line"
        assert line["x0"] == line["x1"] or line["y0"] == line["y1"]
    assert max(line["x0"] for line in report["features"]) < 70


def test_segment_and_flatten_blueprint_reports_missing_input(tmp_path):
    report = segment_and_flatten_blueprint(str(tmp_path / "missing.png"))
    assert report == {
        "status": "error",
        "note": f"Target image file missing: '{tmp_path / 'missing.png'}'.",
    }
