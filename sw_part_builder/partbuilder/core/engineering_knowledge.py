"""Reference catalog for engineering annotations and symbols.

Definitions describe intended knowledge-base coverage; they do not imply that
the corresponding visual symbols can already be recognized.
"""
from dataclasses import asdict, dataclass
from typing import Literal


RecognitionStatus = Literal["implemented_text_only", "not_implemented"]


@dataclass(frozen=True)
class SymbolDefinition:
    identifier: str
    category: str
    standard_references: tuple[str, ...]
    textual_representations: tuple[str, ...]
    visual_patterns: tuple[str, ...]
    meaning: str
    geometry_implications: tuple[str, ...]
    validation_rules: tuple[str, ...]
    recognition_status: RecognitionStatus
    recognition_limitations: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


_TEXT_LIMITATIONS = (
    "Text-pattern matching is not equivalent to recognizing the graphical symbol.",
    "Interpretation must be checked against the drawing's declared standard.",
)
_NOT_IMPLEMENTED = (
    "No graphical symbol recognizer is implemented for this definition.",
    "Unknown or visually similar marks must be sent for human review.",
)


def _definition(
    identifier: str,
    category: str,
    representations: tuple[str, ...],
    meaning: str,
    *,
    visual_patterns: tuple[str, ...] = (),
    geometry_implications: tuple[str, ...] = (),
    validation_rules: tuple[str, ...] = (),
    standard_references: tuple[str, ...] = (),
    recognized_as_text: bool = False,
) -> SymbolDefinition:
    return SymbolDefinition(
        identifier=identifier,
        category=category,
        standard_references=standard_references,
        textual_representations=representations,
        visual_patterns=visual_patterns,
        meaning=meaning,
        geometry_implications=geometry_implications,
        validation_rules=validation_rules,
        recognition_status=(
            "implemented_text_only" if recognized_as_text else "not_implemented"
        ),
        recognition_limitations=_TEXT_LIMITATIONS if recognized_as_text else _NOT_IMPLEMENTED,
    )


