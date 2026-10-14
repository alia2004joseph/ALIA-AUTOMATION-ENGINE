"""Main window: wires the feature tree, editors, 3D preview and the two backends together."""
import os
import threading
import time
import traceback
from tkinter import filedialog, messagebox

import customtkinter as ctk

from ..backends import preview_cq, solidworks
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
            on_import=self.on_import_vector_click, width=320,
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
            self.result = None
            self.viewport.show(None, "Fix the errors (red items) to see the preview")
        else:
            try:
                self.result = preview_cq.build_preview(self.part)
                lines += [f"warn  {w}" for w in self.result.warnings]
                self.viewport.show(self.result, "Nothing to show yet")
            except preview_cq.BuildError as exc:
                self.result = None
                lines.append(f"ERROR preview: {exc}")
                self.viewport.show(None, "Preview failed - see Checks")
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
