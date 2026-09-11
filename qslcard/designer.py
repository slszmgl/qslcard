"""Card designer: graphical editing of card faces (SRS section 7).

A Tk canvas scaled to millimetres shows the bleed box, the trim line and the
3 mm calibration line, with draggable/resizable elements on top.  Image assets
are picked from disk, previewed, and checked for effective print resolution so a
soft-looking logo is caught before printing.

All geometry lives in qslcard.editor (pure, unit tested); this module is the
thin interactive layer.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk
from typing import Any

from .editor import (
    PLACEHOLDER_PRESETS,
    add_element,
    element_rect,
    full_bleed_rect,
    hit_test,
    image_report,
    move_element,
    resize_element,
)
from .images import is_supported_image
from .templates import CardTemplate, Element, available_templates, load_template

__all__ = ["CardDesigner"]

SCALE = 3.4
PADDING = 26
HANDLE_PX = 4
SNAP_MM = 0.5

ELEMENT_LABELS = {
    "text": "文字",
    "image": "图片",
    "rect": "色块",
    "line": "线条",
    "qso_rows": "通联表格",
}


class CardDesigner(tk.Toplevel):
    """Modal-ish designer window bound to one CardTemplate."""

    def __init__(self, parent: tk.Misc, app: Any, template_ref: str = "") -> None:
        super().__init__(parent)
        self.app = app
        self.title("卡片设计器")
        self.geometry("1180x760")
        self.minsize(1060, 680)
        self.transient(parent)

        self.template: CardTemplate = self._load(template_ref)
        self.face_index = 0
        self.selected: Element | None = None
        self.saved = False
        self._drag: dict[str, Any] | None = None
        self._handle_boxes: dict[str, tuple[float, float, float, float]] = {}
        self._canvas_images: list[Any] = []

        self._build()
        self._refresh_list()
        self._draw()
        self.bind("<Escape>", lambda _event: self.destroy())

    # -- loading ----------------------------------------------------------
    def _load(self, reference: str) -> CardTemplate:
        try:
            return load_template(
                reference or self.app.config.template, self.app.config.template_dir
            )
        except (FileNotFoundError, KeyError, ValueError):
            return load_template("classic")

    @property
    def card(self) -> Any:
        return self.template.card

    def _face(self) -> Any:
        return self.template.front if self.face_index == 0 else self.template.back

    # -- layout -----------------------------------------------------------
    def _build(self) -> None:
        style = ttk.Style()
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        self._build_toolbar()
        content = ttk.Frame(self)
        content.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._build_canvas(content)
        self._build_side(content)
        self.status = tk.StringVar(value="拖动元素移动，拖动方块缩放，方向键微调，Delete 删除。")
        ttk.Label(self, textvariable=self.status, padding=(10, 4)).pack(fill="x")

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text="模板").pack(side="left")
        self.template_var = tk.StringVar(value=self.template.name)
        ttk.Combobox(
            bar,
            textvariable=self.template_var,
            values=available_templates(),
            width=14,
            state="readonly",
        ).pack(side="left", padx=(4, 10))
        ttk.Button(bar, text="载入", command=self._reload_template, width=8).pack(side="left")

        ttk.Label(bar, text="编辑面").pack(side="left", padx=(16, 4))
        self.face_var = tk.IntVar(value=0)
        ttk.Radiobutton(
            bar, text="正面", variable=self.face_var, value=0, command=self._switch_face
        ).pack(side="left")
        ttk.Radiobutton(
            bar, text="背面", variable=self.face_var, value=1, command=self._switch_face
        ).pack(side="left", padx=(6, 0))

        ttk.Button(bar, text="保存到文件…", command=self._save_as, width=13).pack(side="right")
        ttk.Button(bar, text="设为当前模板", command=self._use_as_current, width=13).pack(
            side="right", padx=(0, 6)
        )

    def _build_canvas(self, parent: ttk.Frame) -> None:
        card = self.card
        width = int(card.doc_width_mm * SCALE + 2 * PADDING)
        height = int(card.doc_height_mm * SCALE + 2 * PADDING)
        self.canvas = tk.Canvas(
            parent,
            width=width,
            height=height,
            background="#E9ECF1",
            highlightthickness=1,
            highlightbackground="#B9C0CC",
        )
        self.canvas.pack(side="left", fill="y")
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", self._on_double)
        self.bind("<Delete>", lambda _event: self._delete())
        for name, (dx, dy) in (
            ("Left", (-1, 0)),
            ("Right", (1, 0)),
            ("Up", (0, 1)),
            ("Down", (0, -1)),
        ):
            self.bind(f"<{name}>", lambda _event, x=dx, y=dy: self._nudge(x * SNAP_MM, y * SNAP_MM))
            self.bind(f"<Shift-{name}>", lambda _event, x=dx, y=dy: self._nudge(x * 5.0, y * 5.0))

    def _build_side(self, parent: ttk.Frame) -> None:
        side = ttk.Frame(parent, padding=(10, 0))
        side.pack(side="left", fill="both", expand=True)

        listing = ttk.LabelFrame(side, text="元素", padding=(6, 4))
        listing.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(listing, height=8, exportselection=False)
        self.listbox.pack(fill="both", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self._on_list_select)

        buttons = ttk.Frame(listing)
        buttons.pack(fill="x", pady=(4, 0))
        for label, kind in (
            ("+ 文字", "text"),
            ("+ 图片", "image"),
            ("+ 色块", "rect"),
            ("+ 线条", "line"),
            ("+ 表格", "qso_rows"),
        ):
            ttk.Button(buttons, text=label, width=7, command=lambda k=kind: self._add(k)).pack(
                side="left", padx=1
            )
        order = ttk.Frame(listing)
        order.pack(fill="x", pady=(4, 0))
        ttk.Button(order, text="上移", width=7, command=lambda: self._reorder(1)).pack(
            side="left", padx=1
        )
        ttk.Button(order, text="下移", width=7, command=lambda: self._reorder(-1)).pack(
            side="left", padx=1
        )
        ttk.Button(order, text="删除", width=7, command=self._delete).pack(side="left", padx=1)

        props = ttk.LabelFrame(side, text="属性", padding=(6, 4))
        props.pack(fill="both", expand=True, pady=(8, 0))
        self.props: dict[str, tk.Variable] = {
            "x": tk.StringVar(),
            "y": tk.StringVar(),
            "w": tk.StringVar(),
            "h": tk.StringVar(),
            "text": tk.StringVar(),
            "size": tk.StringVar(),
            "align": tk.StringVar(),
            "color": tk.StringVar(),
            "bold": tk.BooleanVar(),
            "image": tk.StringVar(),
        }
        grid = (
            ("X (mm)", "x", 10),
            ("Y (mm)", "y", 10),
            ("宽 (mm)", "w", 10),
            ("高 (mm)", "h", 10),
            ("字号", "size", 10),
        )
        for index, (label, key, width) in enumerate(grid):
            ttk.Label(props, text=label).grid(
                row=index // 2, column=(index % 2) * 2, sticky="w", pady=2
            )
            ttk.Entry(props, textvariable=self.props[key], width=width).grid(
                row=index // 2, column=(index % 2) * 2 + 1, sticky="w", padx=(2, 8)
            )

        ttk.Label(props, text="文本").grid(row=3, column=0, sticky="w", pady=2)
        ttk.Entry(props, textvariable=self.props["text"], width=34).grid(
            row=3, column=1, columnspan=3, sticky="we", pady=2
        )

        placeholders = ttk.Frame(props)
        placeholders.grid(row=4, column=1, columnspan=3, sticky="w")
        for label, value in PLACEHOLDER_PRESETS[:6]:
            ttk.Button(
                placeholders,
                text=label,
                width=8,
                command=lambda v=value: self._insert_placeholder(v),
            ).pack(side="left", padx=1)

        ttk.Label(props, text="对齐").grid(row=5, column=0, sticky="w", pady=2)
        ttk.Combobox(
            props,
            textvariable=self.props["align"],
            values=("left", "center", "right"),
            width=9,
            state="readonly",
        ).grid(row=5, column=1, sticky="w")
        ttk.Checkbutton(props, text="加粗", variable=self.props["bold"]).grid(
            row=5, column=2, sticky="w", padx=(2, 8)
        )
        ttk.Label(props, text="颜色").grid(row=6, column=0, sticky="w", pady=2)
        ttk.Entry(props, textvariable=self.props["color"], width=10).grid(
            row=6, column=1, sticky="w"
        )
        ttk.Button(props, text="选色…", width=7, command=self._pick_colour).grid(
            row=6, column=2, sticky="w", padx=(2, 8)
        )

        ttk.Label(props, text="图片").grid(row=7, column=0, sticky="w", pady=2)
        ttk.Entry(props, textvariable=self.props["image"], width=26).grid(
            row=7, column=1, columnspan=2, sticky="we", pady=2
        )
        ttk.Button(props, text="浏览…", width=7, command=self._pick_image).grid(
            row=7, column=3, sticky="w"
        )

        actions = ttk.Frame(props)
        actions.grid(row=8, column=1, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Button(actions, text="应用属性", width=10, command=self._apply_props).pack(
            side="left", padx=1
        )
        ttk.Button(actions, text="铺满整张卡", width=11, command=self._full_bleed).pack(
            side="left", padx=1
        )

        self.image_note = ttk.Label(
            props, text="", wraplength=330, justify="left", foreground="#555555"
        )
        self.image_note.grid(row=9, column=0, columnspan=4, sticky="w", pady=(6, 0))

        background = ttk.LabelFrame(side, text="整面背景", padding=(6, 4))
        background.pack(fill="x", pady=(8, 0))
        self.bg_var = tk.StringVar()
        ttk.Entry(background, textvariable=self.bg_var, width=30).pack(side="left")
        ttk.Button(background, text="选图片…", width=9, command=self._pick_background).pack(
            side="left", padx=(4, 0)
        )
        ttk.Button(background, text="清除", width=6, command=self._clear_background).pack(
            side="left", padx=(4, 0)
        )
        self.bg_fit = tk.StringVar(value="cover")
        ttk.Combobox(
            background,
            textvariable=self.bg_fit,
            values=("cover", "contain", "stretch"),
            width=8,
            state="readonly",
        ).pack(side="left", padx=(6, 0))

    # -- drawing ----------------------------------------------------------
    def _to_canvas(self, x: float, y: float) -> tuple[float, float]:
        bleed = self.card.bleed_mm
        return (
            PADDING + (x + bleed) * SCALE,
            PADDING + (self.card.height_mm - y + bleed) * SCALE,
        )

    def _to_face(self, px: float, py: float) -> tuple[float, float]:
        bleed = self.card.bleed_mm
        return (
            (px - PADDING) / SCALE - bleed,
            self.card.height_mm - ((py - PADDING) / SCALE - bleed),
        )

    def _rect_to_canvas(
        self, x: float, y: float, w: float, h: float
    ) -> tuple[float, float, float, float]:
        left, top = self._to_canvas(x, y + h)
        right, bottom = self._to_canvas(x + w, y)
        return left, top, right, bottom

    def _draw(self) -> None:
        self.canvas.delete("all")
        self._canvas_images.clear()
        card = self.card
        face = self._face()

        bx, by, bw, bh = full_bleed_rect(card)
        left, top, right, bottom = self._rect_to_canvas(bx, by, bw, bh)
        self.canvas.create_rectangle(left, top, right, bottom, fill=face.background, outline="")

        if face.background_image and Path(face.background_image).is_file():
            self._draw_canvas_image(face.background_image, left, top, right, bottom)

        tx, ty, tw, th = 0.0, 0.0, card.width_mm, card.height_mm
        tl, tt, tr, tb = self._rect_to_canvas(tx, ty, tw, th)
        self.canvas.create_rectangle(tl, tt, tr, tb, outline="#8899AA", dash=(4, 3))
        cal = card.calibration_mm
        cl, ct, cr, cb = self._rect_to_canvas(cal, cal, tw - 2 * cal, th - 2 * cal)
        self.canvas.create_rectangle(cl, ct, cr, cb, outline="#C2CBD8", dash=(2, 3))

        for element in face.elements:
            self._draw_element(element)
        if self.selected is not None and self.selected in face.elements:
            self._draw_handles(self.selected)

    def _draw_canvas_image(
        self, path: str, left: float, top: float, right: float, bottom: float
    ) -> None:
        try:
            from PIL import Image, ImageTk

            with Image.open(path) as handle:
                image = handle.convert("RGBA")
            width = max(int(right - left), 1)
            height = max(int(bottom - top), 1)
            image.thumbnail((width, height))
            photo = ImageTk.PhotoImage(image)
            self._canvas_images.append(photo)
            self.canvas.create_image(
                (left + right) / 2, (top + bottom) / 2, image=photo, anchor="center"
            )
        except Exception:  # noqa: BLE001 - preview is best effort
            self.canvas.create_rectangle(left, top, right, bottom, outline="#8899AA", dash=(2, 2))
            self.canvas.create_text(
                (left + right) / 2, (top + bottom) / 2, text="图片预览不可用", fill="#667788"
            )

    def _draw_element(self, element: Element) -> None:
        x, y, w, h = element_rect(element, self.card)
        left, top, right, bottom = self._rect_to_canvas(x, y, w, h)
        selected = element is self.selected
        outline = "#0B5FFF" if selected else "#8A94A6"
        if element.type == "rect":
            self.canvas.create_rectangle(
                left, top, right, bottom, fill=element.fill or "", outline=element.border or outline
            )
        elif element.type == "line":
            x1, y1 = self._to_canvas(element.x1_mm, element.y1_mm)
            x2, y2 = self._to_canvas(element.x2_mm, element.y2_mm)
            self.canvas.create_line(
                x1, y1, x2, y2, fill=element.color, width=max(int(element.border_width * SCALE), 1)
            )
        elif element.type == "image":
            if element.image and Path(element.image).is_file():
                self._draw_canvas_image(element.image, left, top, right, bottom)
            self.canvas.create_rectangle(left, top, right, bottom, outline=outline)
        elif element.type == "qso_rows":
            self.canvas.create_rectangle(left, top, right, bottom, outline=outline, dash=(3, 2))
            self.canvas.create_text(
                (left + right) / 2, (top + bottom) / 2, text="通联表格", fill="#556070"
            )
        else:
            self.canvas.create_rectangle(left, top, right, bottom, outline=outline, dash=(2, 2))
            font_px = max(int(element.size_pt * (25.4 / 72.0) * SCALE), 7)
            anchor = {"left": "w", "center": "center", "right": "e"}[element.align]
            position_x = (
                left
                if element.align == "left"
                else right
                if element.align == "right"
                else (left + right) / 2
            )
            self.canvas.create_text(
                position_x,
                (top + bottom) / 2 if anchor == "center" else top + 2,
                text=element.text or "（空）",
                fill=element.color,
                font=("Segoe UI", font_px, "bold" if element.bold else "normal"),
                anchor="center" if anchor == "center" else ("w" if anchor == "w" else "e"),
                width=max(int(right - left), 10),
            )

    def _draw_handles(self, element: Element) -> None:
        x, y, w, h = element_rect(element, self.card)
        left, top, right, bottom = self._rect_to_canvas(x, y, w, h)
        positions = {
            "nw": (left, bottom),
            "n": ((left + right) / 2, bottom),
            "ne": (right, bottom),
            "e": (right, (top + bottom) / 2),
            "se": (right, top),
            "s": ((left + right) / 2, top),
            "sw": (left, top),
            "w": (left, (top + bottom) / 2),
        }
        self._handle_boxes.clear()
        for name, (cx, cy) in positions.items():
            self.canvas.create_rectangle(
                cx - HANDLE_PX,
                cy - HANDLE_PX,
                cx + HANDLE_PX,
                cy + HANDLE_PX,
                fill="#0B5FFF",
                outline="#FFFFFF",
            )
            self._handle_boxes[name] = (
                cx - HANDLE_PX,
                cy - HANDLE_PX,
                cx + HANDLE_PX,
                cy + HANDLE_PX,
            )

    # -- interaction ------------------------------------------------------
    def _on_press(self, event: tk.Event) -> None:
        for name, (x0, y0, x1, y1) in self._handle_boxes.items():
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                self._drag = {"mode": "resize", "handle": name, "last": (event.x, event.y)}
                return
        fx, fy = self._to_face(event.x, event.y)
        hit = hit_test(self._face(), self.card, fx, fy)
        self._select(hit)
        if hit is not None:
            self._drag = {"mode": "move", "last": (event.x, event.y)}

    def _on_motion(self, event: tk.Event) -> None:
        if self._drag is None or self.selected is None:
            return
        last_x, last_y = self._drag["last"]
        dx_mm = (event.x - last_x) / SCALE
        dy_mm = -(event.y - last_y) / SCALE
        self._drag["last"] = (event.x, event.y)
        if self._drag["mode"] == "move":
            move_element(self.selected, dx_mm, dy_mm, self.card, allow_bleed=True)
        else:
            resize_element(
                self.selected, self._drag["handle"], dx_mm, dy_mm, self.card, allow_bleed=True
            )
        self._draw()
        self._sync_props()

    def _on_release(self, _event: tk.Event) -> None:
        if self._drag is not None:
            self._drag = None
            self._snap_selected()
            self._draw()
            self._sync_props()

    def _on_double(self, event: tk.Event) -> None:
        fx, fy = self._to_face(event.x, event.y)
        element = hit_test(self._face(), self.card, fx, fy)
        if element is None or element.type != "text":
            return
        text = simpledialog.askstring(
            "编辑文字", "文字内容（可用 {call} 等占位符）", initialvalue=element.text, parent=self
        )
        if text is not None:
            element.text = text
            self._draw()
            self._sync_props()

    def _snap_selected(self) -> None:
        element = self.selected
        if element is None:
            return
        move_element(element, 0.0, 0.0, self.card, allow_bleed=True, step=SNAP_MM)

    def _nudge(self, dx: float, dy: float) -> None:
        if self.selected is None:
            return
        move_element(self.selected, dx, dy, self.card, allow_bleed=True)
        self._draw()
        self._sync_props()

    # -- element list -----------------------------------------------------
    def _refresh_list(self) -> None:
        self.listbox.delete(0, "end")
        for index, element in enumerate(self._face().elements):
            label = ELEMENT_LABELS.get(element.type, element.type)
            detail = (
                element.text or Path(element.image).name
                if element.type in ("text", "image")
                else ""
            )
            self.listbox.insert("end", f"{index + 1}. {label} {detail}".strip())
        if self.selected in self._face().elements:
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(self._face().elements.index(self.selected))

    def _on_list_select(self, _event: tk.Event) -> None:
        selection = self.listbox.curselection()
        if not selection:
            return
        elements = self._face().elements
        if selection[0] < len(elements):
            self._select(elements[selection[0]])

    def _select(self, element: Element | None) -> None:
        self.selected = element
        self._refresh_list()
        self._sync_props()
        self._draw()

    def _add(self, kind: str) -> None:
        image = ""
        if kind == "image":
            chosen = filedialog.askopenfilename(
                title="选择图片素材",
                filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"), ("所有文件", "*.*")],
            )
            if not chosen:
                return
            image = chosen
        element = add_element(self._face(), kind, self.card, image=image)
        self._select(element)
        self._refresh_list()

    def _delete(self) -> None:
        if self.selected is None:
            return
        face = self._face()
        if self.selected in face.elements:
            face.elements.remove(self.selected)
        self.selected = None
        self._refresh_list()
        self._sync_props()
        self._draw()

    def _reorder(self, delta: int) -> None:
        if self.selected is None:
            return
        elements = self._face().elements
        index = elements.index(self.selected)
        new_index = max(0, min(len(elements) - 1, index + delta))
        if new_index != index:
            elements.insert(new_index, elements.pop(index))
        self._refresh_list()
        self._draw()

    # -- properties -------------------------------------------------------
    def _sync_props(self) -> None:
        element = self.selected
        if element is None:
            for key, variable in self.props.items():
                if key == "bold":
                    variable.set(False)
                else:
                    variable.set("")
            self.image_note.configure(text="")
            return
        x, y, w, h = element_rect(element, self.card)
        self.props["x"].set(f"{element.x_mm:g}")
        self.props["y"].set(f"{element.y_mm:g}")
        self.props["w"].set(f"{w:g}")
        self.props["h"].set(f"{h:g}")
        self.props["text"].set(element.text)
        self.props["size"].set(f"{element.size_pt:g}")
        self.props["align"].set(element.align)
        self.props["color"].set(element.color)
        self.props["bold"].set(element.bold)
        self.props["image"].set(element.image)
        self._refresh_image_note()

    def _refresh_image_note(self) -> None:
        element = self.selected
        if element is None or element.type != "image" or not element.image:
            self.image_note.configure(text="")
            return
        x, y, w, h = element_rect(element, self.card)
        report = image_report(element.image, w, h, self.app.config.print.min_image_dpi)
        parts = [Path(element.image).name]
        if report.pixels:
            parts.append(f"{report.pixels[0]}x{report.pixels[1]} px")
        if report.dpi:
            parts.append(f"约 {report.dpi:.0f} DPI")
        self.image_note.configure(
            text=" · ".join(parts) + ("\n" + "\n".join(report.warnings) if report.warnings else ""),
            foreground="#B00020" if report.warnings else "#2E7D32",
        )

    def _insert_placeholder(self, value: str) -> None:
        current = self.props["text"].get()
        self.props["text"].set((current + " " + value).strip())

    def _pick_colour(self) -> None:
        chosen = colorchooser.askcolor(color=self.props["color"].get() or "#000000", parent=self)
        if chosen and chosen[1]:
            self.props["color"].set(chosen[1].upper())

    def _pick_image(self) -> None:
        if self.selected is None or self.selected.type != "image":
            messagebox.showinfo("提示", "请先选择一个图片元素（或点「+ 图片」）。", parent=self)
            return
        chosen = filedialog.askopenfilename(
            title="选择图片素材",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"), ("所有文件", "*.*")],
        )
        if not chosen:
            return
        if not is_supported_image(chosen):
            messagebox.showwarning(
                "格式提醒", "该文件不是常见图片格式，渲染时可能无法嵌入。", parent=self
            )
        self.props["image"].set(chosen)
        self._apply_props()

    def _full_bleed(self) -> None:
        if self.selected is None:
            return
        bx, by, bw, bh = full_bleed_rect(self.card)
        element = self.selected
        element.x_mm, element.y_mm, element.w_mm, element.h_mm = bx, by, bw, bh
        self._draw()
        self._sync_props()

    def _apply_props(self) -> None:
        element = self.selected
        if element is None:
            return

        def number(key: str, fallback: float) -> float:
            try:
                return float(self.props[key].get())
            except (TypeError, ValueError):
                return fallback

        x, y, w, h = element_rect(element, self.card)
        element.x_mm = number("x", element.x_mm)
        element.y_mm = number("y", element.y_mm)
        if element.type != "line":
            element.w_mm = max(number("w", w), 1.0)
            element.h_mm = max(number("h", h), 1.0)
        element.text = self.props["text"].get()
        element.size_pt = max(number("size", element.size_pt), 4.0)
        element.align = self.props["align"].get() or "left"
        element.color = self.props["color"].get() or "#000000"
        element.bold = bool(self.props["bold"].get())
        if element.type == "image":
            element.image = self.props["image"].get()
            if element.image and not element.h_mm:
                from .images import image_size_px

                size = image_size_px(element.image)
                if size and size[0]:
                    element.h_mm = round(element.w_mm * size[1] / size[0], 2)
        self._draw()
        self._refresh_list()
        self._refresh_image_note()

    # -- face level -------------------------------------------------------
    def _switch_face(self) -> None:
        self.face_index = int(self.face_var.get())
        self.selected = None
        self.bg_var.set(self._face().background_image)
        self.bg_fit.set(self._face().background_fit)
        self._refresh_list()
        self._sync_props()
        self._draw()

    def _pick_background(self) -> None:
        chosen = filedialog.askopenfilename(
            title="选择整面背景图片",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"), ("所有文件", "*.*")],
        )
        if not chosen:
            return
        face = self._face()
        face.background_image = chosen
        face.background_fit = self.bg_fit.get() or "cover"
        self.bg_var.set(chosen)
        note = image_report(
            chosen,
            self.card.doc_width_mm,
            self.card.doc_height_mm,
            self.app.config.print.min_image_dpi,
        )
        self.image_note.configure(
            text="背景图：" + " · ".join(note.warnings)
            if note.warnings
            else f"背景图已设置：{Path(chosen).name}",
            foreground="#B00020" if note.warnings else "#2E7D32",
        )
        self._draw()

    def _clear_background(self) -> None:
        face = self._face()
        face.background_image = ""
        self.bg_var.set("")
        self._draw()

    # -- template level ---------------------------------------------------
    def _reload_template(self) -> None:
        self.template = self._load(self.template_var.get())
        self.template_var.set(self.template.name)
        self.selected = None
        self.bg_var.set(self._face().background_image)
        self._build_canvas_refresh()
        self._refresh_list()
        self._draw()

    def _build_canvas_refresh(self) -> None:
        card = self.card
        self.canvas.configure(
            width=int(card.doc_width_mm * SCALE + 2 * PADDING),
            height=int(card.doc_height_mm * SCALE + 2 * PADDING),
        )

    def _save_as(self) -> None:
        name = simpledialog.askstring(
            "保存模板", "模板名称", initialvalue=self.template.name, parent=self
        )
        if not name:
            return
        target_dir = Path(self.app.config.template_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{name}.json"
        if path.exists() and not messagebox.askyesno(
            "覆盖", f"{path.name} 已存在，覆盖吗？", parent=self
        ):
            return
        self.template.name = name
        self.template.save(path)
        self.saved = True
        self.status.set(f"已保存模板：{path}")
        messagebox.showinfo("已保存", f"模板已保存到\n{path}", parent=self)

    def _use_as_current(self) -> None:
        from .config import save_config as write_config

        self.app.config.template = self.template.name
        config_path = getattr(self.app, "config_path", "")
        if config_path:
            write_config(self.app.config, config_path)
        self.status.set(f"当前模板已设为：{self.template.name}")
        self.saved = True
