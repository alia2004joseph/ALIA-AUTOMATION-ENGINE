# Part Builder

Describe a part as a list of features (sketch, extrude, revolve, weldment, ...),
see supported geometry in 3D, then build the list natively in SolidWorks.

## Run (Windows, SolidWorks 2022 installed)

    python -m venv .venv && .venv\Scripts\activate
    pip install -r requirements.txt        # Python 3.10-3.12 recommended (CadQuery)
    python main.py

Open `examples\enclosure.json` or `examples\l_bracket.json` to try it.
Tests (no SolidWorks needed): `python -m pytest`

After code changes, use **Restart GUI** in the top toolbar to launch the updated
application without manually closing the current window first. Confirm the
restart when prompted; unsaved part edits in the current session will be lost.

The editor can import closed vector paths from PDF and DXF drawings. Install
`pdfplumber`, `ezdxf`, `pypdfium2`, and `opencv-python-headless` from
`requirements.txt`. PDF vector coordinates are converted from points to mm;
the UI's legacy DXF import treats unitless coordinates as mm. The rule-based
DXF compiler requires declared DXF units. PDFs containing raster images but no
usable vector outlines fall back to OpenCV contour extraction at 300 DPI. This
assumes the embedded image is placed at its intended physical print scale;
scanned drawings with no known scale cannot yield reliable real-world
dimensions from pixels alone.
PDF sheets that combine native text with raster artwork but lack native drawing
strokes are deliberately not imported as sketch geometry: tracing their page
contours can combine dimensions, annotations, and separate projected views
into a false part. Use a single-view profile or review/enter the geometry
manually instead.

### Drawing analysis and interpretation report

In the Part Builder, choose **Analyze Drawing...** for a PDF, DXF, PNG, JPG,
TIFF, or BMP. Analysis runs in the background, saves reports in a
`<drawing-name>-drawing-analysis` folder beside the input, and previews raster
pages without changing the current part or sketch. PDF uses the existing
native-text/vector inspection and embedded-image pipeline; standalone images
use the raster analyzer; DXF preserves native entities, layers, declared units,
and the existing rule-based profile-parser result. DXF currently has no PNG
preview in this workflow.

The preview defaults to **Drawing views (source layout)** when view candidates
are detected. Its `page-001-drawing-view-layout.png` preserves the full
extracted source image without cropping, stacking, or rearranging views. This
keeps orthographic and isometric views, their drawing-sheet positions, and
surrounding detail/dimension callouts visible even when the heuristic
classifier is uncertain.

Select **Analysis overlay** to inspect detected regions; its PNG preserves the
full frame of the extracted raster image. The overlay is drawn on a
complete-page render so page margins and content outside an embedded image
remain visible. Use **Open PNG** to launch the selected saved image in the
default image viewer. Interpretation details remain in the JSON reports.

The GUI writes `report.json` and `drawing-interpretation.json`. The shared
`inspect_drawing_source(...)` entry point normalizes PDF, DXF, and raster-image
inspection into a common report envelope while preserving source coordinates.
The interpretation report includes explicit units/scale evidence, view and
dimension candidates, source locations, recognition methods, ambiguity,
limitations, and review status (drawing-interpretation schema 1.1).
Layout-based role guesses remain candidates:
the largest orthographic region is exposed only as a layout reference, not
assumed to be the principal/front view. The report separates each candidate's
view type from its role, and records tentative layout relationships when
candidate bounding boxes align within 15% of the smaller region's width or
height and are separated by at least half the larger region's corresponding
dimension. These are explicit, uncalibrated geometric heuristics—not proof
that edges correspond. A top/side role suggested by first- or
third-angle text is conditional on the reference being the principal view;
position alone does not establish projected correspondence. Freely positioned
reference-arrow layouts, graphical projection symbols, and matching edges
across views are not yet recognized. Dimension text alone does not establish
which feature it measures. Nearly tied spatial associations are marked
ambiguous.
Use **Review view inventory** in the analysis window to record a human-reviewed
view type, role, or accepted/rejected relationship. These decisions are saved
under separate `review` fields in `drawing-interpretation.json`; automatic
candidate evidence is retained unchanged.
The review window keeps a drawing reference beside the candidate list, describes
each candidate by its relative page location, and separates the view-type choice
from its orientation/relationship role. Suggestions are explicitly tentative;
the reference overlay labels detected regions with the same view IDs shown in
the list. Choose Unknown when the drawing does not make a view clear. Repeated
front/top/side orientations trigger a confirmation before saving because they
may indicate either separate parts or an accidental duplicate assignment.
Numeric heuristic scores are retained for ranking only; `confidence` is null
unless a calibrated recognizer supplies it. Bare-number matches are explicitly
marked as weak text candidates.

