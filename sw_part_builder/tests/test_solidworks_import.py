import pytest


def test_backend_imports_without_solidworks():
    from partbuilder.backends import solidworks
    assert isinstance(solidworks.AVAILABLE, bool)


def test_volume_tolerance_helper():
    from partbuilder.backends.solidworks import _close
    assert _close(100000.0, 100400.0)
    assert not _close(100000.0, 110000.0)


def test_feature_type_name_supports_method_and_string_property():
    from partbuilder.backends.solidworks import SolidWorksBuilder

    class MethodFeature:
        def GetTypeName2(self):
            return "RefPlane"

    class PropertyFeature:
        GetTypeName2 = "ProfileFeature"

    assert SolidWorksBuilder._feature_type_name(MethodFeature()) == "RefPlane"
    assert SolidWorksBuilder._feature_type_name(PropertyFeature()) == "ProfileFeature"


def test_build_order_groups_bosses_before_cuts_and_modifiers():
    from partbuilder.backends.solidworks import _ordered_build_features
    from partbuilder.core.model import EdgeModifier, Extrude, Rect, Sketch

    sketch = Sketch(name="Profile", entities=[Rect()])
    boss = Extrude(name="Boss", sketch="Profile", operation="boss")
    cut = Extrude(name="Cut", sketch="Profile", operation="cut")
    fillet = EdgeModifier(
        name="Fillet", feature_target="Boss", modifier_type="fillet",
        radius_mm=1, target_edges=["ZWRnZS1yZWY="],
    )

    assert _ordered_build_features([sketch, cut, fillet, boss]) == [
        sketch, boss, cut, fillet,
    ]


def test_build_dxf_binds_recipe_geometry_and_preserves_explicit_depths(tmp_path):
    ezdxf = pytest.importorskip("ezdxf")
    from ezdxf import units
    from partbuilder.backends.solidworks import SolidWorksBuilder

    drawing = ezdxf.new()
    drawing.units = units.MM
    modelspace = drawing.modelspace()
    modelspace.add_lwpolyline(
        [(0, 0), (60, 0), (60, 40), (0, 40)], close=True,
    )
    modelspace.add_circle((20, 20), radius=5)
    dxf_path = tmp_path / "part.dxf"
    drawing.saveas(dxf_path)

    recipe = {
        "name": "PartFromDXF",
        "features": [
            {
                "type": "sketch", "name": "BaseProfile",
                "dxf_geometry_id": "profile-0001",
            },
            {
                "type": "extrude", "name": "Base", "sketch": "BaseProfile",
                "operation": "boss", "depth": 18,
            },
            {
                "type": "sketch", "name": "HoleProfile",
                "dxf_geometry_id": "profile-0002",
            },
            {
                "type": "extrude", "name": "Hole", "sketch": "HoleProfile",
                "operation": "cut", "end": "through_all", "depth": 0,
            },
        ],
    }
    builder = SolidWorksBuilder()
    builder.build_part = lambda part, expected=None: (part, expected)

    built, expected = builder.build_dxf(str(dxf_path), recipe)

    assert expected is None
    assert built.features[1].depth == 18
    assert built.features[1].operation == "boss"
    assert built.features[3].operation == "cut"
    assert built.features[0].entities[0] == Rect(
        cx=30, cy=20, width=60, height=40,
    )
    assert built.features[2].entities[0] == Circle(
        cx=20, cy=20, diameter=10,
    )
    assert "dxf_geometry_id" not in built.features[0].to_dict()


