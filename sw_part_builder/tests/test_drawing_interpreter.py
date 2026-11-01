from partbuilder.core.drawing_interpreter import interpret_drawing


def _word(text, x0, top, x1=None):
    return {
        "text": text,
        "bbox_pt": {
            "x0": x0,
            "top": top,
            "x1": x1 if x1 is not None else x0 + len(text) * 5,
            "bottom": top + 8,
        },
    }


def test_interpret_drawing_extracts_dimension_and_drawing_annotation_candidates():
    report = {
        "source_file": "drawing.pdf",
        "pages": [{
            "page_number": 1,
            "width_pt": 612,
            "height_pt": 792,
            "content_type": "vector",
            "native_text": [
                _word("Ø12.5", 100, 100),
                _word("M8x1.25", 150, 100),
                _word("R3", 220, 100),
                _word("45°", 250, 100),
                _word("25.0", 300, 100),
                _word("±0.1", 330, 100),
                _word("mm", 360, 100),
                _word("42", 390, 100),
                _word("SECTION", 100, 140),
                _word("A-A", 140, 140),
                _word("DETAIL", 200, 140),
                _word("B", 240, 140),
                _word("THIRD", 280, 140),
                _word("ANGLE", 320, 140),
                _word("PROJECTION", 365, 140),
                _word("Ra", 440, 140),
                _word("3.2", 455, 140),
            ],
            "raster_analysis": {
                "status": "ok",
                "source_size_px": {"width": 1000, "height": 700},
                "analysis_scale": 1.0,
                "crop_offset_px": {"left": 0, "top": 0},
                "extraction": {
                    "selected_bbox_pt": {
                        "x0": 0,
                        "top": 0,
                        "x1": 612,
                        "bottom": 792,
                    },
                },
                "view_regions": [{
                    "view_id": "view-01",
                    "classification": "orthographic",
                    "confidence": 0.8,
                    "bbox_px": {"x": 100, "y": 120, "width": 300, "height": 200},
                }],
            },
        }],
    }

    result = interpret_drawing(report)

    page = result["pages"][0]
    assert result["schema_version"] == "1.1"
    assert page["projection_convention_candidate"] == "third"
    assert page["views"][0]["semantic_role"] == "layout_reference_candidate"
    assert page["views"][0]["role_confidence"] is None
    assert page["views"][0]["confidence_status"] == "uncalibrated"
    assert page["views"][0]["view_type"] == "orthographic"
    assert [item["dimension_type"] for item in page["dimensions"]] == [
        "diameter", "thread", "radius", "angle", "linear", "linear",
    ]
    assert page["dimensions"][0]["nominal_value"] == "12.5"
    assert page["dimensions"][-2]["raw_text"] == "25.0 ±0.1 mm"
    assert page["dimensions"][-2]["unit"] == "mm"
    assert page["dimensions"][-2]["tolerance"] == {
        "type": "symmetric",
        "value": "0.1",
    }
    assert page["dimensions"][-1]["raw_text"] == "42"
    assert page["dimensions"][-1]["unit"] is None
    assert all(item["review_required"] for item in page["dimensions"])
    assert [item["kind"] for item in page["symbol_annotations"]] == [
        "section_view_label",
        "detail_view_label",
        "projection_method_label",
        "surface_texture_text",
    ]
    assert not any(
        item["raw_text"] == "3.2" for item in page["dimensions"]
    )
    assert [item["association_status"] for item in page["dimensions"]] == [
        "tentative", "tentative", "tentative", "tentative", "unassigned",
        "unassigned",
    ]
    assert page["dimensions"][-1]["interpretation_status"] == "weak_text_candidate"
    assert [item["candidate_view_role"] for item in page["dimensions"]] == [
        "layout_reference_candidate", "layout_reference_candidate",
        "layout_reference_candidate", "layout_reference_candidate", None, None,
    ]
    assert result["summary"]["requires_human_review"] is True
    assert "unspecified" in result["standards_profile"]