#### Reviewed raster-to-sketch reconstruction (first version)

For a page with an orthographic raster-view candidate, choose **Reconstruct 2D
sketch...** in the drawing analysis window. Select one candidate view, enter an
overall width that you have verified from the drawing, preview the traced
outer contour, and explicitly add it to the current Part. The reconstructed
profile is centered on the Front plane and scaled from that user-entered width;
no drawing text is treated as a dimension. A view already reviewed as
non-orthographic is excluded, and the wizard also requires you to confirm the
selected view before tracing.

After adding, the reconstruction preview stays open for comparison while the
new sketch is selected in the Part Builder. The 3D viewport remains empty until
you add a solid feature with a confirmed depth.

This is deliberately limited to one clean, closed outer contour. Isometric
views, multiple disconnected outlines, detected internal loops, invalid scale,
and contours that touch the crop boundary are rejected. The preview highlights
the proposed contour; inspect it before adding. Holes, inner details, dimension
association, and automatic multi-view reconstruction are not supported. The
result is an editable 2D sketch only; add a confirmed modeling feature such as
an extrusion and depth separately to create a 3D solid.

Offline OCR uses the local Tesseract 5 installation with English data on the
current development machine. The GUI requests local OCR during drawing
analysis; PDF CLI callers can opt in with `--analyze-raster --ocr`.
`pytesseract` alone is not the OCR engine: installations without the separate
Tesseract executable or English language data receive an explicit unavailable
status and no cloud fallback is attempted. Set `ALIA_TESSERACT_CMD` to the
engine executable when it is not on PATH or in a standard Windows install
location. OCR text is review-only and is kept separate from native PDF text.
The symbol catalog and Engineering Intermediate Representation are data
foundations, not recognizers or a 3D reconstruction system. Graphical
GD&T/weld/thread symbols,
scale recovery, and interpretation-to-Part-JSON generation remain incomplete.
All candidates require engineering review; no ISO or ASME drawing convention
is assumed implicitly.

