"""
3D preview widget.

The CadQuery mesh is rendered offscreen with VTK (real depth buffer, so hidden
faces and hidden edges stay hidden) and shown in a Tk label.
  * left-drag : orbit          * right-drag : roll
  * mouse wheel : zoom         * double-click : reset view
Model coordinates are used directly: X right, Y up, Z toward the viewer, which is
the same orientation as SolidWorks' Front view.
"""
import math
import tkinter as tk

import customtkinter as ctk
import numpy as np
from PIL import Image, ImageTk

try:
    import vtk
    from vtk.util import numpy_support as ns
    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False

BG_HEX = "#1E1E1E"
BG = (0.118, 0.118, 0.118)
BODY = (0.36, 0.55, 0.72)
EDGE = (0.0, 0.83, 1.0)
DEFAULT_VIEW = (40.0, 28.0, 1.0)   # azimuth deg, elevation deg, zoom


def _id_array(values):
    return ns.numpy_to_vtkIdTypeArray(np.ascontiguousarray(values, dtype=np.int64), deep=True)


def _triangle_cells(triangles: np.ndarray):
    cells = vtk.vtkCellArray()
    m = len(triangles)
    try:
        cells.SetData(_id_array(np.arange(0, 3 * (m + 1), 3)), _id_array(triangles.ravel()))
    except Exception:  # older VTK
        conn = np.column_stack([np.full(m, 3), triangles]).ravel()
        cells.SetCells(m, _id_array(conn))
    return cells