def test_interpret_drawing_does_not_claim_raster_ocr_or_definitive_view_roles():
    report = {
        "source_file": "scan.pdf",
        "pages": [{
            "page_number": 1,
            "width_pt": 612,
            "height_pt": 792,
            "content_type": "raster",
            "native_text": [],
            "raster_analysis": {
                "view_regions": [{
                    "view_id": "view-01",
                    "classification": "orthographic",
                    "bbox_px": {"x": 10, "y": 20, "width": 100, "height": 60},
                }],
            },
        }],
    }

    page = interpret_drawing(report)["pages"][0]

    assert page["views"][0]["semantic_role"] == "layout_reference_candidate"
    assert page["views"][0]["bbox_pt"] is None
    assert page["dimensions"] == []
    assert any("Raster-only text" in item for item in page["limitations"])
    assert any("layout reference" in item for item in page["limitations"])
    assert page["layout_reference_view_id"] == "view-01"
    assert page["view_relationships"] == []


def _view_layout_report(projection_words):
    words = [
        _word(word, 10 + index * 36, 10)
        for index, word in enumerate(projection_words)
    ]
    return {
        "source_file": "layout.pdf",
        "pages": [{
            "page_number": 1,
            "width_pt": 612,
            "height_pt": 792,
            "content_type": "mixed",
            "native_text": words,
            "raster_analysis": {
                "source_size_px": {"width": 500, "height": 500},
                "analysis_scale": 1.0,
                "crop_offset_px": {"left": 0, "top": 0},
                "extraction": {
                    "selected_bbox_pt": {
                        "x0": 0,
                        "top": 0,
                        "x1": 612,
                        "bottom": 792,
                    },
                },
                "view_regions": [
                    {
                        "view_id": "front",
                        "classification": "orthographic",
                        "confidence": 0.9,
                        "bbox_px": {"x": 180, "y": 180, "width": 140, "height": 140},
                    },
                    {
                        "view_id": "upper",
                        "classification": "orthographic",
                        "confidence": 0.8,
                        "bbox_px": {"x": 200, "y": 30, "width": 100, "height": 100},
                    },
                    {
                        "view_id": "left",
                        "classification": "orthographic",
                        "confidence": 0.8,
                        "bbox_px": {"x": 20, "y": 200, "width": 100, "height": 100},
                    },
                ],
            },
        }],
    }


def test_interpret_drawing_infers_view_roles_from_third_angle_layout():
    page = interpret_drawing(
        _view_layout_report(["THIRD", "ANGLE", "PROJECTION"])
    )["pages"][0]

    roles = {view["view_id"]: view["semantic_role"] for view in page["views"]}
    assert roles == {
        "front": "layout_reference_candidate",
        "upper": "top_view_candidate",
        "left": "left_side_view_candidate",
    }
    assert page["layout_reference_view_id"] == "front"
    assert page["views"][1]["layout_reference_view_id"] == "front"
    assert page["views"][1]["role_candidate"]["status"] == "tentative"
    assert "layout_reference_is_principal_view" in page["views"][1]["role_candidate"]["assumptions"]
    assert len(page["view_relationships"]) == 2
    assert page["view_relationships"][0]["source_view_id"] == "front"
    assert page["view_relationships"][0]["target_view_id"] == "upper"
    assert all(
        relationship["relationship_type"] == "orthographic_alignment_candidate"
        for relationship in page["view_relationships"]
    )
    assert page["view_relationships"][0]["projection_role_candidate"] == "top_view_candidate"
    assert page["view_relationships"][0]["assumptions"] == [
        "aligned_regions_represent_related_orthographic_views"
    ]
    assert page["view_relationships"][0]["alignment_criteria"] == {
        "maximum_center_offset_ratio": 0.15,
        "minimum_separation_ratio": 0.5,
    }


def test_interpret_drawing_infers_view_roles_from_first_angle_layout():
    page = interpret_drawing(
        _view_layout_report(["FIRST", "ANGLE", "PROJECTION"])
    )["pages"][0]

    roles = {view["view_id"]: view["semantic_role"] for view in page["views"]}
    assert roles == {
        "front": "layout_reference_candidate",
        "upper": "bottom_view_candidate",
        "left": "right_side_view_candidate",
    }
    assert page["projection_convention_candidate"] == "first"