Research references: [ASME Y14.3 orthographic and pictorial views](https://www.asme.org/codes-standards/find-codes-standards/y14-3-orthographic-pictorial-views)
defines drawing view requirements; [Tesseract installation](https://tesseract-ocr.github.io/tessdoc/Installation.html)
and [command-line usage](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html)
document a possible offline OCR engine. Neither reference implies that ALIA
currently performs complete projection-symbol recognition or OCR.

Inspect a PDF before importing it into the sketch editor:

    python main.py --inspect-pdf "drawing.pdf" --output "drawing-report.json"

The JSON report lists page dimensions and rotation, positioned native text,
vector entities and bounding boxes, embedded-image dimensions, content type,
and a reasoned vector-first or raster processing route. PDF geometry is
reported in points from the page's top-left; this report does not infer
millimetre scale or claim to reconstruct a part. To also save page renders,
pass `--render-dir "drawing-rendered" --dpi 300`. The existing UI remains the
default when `main.py` is run without `--inspect-pdf`.

To analyze a PDF yourself on Windows and open its first generated overlay, run
this from PowerShell in the `sw_part_builder` folder:

    .\analyze-drawing.ps1

Choose a PDF in the file picker, or pass its path directly:

    .\analyze-drawing.ps1 "C:\path\to\drawing.pdf"

The script saves `report.json` and page overlay PNGs in a
`<drawing-name>-raster-analysis` folder beside the PDF, then opens the first
overlay in your default image viewer. Candidate line and circle detections are
available in the JSON report but are not drawn on the overlay, keeping it
readable. Embedded-image selection also checks whether an image's source aspect
ratio matches its placement on the PDF page, avoiding stretched logos that can
otherwise be mistaken for the drawing. Optional local reference-fixture tests
compare generated overlays pixel-for-pixel with provided PDFs and baselines.
Those source documents are not distributed with the repository; to run these
tests, add authorized fixtures under `tests/fixtures/` and set
`ALIA_RUN_DRAWING_REFERENCE_TESTS=1`.

High-resolution scanned pages are downscaled for analysis and receive local
lighting correction to reduce paper shadows. The report records the source
image dimensions, analysis scale, and scan preprocessing status. Scan view and
feature detections are heuristic candidates and require manual review; this
does not deskew pages, recover dimensions, or reconstruct a model. A scanned
PDF regression fixtures can be supplied locally to cover this path.

### Rule-based DXF build

`parse_universal_dxf(path)` classifies supported closed planar profiles as
bosses or cuts. It uses explicit layer names containing `BOSS`, `OUTER`,
`PROFILE`, `SOLID`, `CUT`, `HOLE`, `VOID`, or `POCKET` when present; otherwise
it uses nested-loop parity. Only `LINE`, planar `ARC` chains, closed
`LWPOLYLINE`/`POLYLINE`, and `CIRCLE` profiles are interpreted. Bulged
polyline segments and ARC chains are polygonized before sketch creation.
Dimensions, text, and unsupported entities are ignored/reported. This is geometry
classification, not a claim that arbitrary drawing sheets encode a complete
manufacturing model.

When the UI detects native DXF `DIMENSION` entities, it reports their stored
values using the document insertion units and each dimension style's `DIMLFAC`.
It also reports entity counts and spatially clustered candidate geometry
regions, excluding low-support groups and clusters that span nearly the whole
drawing sheet. For dimensioned DXFs with a mostly orthogonal closed profile,
the UI can create a **preliminary** extruded model when at least two native
linear dimensions support a consistent drawing scale and another projection
supports the extrusion thickness. When matching plan and end views contain
repeated circular geometry, the modeler also adds through-hole cuts and reports
the inferred bore size. Countersinks are detected as evidence but are not yet
modeled. The import dialog warns that this replaces the current feature tree.
Review the inferred profile, depth, and hole positions before using the model;
the selected geometry is not guaranteed to be the correct front/top/side view.
Unsupported or ambiguous drawings still stop without creating a solid.

Pass the parser's `profile-0001`-style IDs through a sidecar Part recipe's
sketches using `dxf_geometry_id`; keep extrusion parameters and modifiers
explicit in that recipe:

```json
{
  "name": "DXFPart",
  "features": [
    {"type": "sketch", "name": "BaseProfile", "dxf_geometry_id": "profile-0001"},
    {"type": "extrude", "name": "Base", "sketch": "BaseProfile",
     "operation": "boss", "end": "blind", "depth": 20, "direction": "one"}
  ]
}
```

Use `SolidWorksBuilder.build_dxf("part.dxf", "part.recipe.json")` to compile
the selected profiles. The backend reuses its normal feature tree, which stages
bosses before cuts and modifiers. `FILLET`/`CHAMFER` layer entities are returned
as modifier hints; actual modifiers still require explicit SolidWorks edge
persistent-reference tokens and cannot be inferred from a 2D drawing alone.

## How it flows

    Feature list (core/model.py)  ->  validate (core/validate.py)
            |                                   |
            +--> CadQuery preview (backends/preview_cq.py) --> 3D view (ui/viewport.py)
            |            volumes per feature
            +--> SolidWorks build (backends/solidworks.py) --> checks its volumes against the preview

The feature list is the single source of truth. Both backends only read it.
JSON recipes may either reference standalone named sketches or embed a sketch
object directly in an extrusion's `sketch` field. Embedded sketches are
normalized to deterministic named sketch features when loaded, while the
SolidWorks build phases additive features before cuts.

## Feature tree and viewport controls

Features that reference a sketch or another feature are nested beneath that
parent in the Feature Tree. Use the `>` / `v` row buttons to collapse or expand
branches, or the `Expand` and `Collapse` buttons above the tree. The tree list
scrolls when there are more rows than fit in the panel.

In the 3D viewport, left-drag to orbit freely, right-drag to roll the camera,
use the mouse wheel to zoom, and double-click to reset the view.

## Project layout

    main.py
    partbuilder/
      core/model.py          feature dataclasses, Part, JSON save/load
      core/drawing_sources.py common PDF / DXF / raster inspection entry point
      core/drawing_interpreter.py reviewable drawing candidates and evidence
      core/engineering_ir.py drawing-independent, uncertainty-aware IR
      core/engineering_knowledge.py symbol-definition catalog, separate from recognition
      core/validate.py       part-independent geometry/rule checks
      backends/preview_cq.py CadQuery solid + mesh + per-feature volumes
      backends/solidworks.py COM translator (step-labelled errors, volume check)
      ui/app.py              main window, wiring, background threads
      ui/feature_list.py     feature tree panel
      ui/editors.py          property editors
      ui/viewport.py         VTK offscreen 3D view (drag = rotate, wheel = zoom)
    examples/                sample parts (JSON)
    tests/                   unit tests

## Conventions

Lengths are mm. Planes match SolidWorks defaults:

    Front : sketch x -> X,  sketch y -> Y,  normal +Z
    Top   : sketch x -> X,  sketch y -> -Z, normal +Y
    Right : sketch x -> -Z, sketch y -> Y,  normal +X

Extrude direction: `one` = along the normal, `reverse` = against it, `symmetric` = half each side.
A cut that removes nothing is flagged in the Checks panel.

## Advanced SolidWorks features

The JSON model also supports `Sketch3D`, `Revolve`, `EdgeModifier`, and
`WeldmentMember`. These can currently be authored in the part JSON; the UI
displays their tree entries but does not yet provide geometry editors or add
buttons for them.

* `Sketch3D.segments` is a list of endpoint pairs, with XYZ coordinates in mm.
  A weldment member consumes those line segments as its path.
* A revolve uses `sketch_name`, a named SolidWorks axis/reference axis in
  `axis_name`, an angle in degrees, and `boss` or `cut`. SolidWorks receives the
  sweep in radians.
* `EdgeModifier.target_edges` contains base64-encoded persistent-reference byte
  arrays obtained from SolidWorks `GetPersistReference3`. Integer edge indexes
  are intentionally not used because they are not stable topological IDs.
  `SolidWorksBuilder.encode_persistent_edge_reference(edge)` encodes a selected
  SolidWorks edge for JSON storage.
* `WeldmentMember.profile_path` must be an existing absolute `.sldlfp` profile
  path. Corner-treatment values follow SolidWorks: 1=Miter, 2=Butt1, 3=Butt2.

The CadQuery preview currently models only 2D sketches and extrudes. It reports
an explicit unsupported-feature error for advanced features rather than
silently displaying an incomplete model; the UI still allows a validated
SolidWorks build, but volume comparison is unchecked when no preview volume is
available. Open/intersecting 3D wire networks are flagged as likely weldment
paths; a profile file and explicit weldment member are still required to build
solid geometry.

SolidWorks translation uses the documented `FeatureRevolve2` API for both
revolve bosses and cuts, `FeatureFillet3`/`InsertFeatureChamfer` for edge
modifiers, and `InsertStructuralWeldment5` with a structural-member group for
weldments. These calls require a running, installed SolidWorks application and
have not been live-tested in this environment.

## Adding a new feature (e.g. Fillet)

1. `core/model.py`   - add a dataclass with `type`, `to_dict`, `from_dict`; register in `FEATURE_TYPES`.
2. `core/validate.py`- add its rules.
3. `backends/preview_cq.py`  - handle it in `build_preview`.
4. `backends/solidworks.py`  - handle it in `build`.
5. `ui/editors.py` and `ui/feature_list.py` - add its editor and tree label / add button.

## Troubleshooting SolidWorks builds

* Every COM failure is logged with the step name, e.g. `FeatureCut4 failed: ... (bad arg index=N)`.
  The full Python traceback is in the SolidWorks log panel.
* After each extrude/cut the SolidWorks volume is compared with the preview. A wrong
  direction is auto-corrected once; anything else is reported as MISMATCH with both volumes.
* "No default part template": SolidWorks > Tools > Options > Default Templates.
