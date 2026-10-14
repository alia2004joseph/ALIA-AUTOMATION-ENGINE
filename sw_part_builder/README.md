# Part Builder

Describe a part as a list of features (sketch, extrude, revolve, weldment, ...),
see supported geometry in 3D, then build the list natively in SolidWorks.

## Run (Windows, SolidWorks 2022 installed)

    python -m venv .venv && .venv\Scripts\activate
    pip install -r requirements.txt        # Python 3.10-3.12 recommended (CadQuery)
    python main.py

Open `examples\enclosure.json` or `examples\l_bracket.json` to try it.
Tests (no SolidWorks needed): `python -m pytest`

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

### PDF diagnostic report

Inspect a PDF before importing it into the sketch editor:

    python main.py --inspect-pdf "drawing.pdf" --output "drawing-report.json"

The JSON report lists page dimensions and rotation, positioned native text,
vector entities and bounding boxes, embedded-image dimensions, content type,
and a reasoned vector-first or raster processing route. PDF geometry is
reported in points from the page's top-left; this report does not infer
millimetre scale or claim to reconstruct a part. To also save page renders,
pass `--render-dir "drawing-rendered" --dpi 300`. The existing UI remains the
default when `main.py` is run without `--inspect-pdf`.

For deterministic native-PDF diagnostics, add `--classify`:

    python main.py --inspect-pdf "drawing.pdf" --classify --output "drawing-report.json"

This adds a page-level `classified_entities` list with source metadata, role
labels (`geometry`, `dimension`, `annotation`, `centerline`, `hidden-line`,
`unknown`), and evidence tags (for example dashed styles or arrow-like
dimension support). Classification is deterministic and offline only: no OCR,
AI vision model, cloud service, or 3D reconstruction is used at this stage.
`parse_blueprint_vectors()` now consumes this normalized classification so
dimension/title-block-like content is less likely to be imported as sketch
geometry, while preserving vector-first parsing and raster safety fallbacks.

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
