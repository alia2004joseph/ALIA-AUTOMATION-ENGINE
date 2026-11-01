"""Drawing-independent engineering intermediate representation.

This schema records evidence and uncertainty; it does not create Part Builder
features or infer missing dimensions.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


InformationStatus = Literal[
    "directly_observed",
    "interpreted",
    "inferred",
    "user_confirmed",
    "unknown",
]


@dataclass
class EngineeringEvidence:
    source_id: str
    source_kind: str
    description: str
    source_location: dict[str, Any] = field(default_factory=dict)
    recognition_method: str = "unspecified"
    status: InformationStatus = "directly_observed"


@dataclass
class InterpretedFeature:
    feature_id: str
    feature_type: str
    status: InformationStatus = "unknown"
    geometry: dict[str, Any] = field(default_factory=dict)
    dimensions: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[EngineeringEvidence] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)


@dataclass
class EngineeringIntermediateRepresentation:
    source_document: dict[str, Any]
    features: list[InterpretedFeature] = field(default_factory=list)
    schema_version: str = "1.0"
    units: str | None = None
    assumptions: list[str] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_engineering_ir(
    representation: EngineeringIntermediateRepresentation | dict[str, Any],
) -> list[str]:
    """Return structural/uncertainty errors without filling in missing values."""
    data = (
        representation.to_dict()
        if isinstance(representation, EngineeringIntermediateRepresentation)
        else representation
    )
    errors = []
    if not isinstance(data, dict):
        return ["Engineering IR must be a JSON object."]
    if data.get("schema_version") != "1.0":
        errors.append("Engineering IR schema_version must be '1.0'.")
    if not isinstance(data.get("source_document"), dict):
        errors.append("Engineering IR source_document must be an object.")
    features = data.get("features")
    if not isinstance(features, list):
        errors.append("Engineering IR features must be a list.")
        return errors

    valid_statuses = {
        "directly_observed", "interpreted", "inferred", "user_confirmed", "unknown",
    }
    seen_ids = set()
    for index, feature in enumerate(features):
        prefix = f"features[{index}]"
        if not isinstance(feature, dict):
            errors.append(f"{prefix} must be an object.")
            continue
        feature_id = feature.get("feature_id")
        if not isinstance(feature_id, str) or not feature_id.strip():
            errors.append(f"{prefix}.feature_id must be a non-empty string.")
        elif feature_id in seen_ids:
            errors.append(f"{prefix}.feature_id duplicates '{feature_id}'.")
        else:
            seen_ids.add(feature_id)
        if not isinstance(feature.get("feature_type"), str) or not feature["feature_type"].strip():
            errors.append(f"{prefix}.feature_type must be a non-empty string.")
        if feature.get("status") not in valid_statuses:
            errors.append(f"{prefix}.status must describe its information provenance.")
        for field_name in (
            "geometry", "dimensions", "relationships", "evidence",
            "assumptions", "uncertainties", "missing_information",
        ):
            expected = dict if field_name == "geometry" else list
            if not isinstance(feature.get(field_name, expected()), expected):
                errors.append(f"{prefix}.{field_name} must be a {expected.__name__}.")
        evidence = feature.get("evidence", [])
        if isinstance(evidence, list):
            for evidence_index, item in enumerate(evidence):
                if not isinstance(item, dict) or not item.get("source_id"):
                    errors.append(
                        f"{prefix}.evidence[{evidence_index}] needs a source_id."
                    )
        geometry = feature.get("geometry", {})
        missing_information = feature.get("missing_information", [])
        if isinstance(geometry, dict) and isinstance(missing_information, list):
            for name, value in geometry.items():
                if value is None and name not in missing_information:
                    errors.append(
                        f"{prefix}.geometry.{name} is unknown and must be listed "
                        "in missing_information."
                    )
    return errors
