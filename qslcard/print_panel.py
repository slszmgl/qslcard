"""Commercial print parameters panel (SRS CAL-007/008/009).

Everything is presented as a graphical preset plus plain-language notes, with
the specialist options folded away, because the SRS requires that a normal user
never has to understand prepress vocabulary to print a card.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from dataclasses import replace
from tkinter import messagebox, ttk

from .config import (
    PRINT_FIELD_NOTES,
    PrintConfig,
    apply_print_preset,
    describe_preset_changes,
    describe_print_settings,
)
from .geometry import paper_choices, paper_label, parse_paper

__all__ = ["PrintSettingsPanel"]

DPI_CHOICES = ("300", "600", "1200")


class PrintSettingsPanel(tk.Toplevel):
    """Modal panel that edits a PrintConfig without saving it itself."""

    def __init__(
        self,
        parent: tk.Misc,
        config: PrintConfig,
        *,
        on_saved: Callable[[PrintConfig], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.title("商业印刷参数")
        self.transient(parent)
        self.resizable(False, False)
        self.result: PrintConfig | None = None
        self._on_saved = on_saved
        self._current = replace(config)
        self._advanced_shown = False

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)

        self._build_presets(body)
        self._build_main(body)
        self._build_advanced(body)
        self._build_notes(body)
        self._build_buttons(body)

        self._load_into_widgets(self._current)
        self._refresh_notes()
        self.bind("<Escape>", lambda _event: self.destroy())
        self.grab_set()
        self.focus_set()

    # -- sections ---------------------------------------------------------
    def _build_presets(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="预设（点一下即可切换）", padding=(8, 6))
        frame.pack(fill="x")
        ttk.Button(
            frame,
            text="家用打印（推荐）",
            command=lambda: self._apply_preset("home"),
            width=18,
        ).pack(side="left")
        ttk.Button(
            frame,
            text="商业印刷",
            command=lambda: self._apply_preset("commercial"),
            width=14,
        ).pack(side="left", padx=(8, 0))
        self.preset_note = ttk.Label(
            frame, text="", wraplength=430, justify="left", foreground="#444444"
        )
        self.preset_note.pack(side="left", padx=(12, 0))

    def _build_main(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="输出（面向普通用户）", padding=(8, 6))
        frame.pack(fill="x", pady=(8, 0))

        self.pdf_type = tk.StringVar()
        ttk.Label(frame, text="文件类型").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Radiobutton(
            frame, text="通用 PDF（家用打印机）", variable=self.pdf_type, value="pdf"
        ).grid(row=0, column=1, sticky="w")
        ttk.Radiobutton(
            frame,
            text="PDF/X 风格（含成品框/出血框，送印刷厂）",
            variable=self.pdf_type,
            value="pdfx",
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))

        self.color = tk.StringVar()
        ttk.Label(frame, text="色彩模式").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Radiobutton(frame, text="RGB（屏幕/家用）", variable=self.color, value="RGB").grid(
            row=1, column=1, sticky="w"
        )
        ttk.Radiobutton(frame, text="CMYK（印刷机）", variable=self.color, value="CMYK").grid(
            row=1, column=2, sticky="w", padx=(8, 0)
        )

        self.dpi = tk.StringVar()
        ttk.Label(frame, text="分辨率").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Combobox(
            frame, textvariable=self.dpi, values=DPI_CHOICES, width=8, state="readonly"
        ).grid(row=2, column=1, sticky="w")
        ttk.Label(frame, text="DPI").grid(row=2, column=2, sticky="w", padx=(8, 0))

        self.paper = tk.StringVar()
        ttk.Label(frame, text="纸张").grid(row=3, column=0, sticky="w", pady=2)
        ttk.Combobox(
            frame, textvariable=self.paper, values=paper_choices(), width=22, state="readonly"
        ).grid(row=3, column=1, sticky="w")

        self.bleed = tk.StringVar()
        ttk.Label(frame, text="出血 (mm)").grid(row=4, column=0, sticky="w", pady=2)
        ttk.Entry(frame, textvariable=self.bleed, width=8).grid(row=4, column=1, sticky="w")

        self.duplex = tk.BooleanVar()
        ttk.Checkbutton(frame, text="双面（正反面拼版）", variable=self.duplex).grid(
            row=5, column=1, columnspan=2, sticky="w", pady=2
        )

        marks = ttk.Frame(frame)
        marks.grid(row=6, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.crop_marks = tk.BooleanVar()
        self.registration = tk.BooleanVar()
        self.color_bars = tk.BooleanVar()
        self.calibration = tk.BooleanVar()
        ttk.Checkbutton(marks, text="裁切线", variable=self.crop_marks).pack(side="left")
        ttk.Checkbutton(marks, text="套准标记", variable=self.registration).pack(
            side="left", padx=(10, 0)
        )
        ttk.Checkbutton(marks, text="色彩条", variable=self.color_bars).pack(
            side="left", padx=(10, 0)
        )
        ttk.Checkbutton(marks, text="3 mm 校准线", variable=self.calibration).pack(
            side="left", padx=(10, 0)
        )

        for variable in (
            self.pdf_type,
            self.color,
            self.dpi,
            self.paper,
            self.bleed,
            self.duplex,
            self.crop_marks,
            self.registration,
            self.color_bars,
            self.calibration,
        ):
            variable.trace_add("write", lambda *_args: self._refresh_notes())

    def _build_advanced(self, parent: ttk.Frame) -> None:
        self.advanced_button = ttk.Button(
            parent, text="显示高级参数 ▸", command=self._toggle_advanced, width=18
        )
        self.advanced_button.pack(anchor="w", pady=(8, 2))
        self.advanced_frame = ttk.LabelFrame(parent, text="高级参数（专业用户）", padding=(8, 6))

        self.gap = tk.StringVar()
        self.margin = tk.StringVar()
        self.mark_length = tk.StringVar()
        self.min_image_dpi = tk.StringVar()
        self.icc_path = tk.StringVar()

        rows = (
            ("卡片间距 (mm)", self.gap),
            ("纸张边距 (mm)", self.margin),
            ("标记长度 (mm)", self.mark_length),
            ("图片最低分辨率 (DPI)", self.min_image_dpi),
        )
        for index, (label, variable) in enumerate(rows):
            ttk.Label(self.advanced_frame, text=label).grid(row=index, column=0, sticky="w", pady=2)
            ttk.Entry(self.advanced_frame, textvariable=variable, width=10).grid(
                row=index, column=1, sticky="w"
            )

        ttk.Label(
            self.advanced_frame,
            text=PRINT_FIELD_NOTES["min_image_dpi"],
            wraplength=430,
            justify="left",
            foreground="#555555",
        ).grid(row=len(rows), column=0, columnspan=3, sticky="w", pady=(6, 0))

        self.icc_note = ttk.Label(
            self.advanced_frame,
            text=(
                "ICC 输出意图（PDF/X 认证所需）尚未实现：当前导出 PDF/X 风格文件，"
                "包含成品框/出血框、字体嵌入与 CMYK 色彩；如印刷厂要求正式 PDF/X-1a，"
                "请在预检工具中补入本厂 ICC 配置文件。"
            ),
            wraplength=430,
            justify="left",
            foreground="#8A4B00",
        )
        self.icc_note.grid(row=len(rows) + 1, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def _build_notes(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="当前设置说明", padding=(8, 6))
        frame.pack(fill="both", expand=True, pady=(8, 0))
        self.notes = tk.Text(
            frame, height=8, width=68, wrap="word", relief="flat", background="#F7F7F7"
        )
        self.notes.pack(fill="both", expand=True)
        self.notes.configure(state="disabled")

    def _build_buttons(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=(10, 0))
        ttk.Button(frame, text="保存", command=self._save, width=12).pack(side="right")
        ttk.Button(frame, text="取消", command=self.destroy, width=10).pack(
            side="right", padx=(0, 8)
        )
        ttk.Button(frame, text="恢复家用默认", command=self._restore_home, width=14).pack(
            side="left"
        )

    # -- behaviour --------------------------------------------------------
    def _toggle_advanced(self) -> None:
        self._advanced_shown = not self._advanced_shown
        if self._advanced_shown:
            self.advanced_frame.pack(fill="x", pady=(0, 0))
            self.advanced_button.configure(text="隐藏高级参数 ▾")
        else:
            self.advanced_frame.pack_forget()
            self.advanced_button.configure(text="显示高级参数 ▸")

    def _load_into_widgets(self, config: PrintConfig) -> None:
        self.pdf_type.set(config.pdf_type)
        self.color.set(config.color.upper())
        self.dpi.set(str(config.dpi))
        self.paper.set(paper_label(config.paper))
        self.bleed.set(f"{config.bleed_mm:g}")
        self.duplex.set(config.duplex)
        self.crop_marks.set(config.crop_marks)
        self.registration.set(config.registration_marks)
        self.color_bars.set(config.color_bars)
        self.calibration.set(config.calibration_lines)
        self.gap.set(f"{config.gap_mm:g}")
        self.margin.set(f"{config.margin_mm:g}")
        self.mark_length.set(f"{config.mark_length_mm:g}")
        self.min_image_dpi.set(str(config.min_image_dpi))

    def _collect(self) -> PrintConfig:
        def number(variable: tk.StringVar, fallback: float) -> float:
            try:
                return float(variable.get().strip())
            except (TypeError, ValueError):
                return fallback

        def whole(variable: tk.StringVar, fallback: int) -> int:
            try:
                return int(float(variable.get().strip()))
            except (TypeError, ValueError):
                return fallback

        base = self._current
        return replace(
            base,
            pdf_type=self.pdf_type.get(),
            color=self.color.get().upper(),
            dpi=whole(self.dpi, base.dpi),
            paper=parse_paper(self.paper.get()) or base.paper,
            bleed_mm=number(self.bleed, base.bleed_mm),
            duplex=bool(self.duplex.get()),
            crop_marks=bool(self.crop_marks.get()),
            registration_marks=bool(self.registration.get()),
            color_bars=bool(self.color_bars.get()),
            calibration_lines=bool(self.calibration.get()),
            gap_mm=number(self.gap, base.gap_mm),
            margin_mm=number(self.margin, base.margin_mm),
            mark_length_mm=number(self.mark_length, base.mark_length_mm),
            min_image_dpi=whole(self.min_image_dpi, base.min_image_dpi),
            preset="custom",
        )

    def _refresh_notes(self) -> None:
        config = self._collect()
        lines = [
            f"· {label}：{value} — {note}" for label, value, note in describe_print_settings(config)
        ]
        complexity = []
        if config.color == "CMYK":
            complexity.append("CMYK 输出：家用打印机可能偏色，适合送印刷厂。")
        if config.pdf_type == "pdfx":
            complexity.append("PDF/X 风格：文件更大，含成品框与出血框。")
        if config.registration_marks or config.color_bars:
            complexity.append("套准标记与色彩条印在余量里，成品上看不到。")
        if not config.calibration_lines:
            complexity.append("已关闭校准线：适合交付印刷厂的成品文件。")
        if complexity:
            lines.append("")
            lines.extend(f"· {item}" for item in complexity)
        self.notes.configure(state="normal")
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", "\n".join(lines))
        self.notes.configure(state="disabled")

    def _apply_preset(self, name: str) -> None:
        notes = describe_preset_changes(self._current, name)
        target = apply_print_preset(self._current, name)
        self._current = target
        self._load_into_widgets(target)
        self.preset_note.configure(
            text=f"已切换到「{'家用打印' if name == 'home' else '商业印刷'}」：" + " ".join(notes)
        )
        self._refresh_notes()

    def _restore_home(self) -> None:
        self._apply_preset("home")

    def _save(self) -> None:
        config = self._collect()
        if config.bleed_mm < 0 or config.gap_mm < 0 or config.margin_mm < 0:
            messagebox.showwarning("数值无效", "出血、间距与边距不能为负数。", parent=self)
            return
        if (
            config.pdf_type == "pdfx"
            and config.color == "RGB"
            and not messagebox.askyesno(
                "确认",
                "PDF/X 通常配合 CMYK 使用。仍要以 RGB 导出吗？\n\n"
                "（家用打印机选 RGB 没问题；送印刷厂建议改回 CMYK。）",
                parent=self,
            )
        ):
            return
        self.result = config
        if self._on_saved is not None:
            self._on_saved(config)
        self.destroy()
