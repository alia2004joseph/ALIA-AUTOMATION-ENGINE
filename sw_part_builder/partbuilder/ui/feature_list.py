"""Left panel: the feature tree with add / delete / reorder controls."""
import customtkinter as ctk

from ..core.model import EdgeModifier, Extrude, Revolve, Sketch, Sketch3D, WeldmentMember

SELECTED = "#1F6AA5"
IDLE = "#2B2B2B"
BAD = "#7A2A2E"


def describe(feature) -> str:
    if isinstance(feature, Sketch):
        return f"{feature.name}   ({feature.plane}, {len(feature.entities)} shape(s))"
    if isinstance(feature, Extrude):
        kind = "Boss" if feature.operation == "boss" else "Cut"
        return f"{feature.name}   ({kind}, {feature.depth:g} mm)"
    if isinstance(feature, Sketch3D):
        return f"{feature.name}   (3D path, {len(feature.segments)} line(s))"
    if isinstance(feature, Revolve):
        return f"{feature.name}   ({feature.operation.title()} revolve, {feature.angle:g} deg)"
    if isinstance(feature, EdgeModifier):
        return f"{feature.name}   ({feature.modifier_type.title()}, {feature.radius_mm:g} mm)"
    if isinstance(feature, WeldmentMember):
        return f"{feature.name}   (Weldment, {feature.sketch_3d_name})"
    return feature.name


class FeatureList(ctk.CTkFrame):
    def __init__(self, master, on_select, on_add, on_delete, on_move, **kwargs):
        super().__init__(master, **kwargs)
        self.on_select = on_select
        self._part = None
        self._expanded = None
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        heading = ctk.CTkFrame(self, fg_color="transparent")
        heading.grid(row=0, column=0, padx=10, pady=(8, 4), sticky="ew")
        heading.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(heading, text="Feature Tree", font=ctk.CTkFont(size=16, weight="bold")).grid(
            row=0, column=0, padx=2, sticky="w")
        ctk.CTkButton(
            heading, text="Expand", width=58, height=24,
            command=lambda: self._set_all_expanded(True),
        ).grid(row=0, column=1, padx=(2, 1))
        ctk.CTkButton(
            heading, text="Collapse", width=64, height=24,
            command=lambda: self._set_all_expanded(False),
        ).grid(row=0, column=2, padx=(1, 0))
        self.list_frame = ctk.CTkScrollableFrame(self, fg_color="#1E1E1E")
        self.list_frame.grid(row=1, column=0, padx=10, sticky="nsew")

        add_row = ctk.CTkFrame(self, fg_color="transparent")
        add_row.grid(row=2, column=0, padx=10, pady=(8, 2), sticky="ew")
        for col, (text, kind) in enumerate((("+ Sketch", "sketch"), ("+ Boss", "boss"), ("+ Cut", "cut"))):
            add_row.grid_columnconfigure(col, weight=1)
            ctk.CTkButton(add_row, text=text, height=30, command=lambda k=kind: on_add(k)).grid(
                row=0, column=col, padx=2, sticky="ew")

        edit_row = ctk.CTkFrame(self, fg_color="transparent")
        edit_row.grid(row=3, column=0, padx=10, pady=(2, 12), sticky="ew")
        for col, (text, cmd, color) in enumerate((
                ("Up", lambda: on_move(-1), None),
                ("Down", lambda: on_move(1), None),
                ("Delete", on_delete, "#8B2E32"))):
            edit_row.grid_columnconfigure(col, weight=1)
            kw = {"fg_color": color} if color else {}
            ctk.CTkButton(edit_row, text=text, height=28, command=cmd, **kw).grid(
                row=0, column=col, padx=2, sticky="ew")

    def _set_all_expanded(self, expanded):
        if self._part is None:
            return
        self._expanded = {
            feature.name for feature in self._part.features
            if expanded
        }
        self._render_tree()

    def refresh(self, part, selected, bad_names):
        self._part = part
        self._selected = selected
        self._bad_names = bad_names
        if self._expanded is None:
            self._expanded = {feature.name for feature in part.features}
        self._render_tree()

    @staticmethod
    def _parent_index(features):
        index_by_name = {feature.name: i for i, feature in enumerate(features)}
        parent = {}
        for i, feature in enumerate(features):
            reference = None
            if isinstance(feature, (Extrude,)):
                reference = feature.sketch
            elif isinstance(feature, Revolve):
                reference = feature.sketch_name
            elif isinstance(feature, WeldmentMember):
                reference = feature.sketch_3d_name
            elif isinstance(feature, EdgeModifier):
                reference = feature.feature_target
            parent_index = index_by_name.get(reference)
            if parent_index is not None and parent_index < i:
                parent[i] = parent_index
        return parent

    def _render_tree(self):
        for child in self.list_frame.winfo_children():
            child.destroy()
        features = self._part.features
        parent = self._parent_index(features)
        children = {i: [] for i in range(len(features))}
        roots = []
        for index, parent_index in parent.items():
            children[parent_index].append(index)
        for index in range(len(features)):
            if index not in parent:
                roots.append(index)

        def draw(index, depth):
            feature = features[index]
            descendants = children[index]
            row = ctk.CTkFrame(self.list_frame, fg_color="transparent")
            row.pack(fill="x", pady=1)
            row.grid_columnconfigure(2, weight=1)
            ctk.CTkLabel(row, text="", width=depth * 12).grid(row=0, column=0)
            if descendants:
                marker = "v" if feature.name in self._expanded else ">"
                ctk.CTkButton(
                    row, text=marker, width=22, height=30,
                    command=lambda name=feature.name: self._toggle(name),
                ).grid(row=0, column=1, sticky="w", padx=(0, 2))
                text_column = 2
            else:
                ctk.CTkLabel(row, text="", width=24).grid(row=0, column=1)
                text_column = 2
            color = SELECTED if index == self._selected else (
                BAD if feature.name in self._bad_names else IDLE
            )
            ctk.CTkButton(
                row, text=describe(feature), anchor="w", height=30, fg_color=color,
                hover_color="#3A3A3A" if index != self._selected else SELECTED,
                command=lambda idx=index: self.on_select(idx),
            ).grid(row=0, column=text_column, sticky="ew")
            if descendants and feature.name in self._expanded:
                for child_index in descendants:
                    draw(child_index, depth + 1)

        for root_index in roots:
            draw(root_index, 0)

    def _toggle(self, name):
        if name in self._expanded:
            self._expanded.remove(name)
        else:
            self._expanded.add(name)
        self._render_tree()
