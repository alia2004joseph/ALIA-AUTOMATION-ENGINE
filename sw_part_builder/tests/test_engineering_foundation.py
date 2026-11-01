import json

from partbuilder.core.engineering_ir import (
    EngineeringEvidence,
    EngineeringIntermediateRepresentation,
    InterpretedFeature,
    validate_engineering_ir,
)
from partbuilder.core.engineering_knowledge import (
    SYMBOL_DEFINITIONS,
    get_symbol_definition,
)
from partbuilder.core.model import Part


def test_engineering_ir_keeps_unknown_geometry_explicit():
    representation = EngineeringIntermediateRepresentation(
        source_document={"file_name": "drawing.pdf"},
        features=[
            InterpretedFeature(
                feature_id="feature-1",
                feature_type="extrusion",
                status="unknown",
                geometry={"depth_mm": None},
                missing_information=["depth_mm"],
                uncertainties=["Depth is not specified in the source."],
                evidence=[
                    EngineeringEvidence(
                        source_id="view-01",
                        source_kind="drawing_view",
                        description="Orthographic candidate",
                    )
                ],
            )
        ],
    )

    assert validate_engineering_ir(representation) == []
    assert representation.to_dict()["features"][0]["geometry"]["depth_mm"] is None


def test_engineering_ir_rejects_untracked_unknowns_and_duplicate_ids():
    data = {
        "schema_version": "1.0",
        "source_document": {},
        "features": [
            {
                "feature_id": "same",
                "feature_type": "hole",
                "status": "inferred",
                "geometry": {"depth_mm": None},
                "dimensions": [],
                "relationships": [],
                "evidence": [],
                "assumptions": [],
                "uncertainties": [],
                "missing_information": [],
            },
            {
                "feature_id": "same",
                "feature_type": "hole",
                "status": "inferred",
                "geometry": {},
                "dimensions": [],
                "relationships": [],
                "evidence": [],
                "assumptions": [],
                "uncertainties": [],
                "missing_information": [],
            },
        ],
    }

    errors = validate_engineering_ir(data)
    assert any("unknown and must be listed" in error for error in errors)
    assert any("duplicates" in error for error in errors)


def test_engineering_ir_validator_reports_malformed_collection_types():
    errors = validate_engineering_ir({
        "schema_version": "1.0",
        "source_document": {},
        "features": [{
            "feature_id": "feature-1",
            "feature_type": "hole",
            "status": "inferred",
            "geometry": "invalid",
            "dimensions": {},
            "relationships": [],
            "evidence": {},
            "assumptions": [],
            "uncertainties": [],
            "missing_information": [],
        }],
    })

    assert any("geometry must be a dict" in error for error in errors)
    assert any("dimensions must be a list" in error for error in errors)
    assert any("evidence must be a list" in error for error in errors)


def test_symbol_catalog_does_not_claim_unimplemented_visual_recognition():
    assert get_symbol_definition("gdt.position").recognition_status == "not_implemented"
    assert get_symbol_definition("dimension.diameter").recognition_status == "implemented_text_only"
    assert all(
        item.recognition_status in {"implemented_text_only", "not_implemented"}
        for item in SYMBOL_DEFINITIONS.values()
    )
    assert json.loads(Part.default().to_json())["units"] == "mm"