_DEFINITIONS = (
    _definition("dimension.diameter", "dimension", ("Ø", "⌀", "%%c"), "Diameter callout.",
                visual_patterns=("diameter prefix before a numeric value",),
                geometry_implications=("circular feature size",),
                recognized_as_text=True),
    _definition("dimension.radius", "dimension", ("R",), "Radius callout.",
                visual_patterns=("R prefix before a numeric value",),
                geometry_implications=("arc or fillet radius",),
                recognized_as_text=True),
    _definition("dimension.tolerance_plus_minus", "dimension", ("±", "+/-"),
                "Symmetric plus/minus tolerance.",
                visual_patterns=("plus-minus glyph adjacent to a value",)),
    _definition("dimension.angle", "dimension", ("°", "DEG"), "Angular dimension.",
                visual_patterns=("degree mark or DEG suffix",),
                geometry_implications=("angular constraint",),
                recognized_as_text=True),
    _definition("dimension.depth", "dimension", ("DEPTH",), "Depth annotation.",
                visual_patterns=("depth symbol or explicit depth label",),
                geometry_implications=("feature depth only when the target feature is known",)),
    _definition("dimension.thread", "dimension", ("M", "UNC", "UNF"),
                "Thread specification.",
                visual_patterns=("thread designation with size and optional pitch/series",),
                geometry_implications=("threaded feature; not a plain cylindrical hole",)),
    _definition("dimension.counterbore", "dimension", ("⌴", "COUNTERBORE", "CBORE"),
                "Counterbore callout.",
                visual_patterns=("counterbore symbol followed by diameter and depth",),
                geometry_implications=("stepped cylindrical hole",)),
    _definition("dimension.countersink", "dimension", ("⌵", "COUNTERSINK", "CSK"),
                "Countersink callout.",
                visual_patterns=("countersink symbol followed by diameter and angle",),
                geometry_implications=("conical entry to a hole",)),
    _definition("gdt.flatness", "geometric_tolerance", ("FLATNESS",), "Flatness control.",
                visual_patterns=("flatness symbol in a feature-control frame",),
                standard_references=("ASME Y14.5", "ISO 1101")),
    _definition("gdt.straightness", "geometric_tolerance", ("STRAIGHTNESS",),
                "Straightness control.",
                visual_patterns=("straightness symbol in a feature-control frame",),
                standard_references=("ASME Y14.5", "ISO 1101")),
    _definition("gdt.circularity", "geometric_tolerance", ("CIRCULARITY",),
                "Circularity (roundness) control.",
                visual_patterns=("circularity symbol in a feature-control frame",),
                standard_references=("ASME Y14.5", "ISO 1101")),
    _definition("gdt.cylindricity", "geometric_tolerance", ("CYLINDRICITY",),
                "Cylindricity control.",
                visual_patterns=("cylindricity symbol in a feature-control frame",),
                standard_references=("ASME Y14.5", "ISO 1101")),
    _definition("gdt.parallelism", "geometric_tolerance", ("∥", "PARALLELISM"),
                "Parallelism control.",
                visual_patterns=("parallelism symbol in a feature-control frame",),
                standard_references=("ASME Y14.5", "ISO 1101")),
    _definition("gdt.perpendicularity", "geometric_tolerance", ("⟂", "PERPENDICULARITY"),
                "Perpendicularity control.",
                visual_patterns=("perpendicularity symbol in a feature-control frame",),
                standard_references=("ASME Y14.5", "ISO 1101")),
    _definition("gdt.position", "geometric_tolerance", ("⌖", "POSITION"),
                "Position control.",
                visual_patterns=("position symbol in a feature-control frame",),
                standard_references=("ASME Y14.5", "ISO 1101")),
    _definition("gdt.concentricity", "geometric_tolerance", ("◎", "CONCENTRICITY"),
                "Concentricity control.",
                visual_patterns=("concentricity symbol in a feature-control frame",),
                standard_references=("ASME Y14.5", "ISO 1101")),
    _definition("gdt.runout", "geometric_tolerance", ("↗", "RUNOUT"),
                "Runout control.",
                visual_patterns=("runout symbol in a feature-control frame",),
                standard_references=("ASME Y14.5", "ISO 1101")),
    _definition("datum.reference", "datum", ("DATUM",),
                "Datum feature reference.",
                visual_patterns=("datum identifier in a boxed label attached to geometry",)),
    _definition("drawing.section", "view_annotation", ("SECTION", "SECT."),
                "Section-view label or cutting-plane reference.",
                visual_patterns=("cutting-plane line, arrows, and matching section label",),
                recognized_as_text=True),
    _definition("drawing.detail", "view_annotation", ("DETAIL",),
                "Detail-view label.",
                visual_patterns=("detail boundary and matching view label",),
                recognized_as_text=True),
    _definition("drawing.centerline", "line_type", (), "Center or axis line.",
                visual_patterns=("alternating long-short line pattern",)),
    _definition("drawing.hidden_line", "line_type", (), "Hidden edge line.",
                visual_patterns=("short-dash line pattern",)),
    _definition("drawing.projection_method", "projection", ("FIRST ANGLE", "THIRD ANGLE"),
                "Projection convention annotation.",
                visual_patterns=("standardized first-angle or third-angle projection symbol",),
                standard_references=("ASME Y14.3-2012 (R2024)", "ISO 5456 series"),
                recognized_as_text=True),
    _definition("manufacturing.surface_finish", "manufacturing", ("Ra", "Rz"),
                "Surface texture text annotation; symbol meaning may require more context.",
                visual_patterns=("surface-texture mark with value and optional modifiers",),
                standard_references=("ISO 21920 series (confirm applicable part/revision)",),
                recognized_as_text=True),
    _definition("manufacturing.weld", "manufacturing", (),
                "Weld type, size, and location annotation.",
                visual_patterns=("arrow, reference line, and weld symbol elements",)),
    _definition("manufacturing.material", "manufacturing", ("MATERIAL",),
                "Material note or specification.",
                visual_patterns=("material field or general note",)),
    _definition("manufacturing.heat_treatment", "manufacturing", ("HEAT TREAT",),
                "Heat-treatment or condition note.",
                visual_patterns=("heat-treatment note and any stated condition",)),
)

SYMBOL_DEFINITIONS = {item.identifier: item for item in _DEFINITIONS}


def get_symbol_definition(identifier: str) -> SymbolDefinition:
    """Return a known definition or raise KeyError for unknown identifiers."""
    return SYMBOL_DEFINITIONS[identifier]