@pytest.mark.parametrize(
    ("direction", "expected_api_direction"),
    [("one", True), ("reverse", False)],
)
def test_feature_cut4_arguments_select_profile_and_map_direction(
    direction, expected_api_direction,
):
    from partbuilder.backends.solidworks import SolidWorksBuilder
    from partbuilder.core.model import Extrude

    events = []
    feature = type("Feature", (), {})()

    class SketchFeature:
        def Select2(self, append, mark):
            events.append(("select", append, mark))
            return True

    class FeatureManager:
        def FeatureCut4(self, *args):
            events.append(("FeatureCut4", args))
            return feature

    class Model:
        def __init__(self):
            self.FeatureManager = FeatureManager()

        def ClearSelection2(self, clear):
            events.append(("clear", clear))

    messages = []
    builder = SolidWorksBuilder(log=messages.append)
    builder.model = Model()
    result = builder._create_extrude(
        Extrude(
            name="Cut",
            sketch="CutSketch",
            operation="cut",
            end="through_all",
            depth=0.0,
            direction=direction,
        ),
        SketchFeature(),
        flip=False,
    )

    assert result is feature
    assert [event[0] for event in events] == ["clear", "select", "FeatureCut4"]
    args = events[-1][1]
    assert len(args) == 27
    assert args[:7] == (
        True, False, expected_api_direction, 1, 0, 0.0, 0.0,
    )
    assert args[7:18] == (
        False, False, False, False, 0.0, 0.0,
        False, False, False, False, False,
    )
    assert args[18:] == (
        False, True, False, False, False, 0, 0.0, False, False,
    )
    assert any("depth=0 m" in message for message in messages)
    assert any("sketch selected=True" in message for message in messages)
    assert any("returned feature=True" in message for message in messages)


def test_feature_cut4_none_return_has_actionable_diagnostic():
    from partbuilder.backends.solidworks import SWError, SolidWorksBuilder
    from partbuilder.core.model import Extrude

    class SketchFeature:
        def Select2(self, append, mark):
            return True

    class FeatureManager:
        def FeatureCut4(self, *args):
            return None

    class Model:
        def __init__(self):
            self.FeatureManager = FeatureManager()

        def ClearSelection2(self, clear):
            pass

    builder = SolidWorksBuilder()
    builder.model = Model()
    with pytest.raises(SWError, match="FeatureCut4 returned no feature.*sketch selected=True"):
        builder._create_extrude(
            Extrude(
                name="Cut",
                sketch="CutSketch",
                operation="cut",
                end="through_all",
                depth=0.0,
            ),
            SketchFeature(),
            flip=False,
        )


def test_feature_cut4_converts_blind_depth_from_mm_to_meters():
    from partbuilder.backends.solidworks import SolidWorksBuilder
    from partbuilder.core.model import Extrude

    calls = []

    class SketchFeature:
        def Select2(self, append, mark):
            return True

    class FeatureManager:
        def FeatureCut4(self, *args):
            calls.append(args)
            return type("Feature", (), {})()

    class Model:
        def __init__(self):
            self.FeatureManager = FeatureManager()

        def ClearSelection2(self, clear):
            pass

    builder = SolidWorksBuilder()
    builder.model = Model()
    builder._create_extrude(
        Extrude(
            name="BlindCut",
            sketch="CutSketch",
            operation="cut",
            end="blind",
            depth=25.0,
        ),
        SketchFeature(),
        flip=False,
    )

    assert calls[0][3:7] == (0, 0, 0.025, 0.0)


def test_symmetric_through_all_cut_uses_double_ended_api_mapping():
    from partbuilder.backends.solidworks import SolidWorksBuilder
    from partbuilder.core.model import Extrude

    calls = []

    class SketchFeature:
        def Select2(self, append, mark):
            return True

    class FeatureManager:
        def FeatureCut4(self, *args):
            calls.append(args)
            return type("Feature", (), {})()

    class Model:
        def __init__(self):
            self.FeatureManager = FeatureManager()

        def ClearSelection2(self, clear):
            pass

    builder = SolidWorksBuilder()
    builder.model = Model()
    builder._create_extrude(
        Extrude(
            name="MountingHoles",
            sketch="MountingHoles",
            operation="cut",
            end="through_all",
            depth=10.0,
            direction="symmetric",
        ),
        SketchFeature(),
        flip=False,
    )

    args = calls[0]
    assert len(args) == 27
    assert args[:7] == (False, False, False, 1, 1, 0.01, 0.01)
