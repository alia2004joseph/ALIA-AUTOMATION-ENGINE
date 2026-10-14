"""Right panel: property editors that change with the selected feature."""
import customtkinter as ctk

from ..core.model import (
    DIRECTIONS, END_CONDITIONS, OPERATIONS, PLANES,
    Circle, Extrude, Polygon, Rect, Sketch,
)

RED = "#E5484D"


def _fmt(v) -> str:
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def parse_points(text: str):
    """'0,0; 20,0; 20,10' -> [(0.0, 0.0), (20.0, 0.0), (20.0, 10.0)]"""
    pts = []
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        x, y = chunk.split(",")
        pts.append((float(x), float(y)))
    if len(pts) < 3:
        raise ValueError("need 3+ points")
    return pts


def format_points(points) -> str:
    return "; ".join(f"{x:g},{y:g}" for x, y in points)


class EditorPanel(ctk.CTkScrollableFrame):
    def __init__(self, master, get_part, on_change, on_rename, on_import=None, **kwargs):
        super().__init__(master, **kwargs)
        self.get_part = get_part
        self.on_change = on_change
        self.on_rename = on_rename
        self.on_import = on_import
        self.feature = None

    # ------------------------------------------------------------ helpers
    def _clear(self):
        for child in self.winfo_children():
            child.destroy()

    def _title(self, parent, text):
        ctk.CTkLabel(parent, text=text, font=ctk.CTkFont(size=16, weight="bold")).pack(
            anchor="w", padx=4, pady=(8, 6))

    def _row(self, parent, label):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ctk.CTkLabel(row, text=label, width=84, anchor="w").pack(side="left")
        return row

    def _field(self, parent, label, value, cast, setter):
        row = self._row(parent, label)
        var = ctk.StringVar(value=_fmt(value))
        entry = ctk.CTkEntry(row, textvariable=var)
        entry.pack(side="left", fill="x", expand=True)
        normal = entry.cget("border_color")

        def commit(_event=None):
            try:
                value = cast(var.get())
            except (ValueError, TypeError):
                entry.configure(border_color=RED)
                return
            entry.configure(border_color=normal)
            setter(value)
            self.on_change()

        entry.bind("<KeyRelease>", commit)
        return entry

    def _option(self, parent, label, values, current, setter, refresh=False):
        row = self._row(parent, label)
        values = list(values) or ["(none)"]
        var = ctk.StringVar(value=current if current in values else values[0])

        def changed(choice):
            setter(choice)
            if refresh:
                self.show(self.feature)
            self.on_change()

        ctk.CTkOptionMenu(row, values=values, variable=var, command=changed).pack(
            side="left", fill="x", expand=True)

    def _name_field(self, parent, feature):
        row = self._row(parent, "Name")
        var = ctk.StringVar(value=feature.name)
        entry = ctk.CTkEntry(row, textvariable=var)
        entry.pack(side="left", fill="x", expand=True)
        normal = entry.cget("border_color")

        def commit(_event=None):
            if var.get().strip() == feature.name:
                entry.configure(border_color=normal)
                return
            if self.on_rename(feature, var.get()):
                entry.configure(border_color=normal)
            else:
                entry.configure(border_color=RED)

        entry.bind("<Return>", commit)
        entry.bind("<FocusOut>", commit)

    # ------------------------------------------------------------ public
    def show(self, feature):
        self._clear()
        self.feature = feature
        if self.on_import is not None:
            ctk.CTkButton(
                self,
                text="📂 Import Vector Drawing (.pdf, .dxf)",
                command=self.on_import,
                fg_color="#3A3A3A",
                hover_color="#4A4A4A",
                anchor="w",
            ).pack(fill="x", padx=2, pady=(4, 8))
        if feature is None:
            ctk.CTkLabel(self, text="Select a feature in the tree\nor add a new one.",
                         text_color="#9A9A9A").pack(pady=40)
        elif isinstance(feature, Sketch):
            self._show_sketch(feature)
        elif isinstance(feature, Extrude):
            self._show_extrude(feature)
        else:
            self._title(self, type(feature).__name__)
            self._name_field(self, feature)
            ctk.CTkLabel(
                self,
                text="Advanced SolidWorks feature. Configure its geometry and persistent references in the part JSON.",
                justify="left",
                wraplength=270,
                text_color="#9A9A9A",
            ).pack(anchor="w", padx=4, pady=8)

    # ------------------------------------------------------------ sketch
    def _show_sketch(self, sk: Sketch):
        self._title(self, "Sketch")
        self._name_field(self, sk)
        self._option(self, "Plane", PLANES, sk.plane, lambda v: setattr(sk, "plane", v))
        self._field(self, "Offset (mm)", sk.offset, float, lambda v: setattr(sk, "offset", v))
        ctk.CTkLabel(self, text="Shapes (mm, plane coordinates)", anchor="w",
                     text_color="#9A9A9A").pack(fill="x", padx=4, pady=(10, 2))

        for i, ent in enumerate(sk.entities):
            box = ctk.CTkFrame(self, border_width=1, border_color="#444444")
            box.pack(fill="x", pady=4)
            head = ctk.CTkFrame(box, fg_color="transparent")
            head.pack(fill="x", padx=6, pady=(4, 0))
            ctk.CTkLabel(head, text=f"{i + 1}. {ent.kind.title()}",
                         font=ctk.CTkFont(weight="bold")).pack(side="left")
            ctk.CTkButton(head, text="x", width=26, height=22, fg_color="#8B2E32",
                          command=lambda idx=i: self._remove_entity(sk, idx)).pack(side="right")
            inner = ctk.CTkFrame(box, fg_color="transparent")
            inner.pack(fill="x", padx=6, pady=(2, 6))
            if isinstance(ent, Rect):
                self._field(inner, "Center X", ent.cx, float, lambda v, e=ent: setattr(e, "cx", v))
                self._field(inner, "Center Y", ent.cy, float, lambda v, e=ent: setattr(e, "cy", v))
                self._field(inner, "Width", ent.width, float, lambda v, e=ent: setattr(e, "width", v))
                self._field(inner, "Height", ent.height, float, lambda v, e=ent: setattr(e, "height", v))
            elif isinstance(ent, Circle):
                self._field(inner, "Center X", ent.cx, float, lambda v, e=ent: setattr(e, "cx", v))
                self._field(inner, "Center Y", ent.cy, float, lambda v, e=ent: setattr(e, "cy", v))
                self._field(inner, "Diameter", ent.diameter, float, lambda v, e=ent: setattr(e, "diameter", v))
            elif isinstance(ent, Polygon):
                self._field(inner, "Points", format_points(ent.points), parse_points,
                            lambda v, e=ent: setattr(e, "points", v))
                ctk.CTkLabel(inner, text="format: x,y; x,y; x,y", text_color="#9A9A9A",
                             font=ctk.CTkFont(size=11)).pack(anchor="w")

        add = ctk.CTkFrame(self, fg_color="transparent")
        add.pack(fill="x", pady=(6, 2))
        for col, (text, cls) in enumerate((("+ Rectangle", Rect), ("+ Circle", Circle), ("+ Polygon", Polygon))):
            add.grid_columnconfigure(col, weight=1)
            ctk.CTkButton(add, text=text, height=28,
                          command=lambda c=cls: self._add_entity(sk, c)).grid(row=0, column=col, padx=2, sticky="ew")

    def _add_entity(self, sk, cls):
        sk.entities.append(cls())
        self.show(sk)
        self.on_change()

    def _remove_entity(self, sk, index):
        del sk.entities[index]
        self.show(sk)
        self.on_change()

    # ------------------------------------------------------------ extrude
    def _show_extrude(self, f: Extrude):
        title = "Extruded Boss" if f.operation == "boss" else "Extruded Cut"
        self._title(self, title)
        self._name_field(self, f)
        names = [s.name for s in self.get_part().sketches_before(f)]
        if names and f.sketch not in names:
            f.sketch = names[-1]
        self._option(self, "Sketch", names, f.sketch, lambda v: setattr(f, "sketch", "" if v == "(none)" else v))
        self._option(self, "Operation", OPERATIONS, f.operation,
                     lambda v: setattr(f, "operation", v), refresh=True)
        self._option(self, "End", END_CONDITIONS, f.end, lambda v: setattr(f, "end", v))
        self._field(self, "Depth (mm)", f.depth, float, lambda v: setattr(f, "depth", v))
        self._option(self, "Direction", DIRECTIONS, f.direction, lambda v: setattr(f, "direction", v))
        ctk.CTkLabel(
            self, justify="left", anchor="w", text_color="#9A9A9A", font=ctk.CTkFont(size=11),
            text="one = along the plane normal\nreverse = against the normal\nsymmetric = half each side\n"
                 "(Front +Z, Top +Y, Right +X)",
        ).pack(fill="x", padx=4, pady=(8, 0))