def test_view_roles_remain_reviewable_candidates_with_alternatives():
    page = interpret_drawing(_view_layout_report([]))["pages"][0]

    reference = next(view for view in page["views"] if view["view_id"] == "front")
    assert reference["semantic_role"] == "layout_reference_candidate"
    assert reference["classification_status"] == "ambiguous_candidate"
    assert "front_view_candidate" in reference["candidate_roles"]
    assert "region size alone" in reference["uncertainty"].lower()
    assert all(view["review_required"] for view in page["views"])


def test_unaligned_region_gets_no_projected_role_candidate():
    report = _view_layout_report(["THIRD", "ANGLE", "PROJECTION"])
    report["pages"][0]["raster_analysis"]["view_regions"][1]["bbox_px"] = {
        "x": 20,
        "y": 20,
        "width": 70,
        "height": 60,
    }

    page = interpret_drawing(report)["pages"][0]
    upper = next(view for view in page["views"] if view["view_id"] == "upper")
    relation = next(
        item for item in page["view_relationships"]
        if item["target_view_id"] == "upper"
    )

    assert upper["view_type"] == "orthographic"
    assert upper["semantic_role"] == "unknown_view_candidate"
    assert upper["role_candidate"]["status"] == "unknown"
    assert relation["relationship_type"] == "relative_layout_position_candidate"
    assert relation["status"] == "position_only"
    assert relation["alignment_axis"] is None


def test_isometric_candidate_stays_separate_from_orthographic_relationships():
    report = _view_layout_report([])
    report["pages"][0]["raster_analysis"]["view_regions"][1]["classification"] = (
        "isometric_reference"
    )

    page = interpret_drawing(report)["pages"][0]
    upper = next(view for view in page["views"] if view["view_id"] == "upper")

    assert upper["view_type"] == "isometric_reference"
    assert upper["semantic_role"] == "isometric_reference_candidate"
    assert all(
        "upper" not in (
            relation["source_view_id"],
            relation["target_view_id"],
        )
        for relation in page["view_relationships"]
    )


def test_dimension_candidate_preserves_token_bounds_and_repetition():
    report = {
        "source_file": "dimensions.pdf",
        "pages": [{
            "page_number": 1,
            "width_pt": 612,
            "height_pt": 792,
            "native_text": [
                _word("2xØ8", 101, 100, 125),
                _word("25.0", 40, 130, 62),
                _word("±0.1", 66, 130, 87),
            ],
            "raster_analysis": {},
        }],
    }

    page = interpret_drawing(report)["pages"][0]
    repeated = next(item for item in page["dimensions"] if item["dimension_type"] == "diameter")
    weak_linear = next(
        item for item in page["dimensions"]
        if item["raw_text"].startswith("25.0")
    )

    assert repeated["raw_text"] == "2xØ8"
    assert repeated["nominal_value"] == "8"
    assert repeated["repeat_count"] == 2
    assert repeated["bbox_pt"]["x0"] == 101
    assert weak_linear["bbox_pt"]["x0"] == 40
    assert weak_linear["interpretation_status"] == "text_candidate"


def test_nearest_view_ties_are_reported_as_ambiguous():
    from partbuilder.core.drawing_interpreter import _candidate_view_link

    link = _candidate_view_link(
        {"x0": 9, "top": 9, "x1": 11, "bottom": 11},
        [
            {"view_id": "left", "bbox_pt": {"x0": 0, "top": 0, "x1": 8, "bottom": 20}},
            {"view_id": "right", "bbox_pt": {"x0": 12, "top": 0, "x1": 20, "bottom": 20}},
        ],
        {"width_pt": 100, "height_pt": 100},
    )

    assert link["association_status"] == "ambiguous"
    assert link["candidate_view_id"] is None
    assert set(link["candidate_view_ids"]) == {"left", "right"}