class Viewport(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=BG_HEX, corner_radius=10, **kwargs)
        self.label = tk.Label(self, bg=BG_HEX, fg="#9A9A9A", bd=0, highlightthickness=0,
                              font=("Segoe UI", 11), compound="center")
        self.label.pack(fill="both", expand=True, padx=4, pady=4)
        self.hint = ctk.CTkLabel(
            self,
            text="Left-drag: orbit   Right-drag: roll   Wheel: zoom   Double-click: reset",
            fg_color="#2B2B2B",
            text_color="#C8C8C8",
            corner_radius=5,
            font=ctk.CTkFont(size=10),
        )
        self.hint.place(relx=0.99, rely=0.02, anchor="ne")

        self._az, self._el, self._zoom = DEFAULT_VIEW
        self._ren = self._win = self._grab = None
        self._camera_ready = False
        self._center = np.zeros(3)
        self._radius = 1.0
        self._photo = None
        self._last_image = None
        self._pending = None
        self._drag = None

        self.label.bind("<Configure>", lambda _e: self._schedule())
        self.label.configure(cursor="fleur")
        self.label.bind("<ButtonPress-1>", lambda e: self._on_press(e, "orbit"))
        self.label.bind("<B1-Motion>", self._on_drag)
        self.label.bind("<ButtonRelease-1>", self._on_release)
        self.label.bind("<ButtonPress-3>", lambda e: self._on_press(e, "roll"))
        self.label.bind("<B3-Motion>", self._on_drag)
        self.label.bind("<ButtonRelease-3>", self._on_release)
        self.label.bind("<MouseWheel>", lambda e: self._on_wheel(e.delta / 120.0))
        self.label.bind("<Button-4>", lambda _e: self._on_wheel(1))     # Linux
        self.label.bind("<Button-5>", lambda _e: self._on_wheel(-1))
        self.label.bind("<Double-Button-1>", self._reset_view)
        self.show(None, "Add a sketch and an extrude to see the part")

    # ------------------------------------------------------------ public
    def show(self, result=None, message: str = ""):
        if not AVAILABLE:
            self._show_text("VTK is not installed (pip install vtk)")
            return
        if result is None or result.vertices is None or len(result.triangles) == 0:
            self._ren = None
            self._camera_ready = False
            self._show_text(message or "No geometry")
            return
        self._build_scene(result)
        self._schedule()

    def save_png(self, path: str) -> None:
        self._render_now()
        if self._last_image is not None:
            self._last_image.save(path)

    # ------------------------------------------------------------ scene
    def _show_text(self, text):
        self._photo = None
        self.label.configure(image="", text=text)

    def _ensure_window(self):
        if self._win is None:
            self._win = vtk.vtkRenderWindow()
            self._win.SetOffScreenRendering(1)
            self._win.SetMultiSamples(4)
            self._grab = vtk.vtkWindowToImageFilter()
            self._grab.SetInputBufferTypeToRGB()
            self._grab.ReadFrontBufferOff()
            self._grab.SetInput(self._win)

    def _build_scene(self, result):
        self._ensure_window()
        if self._ren is not None:
            self._win.RemoveRenderer(self._ren)
        verts = np.asarray(result.vertices, dtype=float)

        pts = vtk.vtkPoints()
        pts.SetData(ns.numpy_to_vtk(verts, deep=True))
        poly = vtk.vtkPolyData()
        poly.SetPoints(pts)
        poly.SetPolys(_triangle_cells(np.asarray(result.triangles)))

        normals = vtk.vtkPolyDataNormals()
        normals.SetInputData(poly)
        normals.SplittingOn()
        normals.SetFeatureAngle(40.0)
        normals.ConsistencyOn()
        normals.Update()

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(normals.GetOutputPort())
        mapper.SetResolveCoincidentTopologyToPolygonOffset()
        mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(1.0, 1.0)
        body = vtk.vtkActor()
        body.SetMapper(mapper)
        prop = body.GetProperty()
        prop.SetColor(*BODY)
        prop.SetAmbient(0.25)
        prop.SetDiffuse(0.75)
        prop.SetSpecular(0.15)
        prop.SetSpecularPower(20)

        ren = vtk.vtkRenderer()
        ren.SetBackground(*BG)
        ren.AddActor(body)
        edge_actor = self._edge_actor(result)
        if edge_actor is not None:
            ren.AddActor(edge_actor)
        kit = vtk.vtkLightKit()
        kit.AddLightsToRenderer(ren)
        ren.TwoSidedLightingOn()

        lo, hi = verts.min(axis=0), verts.max(axis=0)
        self._center = (lo + hi) / 2.0
        self._radius = max(float(np.linalg.norm(hi - lo)) / 2.0, 1.0)
        self._ren = ren
        self._camera_ready = False
        self._win.AddRenderer(ren)

    def _edge_actor(self, result):
        if not result.edges:
            return None
        all_pts = np.vstack(result.edges)
        pts = vtk.vtkPoints()
        pts.SetData(ns.numpy_to_vtk(all_pts, deep=True))
        lines = vtk.vtkCellArray()
        offset = 0
        for e in result.edges:
            n = len(e)
            lines.InsertNextCell(n)
            for i in range(n):
                lines.InsertCellPoint(offset + i)
            offset += n
        poly = vtk.vtkPolyData()
        poly.SetPoints(pts)
        poly.SetLines(lines)
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(poly)
        mapper.SetResolveCoincidentTopologyToPolygonOffset()
        mapper.SetRelativeCoincidentTopologyLineOffsetParameters(-1.0, -1.0)
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(*EDGE)
        actor.GetProperty().SetLineWidth(1.5)
        actor.GetProperty().LightingOff()
        return actor

    # ------------------------------------------------------------ rendering
    def _schedule(self):
        if self._pending is None:
            self._pending = self.after(12, self._render_now)

    def _render_now(self):
        self._pending = None
        if self._ren is None or self._win is None:
            return
        w, h = max(self.label.winfo_width(), 200), max(self.label.winfo_height(), 200)
        self._win.SetSize(w, h)

        cam = self._ren.GetActiveCamera()
        if not self._camera_ready:
            az, el = math.radians(self._az), math.radians(self._el)
            direction = np.array([
                math.cos(el) * math.sin(az),
                math.sin(el),
                math.cos(el) * math.cos(az),
            ])
            cam.SetFocalPoint(*self._center)
            cam.SetPosition(*(self._center + direction * self._radius * 6.0))
            cam.SetViewUp(0, 1, 0)
            self._camera_ready = True
        cam.ParallelProjectionOn()
        cam.SetParallelScale(self._radius * 1.1 / self._zoom)
        self._ren.ResetCameraClippingRange()

        self._win.Render()
        self._grab.Modified()
        self._grab.Update()
        img = self._grab.GetOutput()
        iw, ih, _ = img.GetDimensions()
        arr = ns.vtk_to_numpy(img.GetPointData().GetScalars()).reshape(ih, iw, -1)[::-1, :, :3]
        self._last_image = Image.fromarray(np.ascontiguousarray(arr))
        self._photo = ImageTk.PhotoImage(self._last_image)
        self.label.configure(image=self._photo, text="")

    # ------------------------------------------------------------ mouse
    def _on_press(self, event, mode):
        self._drag = (event.x, event.y, mode)

    def _on_drag(self, event):
        if self._drag is None:
            return
        dx, dy = event.x - self._drag[0], event.y - self._drag[1]
        mode = self._drag[2]
        self._drag = (event.x, event.y, mode)
        if self._ren is not None and self._camera_ready:
            camera = self._ren.GetActiveCamera()
            if mode == "roll":
                camera.Roll(dx * 0.5)
            else:
                camera.Azimuth(-dx * 0.5)
                camera.Elevation(dy * 0.5)
                camera.OrthogonalizeViewUp()
        self._schedule()

    def _on_release(self, _event=None):
        self._drag = None

    def _on_wheel(self, steps):
        self._zoom = max(0.2, min(10.0, self._zoom * (1.12 ** steps)))
        self._schedule()

    def _reset_view(self, _event=None):
        self._az, self._el, self._zoom = DEFAULT_VIEW
        self._camera_ready = False
        self._schedule()
