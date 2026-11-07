"""Main window: wires the feature tree, editors, 3D preview and the two backends together."""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from tkinter import BooleanVar, TclError, filedialog, messagebox

import customtkinter as ctk

from ..core.model import Circle, Extrude, Part, Polygon, Rect, Sketch
from ..core.validate import has_errors, validate
from .editors import EditorPanel
from .feature_list import FeatureList
from .viewport import Viewport

EXAMPLES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "examples"))
ORANGE, ORANGE_HOVER = "#FF8C00", "#E67E00"


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title("Part Builder")
        self.geometry(f"{min(1380, self.winfo_screenwidth() - 40)}x800")
        self.minsize(1120, 680)

        self.part = Part.default()
        self.selected = 0
        self.result = None
        self.issues = []
        self.busy = False
        self._rebuild_job = None
        self._raster_blueprint_source = None
        self._preview_generation = 0
        self._preview_lock = threading.Lock()

        self._build_layout()
        self.refresh_all()

    # ================================================================ layout
    def _build_layout(self):
        self.grid_columnconfigure(0, minsize=270)
        self.grid_columnconfigure(1, weight=1)
        self.grid_columnconfigure(1, minsize=300)
        self.grid_columnconfigure(2, minsize=320)
        self.grid_rowconfigure(1, weight=1)

        bar = ctk.CTkFrame(self, corner_radius=0, fg_color="#1A1A1A")
        bar.grid(row=0, column=0, columnspan=3, sticky="ew")
        self.part_title = ctk.CTkLabel(bar, text="", font=ctk.CTkFont(size=16, weight="bold"))
        self.part_title.pack(side="left", padx=14, pady=10)
        self.btn_sw_export = ctk.CTkButton(bar, text="Export from SolidWorks...", width=170,
                                           fg_color="#3A3A3A", command=self.export_from_sw)
        self.btn_sw_export.pack(side="right", padx=(4, 12), pady=8)
        self.btn_sw_build = ctk.CTkButton(bar, text="COMPILE TO SOLIDWORKS", width=190, fg_color=ORANGE,
                                          hover_color=ORANGE_HOVER, text_color="black",
                                          font=ctk.CTkFont(weight="bold"), command=self.on_compile_click)
        self.btn_compile = self.btn_sw_build
        self.btn_sw_build.pack(side="right", padx=4, pady=8)
        self.btn_pdf_analysis = ctk.CTkButton(
            bar,
            text="Analyze Drawing...",
            width=140,
            fg_color="#3A3A3A",
            command=self.on_analyze_pdf_click,
        )
        self.btn_pdf_analysis.pack(side="right", padx=4, pady=8)
        ctk.CTkButton(
            bar,
            text="Restart GUI",
            width=100,
            fg_color="#3A3A3A",
            command=self.restart_gui,
        ).pack(side="right", padx=4, pady=8)
        for text, cmd in (("Export STL (preview)...", self.export_preview), ("Save...", self.save_file),
                          ("Open...", self.open_file), ("New", self.new_part)):
            ctk.CTkButton(bar, text=text, width=130 if "STL" in text else 70, fg_color="#3A3A3A",
                          command=cmd).pack(side="right", padx=4, pady=8)

        self.feature_list = FeatureList(self, self.select, self.add_feature, self.delete_selected,
                                        self.move_selected, width=270)
        self.feature_list.grid(row=1, column=0, sticky="nsew", padx=(10, 5), pady=10)
        self.feature_list.grid_propagate(False)

        self.viewport = Viewport(self)
        self.viewport.grid(row=1, column=1, sticky="nsew", padx=5, pady=10)

        self.editor = EditorPanel(
            self, lambda: self.part, self.on_edit, self.on_rename,
            on_import=self.on_import_vector_click,
            width=320,
        )
        self.editor.grid(row=1, column=2, sticky="nsew", padx=(5, 10), pady=10)

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=2, column=0, columnspan=3, sticky="ew", padx=10, pady=(0, 10))
        bottom.grid_columnconfigure((0, 1), weight=1)
        self.issue_box = self._make_box(bottom, "Checks", 0)
        self.log_box = self._make_box(bottom, "SolidWorks log", 1)

    def _make_box(self, parent, title, col):
        frame = ctk.CTkFrame(parent)
        frame.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 5, 5 if col == 0 else 0))
        ctk.CTkLabel(frame, text=title, font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=10, pady=(6, 0))
        box = ctk.CTkTextbox(frame, height=110, wrap="word", state="disabled")
        box.pack(fill="x", padx=8, pady=(2, 8))
        return box

    @staticmethod
    def _set_text(box, text):
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", text)
        box.configure(state="disabled")

    # ================================================================ refresh
    def refresh_all(self):
        self.part_title.configure(text=f"Part: {self.part.name}")
        self.editor.show(self.current_feature())
        self.rebuild()

    def current_feature(self):
        if self.selected is not None and 0 <= self.selected < len(self.part.features):
            return self.part.features[self.selected]
        return None

    def _schedule_rebuild(self):
        if self._rebuild_job is not None:
            self.after_cancel(self._rebuild_job)
        self._rebuild_job = self.after(250, self.rebuild)

    def rebuild(self):
        self._rebuild_job = None
        self.issues = validate(self.part)
        errors = {i.feature for i in self.issues if i.level == "error"}
        self.feature_list.refresh(self.part, self.selected, errors)
        lines = [str(i) for i in self.issues]

        if has_errors(self.issues):
            self._preview_generation += 1
            self.result = None
            self.viewport.show(None, "Fix the errors (red items) to see the preview")
            self._set_text(self.issue_box, "\n".join(lines))
            return

        self._preview_generation += 1
        generation = self._preview_generation
        part_snapshot = Part.from_dict(self.part.to_dict())
        self.result = None
        self.viewport.show(None, "Preparing 3D preview...")
        self._set_text(self.issue_box, "\n".join(lines) or "Building 3D preview...")

        def worker():
            try:
                with self._preview_lock:
                    if generation != self._preview_generation:
                        return
                    from ..backends import preview_cq
                    from .viewport import load_vtk_backend

                    result = preview_cq.build_preview(part_snapshot)
                    load_vtk_backend()
                    error = None
            except Exception as exc:
                result = None
                error = exc
            try:
                self.after(
                    0,
                    lambda: self._preview_finished(
                        generation, result, error, lines
                    ),
                )
            except (RuntimeError, TclError):
                return

        try:
            threading.Thread(target=worker, daemon=True).start()
        except RuntimeError as exc:
            self.result = None
            lines.append(f"ERROR preview worker: {exc}")
            self.viewport.show(None, "Preview could not be started - see Checks")
            self._set_text(self.issue_box, "\n".join(lines))

    def _preview_finished(self, generation, result, error, validation_lines):
        if generation != self._preview_generation:
            return
        lines = list(validation_lines)
        if error is not None:
            self.result = None
            lines.append(f"ERROR preview: {error}")
            self.viewport.show(None, "Preview failed - see Checks")
        else:
            self.result = result
            lines += [f"warn  {warning}" for warning in result.warnings]
            self.viewport.show(result, "Nothing to show yet")
        self._set_text(self.issue_box, "\n".join(lines) if lines else "All checks passed.")

    # ================================================================ tree actions
    def select(self, index):
        self.selected = index
        self.refresh_all()

    def add_feature(self, kind):
        part = self.part
        if kind == "sketch":
            feature = Sketch(name=part.unique_name("Sketch"), entities=[Rect(width=40, height=30)])
        else:
            sketches = [f for f in part.features if isinstance(f, Sketch)]
            feature = Extrude(
                name=part.unique_name("Extrude" if kind == "boss" else "Cut"),
                sketch=sketches[-1].name if sketches else "",
                operation=kind,
                depth=10.0,
            )
        pos = len(part.features) if self.selected is None else self.selected + 1
        part.features.insert(pos, feature)
        self.selected = pos
        self.refresh_all()

    def delete_selected(self):
        feature = self.current_feature()
        if feature is None:
            return
        self.part.features.remove(feature)
        self.selected = min(self.selected, len(self.part.features) - 1) if self.part.features else None
        self.refresh_all()

    def move_selected(self, delta):
        i, j = self.selected, (self.selected or 0) + delta
        if i is None or not (0 <= j < len(self.part.features)):
            return
        feats = self.part.features
        feats[i], feats[j] = feats[j], feats[i]
        self.selected = j
        self.refresh_all()

    def on_edit(self):
        self._schedule_rebuild()

    def on_rename(self, feature, new_name) -> bool:
        if not self.part.rename_feature(feature, new_name):
            return False
        self._schedule_rebuild()
        return True

    # ================================================================ files
    def restart_gui(self):
        if not messagebox.askyesno(
            "Restart Part Builder",
            "Restart the application to load code changes?\n\n"
            "Any unsaved part changes will be lost.",
            parent=self,
        ):
            return

        script_path = os.path.abspath(sys.argv[0])
        command = [sys.executable, script_path, *sys.argv[1:]]
        try:
            subprocess.Popen(command, cwd=os.getcwd())
        except OSError as exc:
            messagebox.showerror(
                "Restart failed",
                f"Could not restart the Part Builder:\n{exc}",
                parent=self,
            )
            return
        self.destroy()

    def on_analyze_pdf_click(self):
        file_path = filedialog.askopenfilename(
            filetypes=[
                ("Engineering drawings", "*.pdf *.dxf *.png *.jpg *.jpeg *.tif *.tiff *.bmp"),
                ("PDF drawings", "*.pdf"),
                ("DXF drawings", "*.dxf"),
                ("Raster images", "*.png *.jpg *.jpeg *.tif *.tiff *.bmp"),
            ],
        )
        if not file_path:
            return

        pdf_path = Path(file_path)
        output_dir = Path(tempfile.mkdtemp(prefix="part-builder-drawing-analysis-"))
        report_path = output_dir / "report.json"
        interpretation_path = output_dir / "drawing-interpretation.json"
        self.btn_pdf_analysis.configure(state="disabled")
        self.safe_log_to_sw_panel(
            f"[DRAWING ANALYSIS] Processing {pdf_path.name}; model output will be archived centrally."
        )

        def worker():
            try:
                from ..core.drawing_interpreter import interpret_drawing
                from ..core.drawing_sources import inspect_drawing_source

                output_dir.mkdir(parents=True, exist_ok=True)
                report = inspect_drawing_source(
                    pdf_path,
                    dpi=300,
                    output_dir=output_dir,
                    run_ocr=True,
                )
                interpretation = interpret_drawing(report)
                checkpoint = (
                    Path(__file__).resolve().parents[2]
                    / "data"
                    / "training"
                    / "view_detection"
                    / "models"
                    / "view_detector.pt"
                )
                if checkpoint.is_file():
                    from ..core.view_detector import evaluate_report

                    report["view_detector_evaluation"] = evaluate_report(
                        report,
                        checkpoint,
                        Path(__file__).resolve().parents[2]
                        / "data"
                        / "training"
                        / "view_detection"
                        / "images"
                        / "model-predictions",
                        output_prefix=pdf_path.stem,
                    )
                else:
                    report["view_detector_evaluation"] = {
                        "status": "checkpoint_missing",
                        "checkpoint": str(checkpoint),
                        "prediction_count": 0,
                        "pages": [],
                    }
                report_path.write_text(
                    json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                interpretation_path.write_text(
                    json.dumps(interpretation, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
            except Exception as exc:
                self.after(
                    0,
                    lambda error=exc: self._pdf_analysis_finished(
                        pdf_path, output_dir, None, None, error
                    ),
                )
            else:
                self.after(
                    0,
                    lambda: self._pdf_analysis_finished(
                        pdf_path, output_dir, report, interpretation, None
                    ),
                )

        try:
            threading.Thread(target=worker, daemon=True).start()
        except RuntimeError as exc:
            self.btn_pdf_analysis.configure(state="normal")
            messagebox.showerror("PDF analysis", f"Could not start analysis:\n{exc}")

    def _pdf_analysis_finished(self, pdf_path, output_dir, report, interpretation, error):
        self.btn_pdf_analysis.configure(state="normal")
        if error is not None:
            shutil.rmtree(output_dir, ignore_errors=True)
            self.safe_log_to_sw_panel(f"[DRAWING ANALYSIS] Failed: {error}")
            messagebox.showerror("Drawing analysis failed", str(error))
            return

        self.safe_log_to_sw_panel(
            f"[DRAWING ANALYSIS] Complete: {report['page_count']} page(s), "
            f"{interpretation['summary']['dimension_candidate_count']} dimension candidate(s), "
            f"{interpretation['summary']['symbol_annotation_count']} symbol annotation(s). "
            f"Offline OCR: {report.get('offline_ocr', {}).get('status', 'not reported')}. "
            f"{report.get('view_detector_evaluation', {}).get('prediction_count', 0)} "
            f"model prediction(s) archived centrally. "
            f"Temporary analysis files will be removed when the preview closes."
        )
        self._show_pdf_analysis(pdf_path, output_dir, report, interpretation)

    def _show_pdf_analysis(self, pdf_path, output_dir, report, interpretation):
        window = ctk.CTkToplevel(self)
        def close_analysis_window():
            window.destroy()
            shutil.rmtree(output_dir, ignore_errors=True)

        window.protocol("WM_DELETE_WINDOW", close_analysis_window)
        window.title(f"Drawing analysis: {pdf_path.name}")
        window.geometry("1120x820")
        window.minsize(720, 560)
        window.grid_columnconfigure(0, weight=1)
        window.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            window,
            text=f"Drawing analysis: {pdf_path.name}",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 4))

        page_values = [f"Page {page['page_number']}" for page in report["pages"]]
        selected_page = ctk.StringVar(value=page_values[0])
        has_drawing_layout = any(
            page.get("raster_analysis", {}).get("drawing_views_png")
            and Path(page["raster_analysis"]["drawing_views_png"]).is_file()
            for page in report["pages"]
        )
        has_any_overlay = any(
            (
                page.get("raster_analysis", {}).get("full_page_overlay_png")
                or page.get("raster_analysis", {}).get("overlay_png")
            )
            and Path(
                page.get("raster_analysis", {}).get("full_page_overlay_png")
                or page["raster_analysis"]["overlay_png"]
            ).is_file()
            for page in report["pages"]
        )
        has_model_overlay = any(
            page.get("raster_analysis", {}).get("view_detector_overlay_png")
            and Path(page["raster_analysis"]["view_detector_overlay_png"]).is_file()
            for page in report["pages"]
        )
        selected_image = ctk.StringVar(
            value=(
                "Drawing views (source layout)" if has_drawing_layout
                else "Model predictions" if has_model_overlay
                else "Analysis overlay" if has_any_overlay
                else "Full page"
            )
        )
        current_image_path: list[Path | None] = [None]

        controls = ctk.CTkFrame(window, fg_color="transparent")
        controls.grid(row=1, column=0, sticky="ew", padx=12, pady=4)
        controls.grid_columnconfigure(1, weight=1)
        image_mode = ctk.CTkSegmentedButton(
            controls,
            values=[
                "Drawing views (source layout)",
                "Model predictions",
                "Analysis overlay",
                "Full page",
            ],
            variable=selected_image,
            command=lambda _value: show_page(),
        )
        image_mode.grid(row=0, column=0, sticky="w")
        if len(page_values) > 1:
            ctk.CTkOptionMenu(
                controls,
                values=page_values,
                variable=selected_page,
                command=lambda _value: show_page(),
            ).grid(row=0, column=1, sticky="e", padx=8)
        else:
            ctk.CTkLabel(controls, text=page_values[0], text_color="#9A9A9A").grid(
                row=0, column=1, sticky="e", padx=8
            )

        def open_current_png():
            image_path = current_image_path[0]
            if image_path is None or not image_path.is_file():
                messagebox.showerror(
                    "PNG preview",
                    "The selected PNG file is no longer available.",
                    parent=window,
                )
                return
            try:
                os.startfile(str(image_path))
            except OSError as exc:
                messagebox.showerror(
                    "Could not open PNG",
                    f"Could not open {image_path}:\n{exc}",
                    parent=window,
                )

        open_png_button = ctk.CTkButton(
            controls,
            text="Open PNG",
            width=100,
            command=open_current_png,
        )
        open_png_button.grid(row=0, column=2, sticky="e")
        review_button = ctk.CTkButton(
            controls,
            text="Review view inventory",
            width=160,
            command=lambda: self._show_view_inventory_review(
                window,
                int(selected_page.get().split()[-1]),
                interpretation,
                output_dir / "drawing-interpretation.json",
                self._view_review_image_path(
                    report["pages"][int(selected_page.get().split()[-1]) - 1],
                    output_dir,
                ),
            ),
        )
        review_button.grid(row=0, column=3, sticky="e", padx=(8, 0))
        raster_views = [
            view
            for page in report["pages"]
            for view in page.get("raster_analysis", {}).get("view_regions", [])
            if view.get("classification") == "orthographic"
        ]
        reconstruct_button = ctk.CTkButton(
            controls,
            text="Reconstruct 2D sketch...",
            width=175,
            command=lambda: self._show_sketch_reconstruction(
                window,
                report["pages"][int(selected_page.get().split()[-1]) - 1],
                next(
                    (
                        page for page in interpretation.get("pages", [])
                        if page.get("page_number")
                        == int(selected_page.get().split()[-1])
                    ),
                    None,
                ),
            ),
            state="normal" if raster_views else "disabled",
        )
        reconstruct_button.grid(row=0, column=4, sticky="e", padx=(8, 0))

        image_frame = ctk.CTkFrame(window)
        image_frame.grid(row=2, column=0, sticky="nsew", padx=12, pady=8)
        image_frame.grid_columnconfigure(0, weight=1)
        image_frame.grid_rowconfigure(0, weight=1)
        image_label = ctk.CTkLabel(image_frame, text="Loading PNG preview...")
        image_label.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        source_preview = [None]

        def resize_preview(_event=None):
            source = source_preview[0]
            if source is None:
                return
            available_width = max(100, image_frame.winfo_width() - 20)
            available_height = max(100, image_frame.winfo_height() - 20)
            preview = source.copy()
            preview.thumbnail((available_width, available_height))
            window.preview_image = ctk.CTkImage(
                light_image=preview,
                dark_image=preview,
                size=preview.size,
            )
            image_label.configure(image=window.preview_image, text="")

        image_frame.bind("<Configure>", resize_preview)
        output_label = ctk.CTkLabel(
            window,
            text="",
            text_color="#9A9A9A",
            wraplength=1080,
        )
        output_label.grid(row=3, column=0, sticky="w", padx=12, pady=(0, 12))

        def show_page():
            page_number = int(selected_page.get().split()[-1])
            page = report["pages"][page_number - 1]
            raster = page.get("raster_analysis", {})
            overlay = raster.get("full_page_overlay_png") or raster.get("overlay_png")
            model_overlay = raster.get("view_detector_overlay_png")
            drawing_layout = raster.get("drawing_views_png")
            rendered = page.get("render", {}).get("file")
            has_drawing_layout = bool(
                drawing_layout and Path(drawing_layout).is_file()
            )
            has_overlay = bool(overlay and Path(overlay).is_file())
            has_model_overlay = bool(model_overlay and Path(model_overlay).is_file())
            has_rendered = bool(rendered and (output_dir / rendered).is_file())
            has_preview = (
                has_drawing_layout
                or has_model_overlay
                or has_overlay
                or has_rendered
            )
            image_mode.configure(state="normal")
            if (
                not has_drawing_layout
                and selected_image.get() == "Drawing views (source layout)"
            ):
                selected_image.set(
                    "Model predictions" if has_model_overlay
                    else "Analysis overlay" if has_overlay
                    else "Full page"
                )
            elif not has_model_overlay and selected_image.get() == "Model predictions":
                selected_image.set("Analysis overlay" if has_overlay else "Full page")
            elif not has_overlay and selected_image.get() == "Analysis overlay":
                selected_image.set("Full page")
            if (
                selected_image.get() == "Drawing views (source layout)"
                and has_drawing_layout
            ):
                image_path = Path(drawing_layout)
            elif selected_image.get() == "Model predictions" and has_model_overlay:
                image_path = Path(model_overlay)
            elif selected_image.get() == "Analysis overlay" and has_overlay:
                image_path = Path(overlay)
            else:
                image_path = output_dir / rendered if rendered else None
            current_image_path[0] = image_path
            image_mode.configure(state="normal" if has_preview else "disabled")
            open_png_button.configure(
                state="normal" if image_path is not None and image_path.is_file() else "disabled"
            )
            reviewed_page = next(
                (
                    item for item in interpretation.get("pages", [])
                    if item.get("page_number") == page_number
                ),
                {},
            )
            from ..core.sketch_reconstructor import (
                orthographic_reconstruction_candidates,
            )

            has_orthographic_candidate = bool(
                orthographic_reconstruction_candidates(raster, reviewed_page)
            )
            reconstruct_button.configure(
                state=(
                    "normal"
                    if has_orthographic_candidate
                    and drawing_layout
                    and Path(drawing_layout).is_file()
                    else "disabled"
                )
            )
            if image_path is not None and image_path.is_file():
                from PIL import Image

                with Image.open(image_path) as source:
                    source_preview[0] = source.convert("RGB")
                resize_preview()
                window.after_idle(resize_preview)
            else:
                source_preview[0] = None
                window.preview_image = None
                image_label.configure(
                    image=None,
                    text=(
                        "No PNG preview is available for this vector drawing. "
                        "See report.json and drawing-interpretation.json."
                        if not has_preview
                        else "No PNG image was generated for this page."
                    ),
                )

            output_label.configure(
                text=f"PNG: {image_path}" if image_path is not None else f"Output folder: {output_dir}"
            )

        show_page()

    def _show_sketch_reconstruction(
        self,
        parent,
        report_page,
        interpretation_page=None,
    ):
        from ..core.sketch_reconstructor import (
            orthographic_reconstruction_candidates,
        )

        raster = report_page.get("raster_analysis", {})
        regions = orthographic_reconstruction_candidates(
            raster,
            interpretation_page,
        )
        image_path = raster.get("drawing_views_png")
        if not regions or not image_path or not Path(image_path).is_file():
            messagebox.showinfo(
                "2D sketch reconstruction",
                "This page has no usable orthographic raster-view candidate. "
                "For this first version, use a raster drawing with a clean, "
                "single outer profile.",
                parent=parent,
            )
            return

        try:
            import numpy as np
            from PIL import Image

            with Image.open(image_path) as source:
                source_image = np.asarray(source.convert("RGB"))
        except (ImportError, OSError) as exc:
            messagebox.showerror(
                "2D sketch reconstruction",
                f"Could not load the source drawing image:\n{exc}",
                parent=parent,
            )
            return

        choices = {
            f"{region['view_id']} - orthographic candidate "
            f"(heuristic score {region.get('heuristic_score', 'unknown')})":
                region["view_id"]
            for region in regions
        }
        selected_view = ctk.StringVar(value=next(iter(choices)))
        known_width = ctk.StringVar()
        reconstruction = [None]

        window = ctk.CTkToplevel(parent)
        source_confirmed = BooleanVar(master=window, value=False)
        window.title("Reconstruct a reviewed 2D sketch")
        window.geometry("900x760")
        window.minsize(720, 580)
        window.grid_columnconfigure(0, weight=1)
        window.grid_rowconfigure(2, weight=1)
        ctk.CTkLabel(
            window,
            text=(
                "This first version traces only one clean outer profile from a "
                "candidate orthographic raster view. It does not read drawing "
                "dimensions. Enter a width you have verified from the drawing; "
                "holes, multiple outlines, and uncertain contours are rejected."
            ),
            wraplength=860,
            justify="left",
        ).grid(row=0, column=0, sticky="ew", padx=12, pady=10)

        controls = ctk.CTkFrame(window)
        controls.grid(row=1, column=0, sticky="ew", padx=12, pady=6)
        controls.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(controls, text="Source view").grid(
            row=0, column=0, sticky="w", padx=8, pady=8
        )
        selected_view_option = ctk.CTkOptionMenu(
            controls,
            values=list(choices),
            variable=selected_view,
            width=300,
        )
        selected_view_option.grid(row=0, column=1, sticky="w", padx=8, pady=8)
        ctk.CTkLabel(
            controls,
            text="Verified overall width (mm)",
        ).grid(row=1, column=0, sticky="w", padx=8, pady=8)
        ctk.CTkEntry(
            controls,
            textvariable=known_width,
            placeholder_text="Enter a dimension you verified",
            width=220,
        ).grid(row=1, column=1, sticky="w", padx=8, pady=8)
        ctk.CTkCheckBox(
            controls,
            text="I confirmed this is an orthographic view suitable for a 2D profile.",
            variable=source_confirmed,
            command=lambda: reset_preview(),
        ).grid(row=2, column=0, columnspan=3, sticky="w", padx=8, pady=8)

        preview_frame = ctk.CTkFrame(window)
        preview_frame.grid(row=2, column=0, sticky="nsew", padx=12, pady=6)
        preview_frame.grid_columnconfigure(0, weight=1)
        preview_frame.grid_rowconfigure(0, weight=1)
        preview_label = ctk.CTkLabel(
            preview_frame,
            text=(
                "Choose a view and enter its verified overall width, then preview "
                "the detected outer contour."
            ),
            wraplength=820,
        )
        preview_label.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        status_label = ctk.CTkLabel(
            window,
            text="No sketch has been added to the part.",
            anchor="w",
            wraplength=860,
        )
        status_label.grid(row=3, column=0, sticky="ew", padx=12, pady=6)

        def preview_profile():
            from ..core.sketch_reconstructor import (
                SketchReconstructionError,
                reconstruct_raster_profile,
            )

            if not source_confirmed.get():
                reconstruction[0] = None
                add_button.configure(state="disabled")
                status_label.configure(
                    text="Confirm that the selected candidate is an orthographic "
                    "view before previewing a sketch."
                )
                return
            try:
                result = reconstruct_raster_profile(
                    source_image,
                    raster,
                    choices[selected_view.get()],
                    known_width.get(),
                )
            except SketchReconstructionError as exc:
                reconstruction[0] = None
                add_button.configure(state="disabled")
                window.profile_preview = None
                preview_label.configure(
                    image="",
                    text="No valid contour preview is available.",
                )
                status_label.configure(text=f"Not reconstructed: {exc}")
                return

            reconstruction[0] = result
            preview = Image.fromarray(result["preview_rgb"])
            preview.thumbnail((760, 500))
            window.profile_preview = ctk.CTkImage(
                light_image=preview,
                dark_image=preview,
                size=preview.size,
            )
            preview_label.configure(image=window.profile_preview, text="")
            if result["profile_type"] == "polygon":
                profile_detail = f"{result['vertex_count']} polygon vertices"
            else:
                profile_detail = f"{result['profile_type']} profile"
            status_label.configure(
                text=(
                    f"Preview only: {profile_detail}, "
                    f"{result['width_mm']:g} mm wide × "
                    f"{result['height_mm']:g} mm high. Scale uses the width "
                    "you entered. Drawing dimensions and internal features "
                    "were not extracted. Inspect the green outline before adding."
                )
            )
            add_button.configure(state="normal")

        ctk.CTkButton(
            controls,
            text="Preview outer profile",
            command=preview_profile,
        ).grid(row=1, column=2, sticky="e", padx=8, pady=8)

        def reset_preview(*_args):
            reconstruction[0] = None
            add_button.configure(state="disabled")
            status_label.configure(text="No sketch has been added to the part.")
            preview_label.configure(
                image="",
                text=(
                    "Confirm the view and enter its verified overall width, then "
                    "preview the detected outer contour."
                ),
            )
            window.profile_preview = None

        selected_view_option.configure(command=reset_preview)
        known_width.trace_add("write", reset_preview)

        def add_sketch():
            result = reconstruction[0]
            if result is None:
                return
            shape = result["shape"]
            if shape["type"] == "circle":
                entities = [Circle(
                    cx=shape["cx"],
                    cy=shape["cy"],
                    diameter=shape["diameter"],
                )]
            else:
                entities = [Polygon(
                    points=[tuple(point) for point in shape["points"]],
                )]
            sketch = Sketch(
                name=self.part.unique_name("DrawingSketch"),
                plane="Front",
                entities=entities,
            )
            self.part.features.append(sketch)
            self.selected = len(self.part.features) - 1
            self.refresh_all()
            self.safe_log_to_sw_panel(
                f"[DRAWING RECONSTRUCTION] Added reviewed {result['profile_type']} "
                f"outer profile as '{sketch.name}' at {result['width_mm']:g} mm width. "
                "No dimensions or internal features were inferred."
            )
            status_label.configure(
                text=(
                    f"Added '{sketch.name}' to the current Part. The traced profile "
                    "remains visible here for comparison. The main 3D viewport will "
                    "show a solid only after you add a modeling feature and confirm "
                    "its depth."
                )
            )
            add_button.configure(state="disabled", text="Sketch added")
            close_button.configure(text="Close")

        footer = ctk.CTkFrame(window, fg_color="transparent")
        footer.grid(row=4, column=0, sticky="e", padx=12, pady=10)
        add_button = ctk.CTkButton(
            footer,
            text="Add reviewed sketch to Part",
            command=add_sketch,
            state="disabled",
        )
        add_button.pack(side="right", padx=(8, 0))
        close_button = ctk.CTkButton(
            footer,
            text="Cancel",
            command=window.destroy,
        )
        close_button.pack(side="right")

    @staticmethod
    def _view_review_image_path(page, output_dir):
        raster = page.get("raster_analysis", {})
        candidates = [
            raster.get("full_page_overlay_png"),
            raster.get("overlay_png"),
            raster.get("drawing_views_png"),
        ]
        rendered = page.get("render", {}).get("file")
        if rendered:
            candidates.append(output_dir / rendered)
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return Path(candidate)
        return None

    def _show_view_inventory_review(
        self,
        parent,
        page_number,
        interpretation,
        interpretation_path,
        reference_image_path=None,
    ):
        page = next(
            (
                item for item in interpretation.get("pages", [])
                if item.get("page_number") == page_number
            ),
            None,
        )
        if page is None:
            messagebox.showerror(
                "View inventory",
                f"No interpretation data is available for page {page_number}.",
                parent=parent,
            )
            return

        review_window = ctk.CTkToplevel(parent)
        review_window.title(f"Review view inventory - page {page_number}")
        review_window.geometry("1180x760")
        review_window.minsize(850, 560)
        review_window.grid_columnconfigure(0, weight=1)
        review_window.grid_columnconfigure(1, weight=2)
        review_window.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            review_window,
            text=(
                "Compare each candidate with the drawing before choosing. "
                "The automatic suggestions are uncertain: the largest view is "
                "not necessarily the front view. Choose Unknown whenever unsure."
            ),
            wraplength=1120,
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=10)

        image_frame = ctk.CTkFrame(review_window)
        image_frame.grid(row=1, column=0, sticky="nsew", padx=(12, 6), pady=6)
        image_frame.grid_columnconfigure(0, weight=1)
        image_frame.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            image_frame,
            text="Drawing reference - match the view IDs",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=8, pady=8)
        reference_label = ctk.CTkLabel(
            image_frame,
            text="Drawing image unavailable",
            wraplength=390,
        )
        reference_label.grid(row=1, column=0, sticky="nsew", padx=8, pady=8)
        if reference_image_path is not None and Path(reference_image_path).is_file():
            try:
                from PIL import Image

                with Image.open(reference_image_path) as source:
                    preview = source.copy()
                preview.thumbnail((460, 640))
                review_window.reference_image = ctk.CTkImage(
                    light_image=preview,
                    dark_image=preview,
                    size=preview.size,
                )
                reference_label.configure(
                    image=review_window.reference_image,
                    text="",
                )
            except OSError as exc:
                reference_label.configure(text=f"Could not load drawing image:\n{exc}")

        scroll = ctk.CTkScrollableFrame(review_window)
        scroll.grid(row=1, column=1, sticky="nsew", padx=(6, 12), pady=6)
        scroll.grid_columnconfigure(0, weight=1)
        view_type_options = {
            "Not reviewed": None,
            "Orthographic": "orthographic",
            "Isometric": "isometric_reference",
            "Section": "section",
            "Detail": "detail",
            "Auxiliary": "auxiliary",
            "Unknown": "unknown",
        }
        role_options = {
            "Not reviewed": None,
            "Front (principal)": "front",
            "Top": "top",
            "Bottom": "bottom",
            "Left side": "left_side",
            "Right side": "right_side",
            "Rear": "rear",
            "Parent (source view)": "parent",
            "Derived (section/detail/etc.)": "derived",
            "Unknown (not sure)": "unknown",
            "Not applicable": "not_applicable",
        }
        relationship_options = {
            "Not reviewed": None,
            "Confirm": "accepted",
            "Reject": "rejected",
        }
        view_list = page.get("views", [])
        view_rows = []
        type_reverse = {value: label for label, value in view_type_options.items() if value}
        role_reverse = {value: label for label, value in role_options.items() if value}
        candidate_role_labels = {
            "front_view_candidate": "Front (principal)",
            "top_view_candidate": "Top",
            "bottom_view_candidate": "Bottom",
            "left_side_view_candidate": "Left side",
            "right_side_view_candidate": "Right side",
            "rear_view_candidate": "Rear",
        }
        view_labels = {
            view.get("view_id"): f"View {index} ({view.get('view_id', 'unknown')})"
            for index, view in enumerate(view_list, start=1)
        }

        def relative_location(view):
            bbox = view.get("bbox_pt") or view.get("bbox_px")
            if not isinstance(bbox, dict):
                return "location unknown"
            x = float(bbox.get("x0", bbox.get("x", 0)))
            y = float(bbox.get("top", bbox.get("y", 0)))
            width = float(bbox.get("x1", x + float(bbox.get("width", 0)))) - x
            height = float(bbox.get("bottom", y + float(bbox.get("height", 0)))) - y
            centers = []
            for candidate in view_list:
                candidate_box = candidate.get("bbox_pt") or candidate.get("bbox_px") or {}
                candidate_x = float(candidate_box.get("x0", candidate_box.get("x", 0)))
                candidate_y = float(candidate_box.get("top", candidate_box.get("y", 0)))
                candidate_width = float(
                    candidate_box.get(
                        "x1", candidate_x + float(candidate_box.get("width", 0))
                    )
                ) - candidate_x
                candidate_height = float(
                    candidate_box.get(
                        "bottom", candidate_y + float(candidate_box.get("height", 0))
                    )
                ) - candidate_y
                centers.append((
                    candidate_x + candidate_width / 2,
                    candidate_y + candidate_height / 2,
                ))
            center_x, center_y = x + width / 2, y + height / 2

            def relative_axis(value, values, low_label, high_label):
                low, high = min(values), max(values)
                if high - low <= 1:
                    return "middle"
                position = (value - low) / (high - low)
                if position < 1 / 3:
                    return low_label
                if position > 2 / 3:
                    return high_label
                return "middle"

            horizontal = relative_axis(
                center_x, [center[0] for center in centers], "left", "right"
            )
            vertical = relative_axis(
                center_y, [center[1] for center in centers], "upper", "lower"
            )
            if len(view_list) == 1:
                return "only detected region"
            return f"{vertical}-{horizontal} among detected regions"

        ctk.CTkLabel(
            scroll,
            text="1. Classify each view",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))
        for index, view in enumerate(view_list, start=1):
            row = ctk.CTkFrame(scroll)
            row.grid(row=index, column=0, sticky="ew", padx=4, pady=4)
            row.grid_columnconfigure(0, weight=1)
            suggested_role = view.get("role_candidate", {}).get("value")
            if suggested_role == "layout_reference_candidate":
                role_description = (
                    "Layout anchor only (largest detected view; not automatically front)"
                )
            elif suggested_role and suggested_role != "unknown_view_candidate":
                friendly_role = candidate_role_labels.get(
                    suggested_role,
                    role_reverse.get(suggested_role, suggested_role),
                )
                role_description = (
                    f"Possible {friendly_role} "
                    "(unconfirmed)"
                )
            else:
                role_description = "No reliable role suggested"
            detected_type = view.get("view_type", "unknown")
            if detected_type == "isometric_reference":
                detected_type = "isometric"
            ctk.CTkLabel(
                row,
                text=(
                    f"{view_labels.get(view.get('view_id'), 'View')}\n"
                    f"Location: {relative_location(view)}\n"
                    f"Automatic type guess: {detected_type}\n"
                    f"Automatic role guess: {role_description}"
                ),
                anchor="w",
                justify="left",
                wraplength=600,
            ).grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=(6, 2))
            ctk.CTkLabel(row, text="Your view type").grid(
                row=1, column=0, sticky="w", padx=8, pady=(2, 6)
            )
            ctk.CTkLabel(row, text="Your role (orientation or relation)").grid(
                row=1, column=1, sticky="w", padx=8, pady=(2, 6)
            )
            prior_review = view.get("review", {})
            type_value = ctk.StringVar(
                value=type_reverse.get(prior_review.get("view_type"), "Not reviewed")
            )
            role_value = ctk.StringVar(
                value=role_reverse.get(prior_review.get("role"), "Not reviewed")
            )
            ctk.CTkOptionMenu(
                row,
                values=list(view_type_options),
                variable=type_value,
                width=190,
            ).grid(row=2, column=0, sticky="w", padx=8, pady=(0, 8))
            ctk.CTkOptionMenu(
                row,
                values=list(role_options),
                variable=role_value,
                width=190,
            ).grid(row=2, column=1, sticky="w", padx=8, pady=(0, 8))
            view_rows.append((view.get("view_id"), type_value, role_value))

        relation_start = len(view_rows) + 1
        ctk.CTkLabel(
            scroll,
            text="2. Check suggested view links",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=relation_start, column=0, sticky="w", padx=8, pady=(14, 4))
        relationship_rows = []
        for index, relation in enumerate(
            page.get("view_relationships", []),
            start=relation_start + 1,
        ):
            row = ctk.CTkFrame(scroll)
            row.grid(row=index, column=0, sticky="ew", padx=4, pady=4)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                row,
                text=(
                    f"{view_labels.get(relation.get('source_view_id'), 'View')} and "
                    f"{view_labels.get(relation.get('target_view_id'), 'View')}: "
                    f"the second is {relation.get('relative_position', 'nearby')} the first"
                    + (
                        f" and their boxes align {relation.get('alignment_axis')}"
                        if relation.get("alignment_axis")
                        else "; alignment was not established"
                    )
                    + ". Box alignment does not prove these views show the same part."
                ),
                anchor="w",
                wraplength=600,
                justify="left",
            ).grid(row=0, column=0, sticky="ew", padx=8, pady=6)
            prior_review = relation.get("review", {}).get("status")
            relationship_value = ctk.StringVar(
                value=(
                    "Confirm" if prior_review == "accepted"
                    else "Reject" if prior_review == "rejected"
                    else "Not reviewed"
                )
            )
            ctk.CTkOptionMenu(
                row,
                values=list(relationship_options),
                variable=relationship_value,
                width=160,
            ).grid(row=0, column=1, sticky="e", padx=8, pady=6)
            relationship_rows.append((relation.get("relationship_id"), relationship_value))

        def save_review():
            role_selections = {}
            for view_id, _type_value, role_value in view_rows:
                role = role_options[role_value.get()]
                if role in {"front", "top", "bottom", "left_side", "right_side", "rear"}:
                    role_selections.setdefault(role, []).append(view_id)
            duplicates = {
                role: view_ids
                for role, view_ids in role_selections.items()
                if len(view_ids) > 1
            }
            if duplicates:
                repeated = "; ".join(
                    f"{role_reverse[role]}: "
                    + ", ".join(view_labels.get(view_id, "View") for view_id in view_ids)
                    for role, view_ids in duplicates.items()
                )
                if not messagebox.askyesno(
                    "Repeated view roles",
                    "More than one view has the same orientation:\n\n"
                    f"{repeated}\n\n"
                    "This can be valid for separate parts, but may be a mistake. "
                    "Save these choices anyway?",
                    parent=review_window,
                ):
                    return

            updated = copy.deepcopy(interpretation)
            updated_page = next(
                item for item in updated["pages"]
                if item.get("page_number") == page_number
            )
            views_by_id = {
                view.get("view_id"): view
                for view in updated_page.get("views", [])
            }
            for view_id, type_value, role_value in view_rows:
                reviewed_type = view_type_options[type_value.get()]
                reviewed_role = role_options[role_value.get()]
                if reviewed_type is None and reviewed_role is None:
                    continue
                view = views_by_id.get(view_id)
                if view is not None:
                    reviewed = reviewed_type is not None and reviewed_role is not None
                    view["review"] = {
                        "source": "human_review",
                        "status": "reviewed" if reviewed else "partially_reviewed",
                        "view_type": reviewed_type,
                        "view_type_status": (
                            "reviewed" if reviewed_type is not None else "not_reviewed"
                        ),
                        "role": reviewed_role,
                        "role_status": (
                            "reviewed" if reviewed_role is not None else "not_reviewed"
                        ),
                    }

            relationships_by_id = {
                relation.get("relationship_id"): relation
                for relation in updated_page.get("view_relationships", [])
            }
            for relation_id, relationship_value in relationship_rows:
                status = relationship_options[relationship_value.get()]
                if status is None:
                    continue
                relation = relationships_by_id.get(relation_id)
                if relation is not None:
                    relation["review"] = {
                        "source": "human_review",
                        "status": status,
                    }

            temporary_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    newline="\n",
                    dir=interpretation_path.parent,
                    prefix=f".{interpretation_path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary_file:
                    temporary_path = Path(temporary_file.name)
                    json.dump(updated, temporary_file, indent=2, ensure_ascii=False)
                    temporary_file.write("\n")
                os.replace(temporary_path, interpretation_path)
            except OSError as exc:
                if temporary_path is not None:
                    try:
                        temporary_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                messagebox.showerror(
                    "Could not save view review",
                    f"Could not update {interpretation_path}:\n{exc}",
                    parent=review_window,
                )
                return

            interpretation.clear()
            interpretation.update(updated)
            self.safe_log_to_sw_panel(
                f"[DRAWING ANALYSIS] View review saved to {interpretation_path}."
            )
            review_window.destroy()

        ctk.CTkButton(
            review_window,
            text="Save review",
            command=save_review,
        ).grid(row=2, column=0, columnspan=2, sticky="e", padx=12, pady=10)

    def on_import_vector_click(self):
        file_path = filedialog.askopenfilename(
            filetypes=[
                ("Vector Drawings", "*.pdf *.dxf"),
                ("PDF drawings", "*.pdf"),
                ("DXF drawings", "*.dxf"),
            ],
        )
        if not file_path:
            return

        self.safe_log_to_sw_panel(f"[INGESTION]: Loading blueprint file: {file_path}...")
        if os.path.splitext(file_path)[1].lower() == ".dxf":
            from ..core.dxf_dimensions import (
                analyze_dxf_drawing,
                format_dxf_analysis,
            )

            try:
                dxf_report = analyze_dxf_drawing(file_path)
            except Exception as exc:
                self.safe_log_to_sw_panel(f"[INGESTION] ERROR: Could not inspect DXF: {exc}")
                return
            if dxf_report["entity_counts"].get("DIMENSION", 0):
                for line in format_dxf_analysis(dxf_report):
                    self.safe_log_to_sw_panel(line)
                from ..core.dxf_modeler import (
                    PreliminaryModelError,
                    build_preliminary_dxf_part,
                )

                try:
                    preliminary = build_preliminary_dxf_part(file_path)
                except PreliminaryModelError as exc:
                    self.safe_log_to_sw_panel(
                        f"[DXF] Preliminary model unavailable: {exc}"
                    )
                    return
                except Exception as exc:
                    self.safe_log_to_sw_panel(
                        f"[DXF] ERROR: Preliminary model generation failed: {exc}"
                    )
                    return

                confirmed = messagebox.askyesno(
                    "Preliminary DXF model",
                    f"Create a preliminary model from {preliminary.source_region}?\n\n"
                    f"Profile: {preliminary.profile_width_mm:.1f} × "
                    f"{preliminary.profile_height_mm:.1f} mm\n"
                    f"Extrusion: {preliminary.thickness_mm:.1f} mm\n\n"
                    f"Detected holes: {preliminary.hole_count}"
                    + (
                        f" at about Ø{preliminary.hole_diameter_mm:.2f} mm"
                        if preliminary.hole_diameter_mm is not None
                        else ""
                    )
                    + "\n\nThis replaces the current feature tree. The view, "
                    "thickness, and hole sizes/locations are inferred; countersinks "
                    "are not modeled. Review before manufacturing.",
                    parent=self,
                )
                if not confirmed:
                    self.safe_log_to_sw_panel(
                        "[DXF] Preliminary model creation cancelled; current part unchanged."
                    )
                    return

                self.part = preliminary.part
                self.selected = 0
                self._raster_blueprint_source = None
                self.safe_log_to_sw_panel(
                    f"[DXF] Created PRELIMINARY model from {preliminary.source_region}; "
                    f"scale={preliminary.drawing_scale:.4g}×, profile="
                    f"{preliminary.profile_width_mm:.1f} × "
                    f"{preliminary.profile_height_mm:.1f} mm, "
                    f"extrusion={preliminary.thickness_mm:.1f} mm."
                )
                self.safe_log_to_sw_panel(
                    f"[DXF] Review required: {preliminary.review_note}"
                )
                self.safe_log_to_sw_panel(
                    "[DXF] Edit the generated sketch/extrude in the feature tree, "
                    "then use COMPILE TO SOLIDWORKS to build it."
                )
                self.refresh_all()
                return
            if dxf_report["document_units"] is None:
                self.safe_log_to_sw_panel(
                    "[DXF] WARNING: document units are unresolved; no physical "
                    "scale will be assumed."
                )

        from ..core.validate import parse_blueprint_vectors

        self._raster_blueprint_source = None
        manual_review = []

        def ingestion_log(message):
            if "Scanned drawing image sheet detected" in message:
                self._raster_blueprint_source = file_path
            if "Automatic contour tracing is unsafe" in message:
                manual_review.append(message)
            self.safe_log_to_sw_panel(message)

        try:
            parsed_shapes = parse_blueprint_vectors(
                file_path,
                log=ingestion_log,
            )
        except Exception as exc:
            self.safe_log_to_sw_panel(f"[INGESTION] ERROR: Could not parse drawing: {exc}")
            return

        if not parsed_shapes:
            if not manual_review:
                self.safe_log_to_sw_panel(
                    "[INGESTION] WARNING: No readable vectors found. "
                    "The file may be scanned, empty, unsupported, or missing its parser dependency."
                )
            return

        entities = []
        for shape in parsed_shapes:
            if shape.get("type") == "rect":
                entities.append(Rect(
                    cx=float(shape.get("cx", 0)),
                    cy=float(shape.get("cy", 0)),
                    width=float(shape["width"]),
                    height=float(shape["height"]),
                ))
            elif shape.get("type") == "polygon":
                entities.append(Polygon(
                    points=[(float(x), float(y)) for x, y in shape["points"]],
                ))
            elif shape.get("type") == "circle":
                entities.append(Circle(
                    cx=float(shape.get("cx", 0)),
                    cy=float(shape.get("cy", 0)),
                    diameter=float(shape["diameter"]),
                ))

        if not entities:
            self.safe_log_to_sw_panel(
                "[INGESTION] WARNING: The file contained vectors, but no supported "
                "closed rectangle or polygon paths were found."
            )
            return

        target = self.current_feature()
        if isinstance(target, Extrude):
            target = self.part.find(target.sketch)
        if not isinstance(target, Sketch):
            target = next(
                (feature for feature in reversed(self.part.features) if isinstance(feature, Sketch)),
                None,
            )
        if target is None:
            target = Sketch(
                name=self.part.unique_name("Sketch"),
                entities=[],
            )
            self.part.features.append(target)
        target.entities = entities
        self.selected = self.part.features.index(target)
        xs, ys = [], []
        for entity in entities:
            if isinstance(entity, Rect):
                xs.extend((entity.cx - entity.width / 2, entity.cx + entity.width / 2))
                ys.extend((entity.cy - entity.height / 2, entity.cy + entity.height / 2))
            elif isinstance(entity, Circle):
                radius = entity.diameter / 2
                xs.extend((entity.cx - radius, entity.cx + radius))
                ys.extend((entity.cy - radius, entity.cy + radius))
            elif isinstance(entity, Polygon):
                xs.extend(point[0] for point in entity.points)
                ys.extend(point[1] for point in entity.points)
        if xs and ys:
            self.safe_log_to_sw_panel(
                f"[INGESTION] Local sketch bounds: "
                f"{max(xs) - min(xs):.2f} mm wide × {max(ys) - min(ys):.2f} mm high."
            )
        self.safe_log_to_sw_panel(
            f"[INGESTION]: Successfully extracted {len(entities)} geometry paths "
            f"into '{target.name}'."
        )
        self.safe_log_to_sw_panel("[INGESTION]: Sketch configuration fields updated from drawing.")
        self.refresh_all()

    def new_part(self):
        self.part = Part(name="Part", features=[])
        self.selected = None
        self.refresh_all()

    def open_file(self):
        path = filedialog.askopenfilename(
            initialdir=EXAMPLES_DIR if os.path.isdir(EXAMPLES_DIR) else None,
            filetypes=[("Part JSON", "*.json")])
        if not path:
            return
        try:
            self.part = Part.load(path)
        except Exception as exc:
            messagebox.showerror("Open failed", f"Could not read {path}\n\n{exc}")
            return
        self.selected = 0 if self.part.features else None
        self.refresh_all()

    def save_file(self):
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("Part JSON", "*.json")],
                                            initialfile=f"{self.part.name}.json")
        if path:
            self.part.save(path)

    def export_preview(self):
        if self.result is None:
            messagebox.showinfo("Export", "Nothing to export - fix the errors first.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".stl",
                                            filetypes=[("STL", "*.stl"), ("STEP", "*.step")])
        if not path:
            return
        try:
            from ..backends import preview_cq

            preview_cq.export(self.part, path)
            self.log(f"Exported preview solid to {path}")
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))

    # ================================================================ SolidWorks
    def log(self, message):
        """Thread-safe append to the SolidWorks log."""
        self.safe_log_to_sw_panel(message)

    def safe_log_to_sw_panel(self, message):
        """Queue a log append on the Tk main thread."""
        def append():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", message + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

        self.after(0, append)

    def safe_log_to_terminal(self, message):
        """Backward-compatible alias for the SolidWorks log callback."""
        self.safe_log_to_sw_panel(message)

    def _run_bg(self, label, fn):
        self.busy = True
        for b in (self.btn_sw_build, self.btn_sw_export):
            b.configure(state="disabled")
        self.log(label)

        def worker():
            try:
                from ..backends import solidworks

                with solidworks.com_thread():
                    fn()
            except Exception as exc:
                self.log(f"FAILED: {exc}")
                self.log(traceback.format_exc())
            finally:
                self.after(0, self._bg_done)

        threading.Thread(target=worker, daemon=True).start()

    def _bg_done(self):
        self.busy = False
        for b in (self.btn_sw_build, self.btn_sw_export):
            b.configure(state="normal")

    def on_compile_click(self):
        """Snapshot the current model and start the SolidWorks COM worker."""
        if self.busy:
            return
        from ..backends import solidworks

        if not solidworks.AVAILABLE:
            messagebox.showerror(
                "SolidWorks",
                "pywin32 not found. This only runs on Windows:\npip install pywin32",
            )
            return
        if has_errors(validate(self.part)):
            messagebox.showinfo("SolidWorks", "Fix the pre-flight errors first.")
            return

        # No pipeline selector or edge-isolation checkbox exists in this UI;
        # the backend builds the feature types in the model's ordered tree.
        part = Part.from_dict(self.part.to_dict())
        expected = dict(self.result.volumes) if self.result is not None else {}
        raster_source = self._raster_blueprint_source

        self.busy = True
        self.btn_compile.configure(state="disabled", text="🔨 COMPILING...")
        self.btn_sw_export.configure(state="disabled")
        self._set_text(self.log_box, "")
        self.safe_log_to_sw_panel("[ALIA ENGINE]: Initializing background thread...")

        worker = threading.Thread(
            target=self._threaded_build_worker,
            args=((part, expected, raster_source),),
            daemon=True,
        )
        try:
            worker.start()
        except RuntimeError as exc:
            self.busy = False
            self.btn_compile.configure(state="normal", text="COMPILE TO SOLIDWORKS")
            self.btn_sw_export.configure(state="normal")
            self.safe_log_to_sw_panel(f"FAILED to start SolidWorks worker: {exc}")

    def _threaded_build_worker(self, part_data):
        """Build the snapshotted part in a worker thread with initialized COM."""
        started = time.perf_counter()
        try:
            from ..core.validate import segment_and_flatten_blueprint

            if len(part_data) == 3:
                part, expected, raster_source = part_data
            else:
                part, expected = part_data
                raster_source = None

            if raster_source:
                blueprint_report = segment_and_flatten_blueprint(raster_source)
                if blueprint_report["status"] != "ok":
                    self.safe_log_to_sw_panel(
                        f"⚠️ Validation Warning: {blueprint_report['note']}"
                    )
                    return
                self.safe_log_to_sw_panel(
                    "[ALIA ENGINE]: Pre-flight Input Diagnosis Checked. "
                    f"Snapped {len(blueprint_report['features'])} lines to "
                    "90-degree orthogonal constraints."
                )
            else:
                self.safe_log_to_sw_panel(
                    "[ALIA ENGINE]: No raster blueprint source is attached; "
                    "using the validated feature tree."
                )

            from ..backends import solidworks
            with solidworks.com_thread():
                builder = solidworks.SolidWorksBuilder(log=self.safe_log_to_sw_panel)
                self.safe_log_to_sw_panel(
                    "[COM CONNECT]: Starting SolidWorks connection and feature build..."
                )
                builder.connect()
                builder.app.UserControlBackground = True
                builder.call(
                    "SetUserPreferenceToggle(192)",
                    builder.app.SetUserPreferenceToggle,
                    192,
                    True,
                )
                report = builder.build(part, expected)
                self.safe_log_to_sw_panel(report.summary())
        except Exception as exc:
            self.safe_log_to_sw_panel(f"FAILED: {exc}")
            self.safe_log_to_sw_panel(traceback.format_exc())
        finally:
            elapsed = time.perf_counter() - started
            self.safe_log_to_sw_panel(f"Compilation finished in {elapsed:.1f} seconds.")
            self.after(0, self._compile_finished)

    def _compile_finished(self):
        self.busy = False
        self.btn_compile.configure(state="normal", text="COMPILE TO SOLIDWORKS")
        self.btn_sw_export.configure(state="normal")

    def build_in_sw(self):
        """Backward-compatible entry point for callers using the old method."""
        self.on_compile_click()

    def export_from_sw(self):
        if self.busy:
            return
        from ..backends import solidworks

        if not solidworks.AVAILABLE:
            messagebox.showerror("SolidWorks", "pywin32 not found. This only runs on Windows:\npip install pywin32")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".stl",
            filetypes=[("STL", "*.stl"), ("STEP", "*.step"), ("SolidWorks part", "*.sldprt")])
        if not path:
            return

        def job():
            builder = solidworks.SolidWorksBuilder(log=self.log)
            builder.connect()
            self.log(f"Saved {builder.export(path)}")

        self._run_bg("Exporting from SolidWorks...", job)
