"""Tkinter desktop GUI (SRS 3.3 and 11).

This is the primary interface of the packaged application, so it has to stand on
its own: a first-run wizard, a settings dialog, importing, filtering, rendering
and opening the result, without ever requiring a terminal.

Resource choices: Tkinter ships with CPython, starts in milliseconds and costs a
few megabytes, where a bundled browser runtime costs hundreds.  Long work runs
on a worker thread and reports back through a queue that the Tk main loop drains,
so Tk objects are only ever touched from the main thread.
"""

from __future__ import annotations

import csv
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from . import __version__
from .adif import iter_qsos
from .cards import plan_cards, summarize_cards
from .config import QSL_VIA_OPTIONS, PrintConfig, format_qsl_via, parse_qsl_via, save_config
from .geometry import LayoutError, paper_choices, paper_label, parse_paper
from .model import business_key
from .pdfout import CardRenderer, RenderOptions
from .privacy import mask_secret
from .rules import CardRule, select_qsos
from .store import ImportStats, QsoFilter
from .templates import available_templates, load_template

__all__ = ["QslCardWindow", "open_path", "run_gui"]

PAGE_SIZE = 200
APP_TITLE = "QSL 卡片打印系统"


def open_path(path: str | Path) -> bool:
    """Open a file or folder with the desktop default handler."""
    target = str(path)
    try:
        if sys.platform.startswith("win"):
            os.startfile(target)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
        return True
    except Exception:  # noqa: BLE001 - opening is best effort
        return False


class SettingsDialog(tk.Toplevel):
    """Modal editor for station, output and print defaults."""

    def __init__(self, parent: tk.Misc, app: Any) -> None:
        super().__init__(parent)
        self.app = app
        self.title("设置")
        self.transient(parent)
        self.resizable(False, False)
        self.saved = False

        station = app.config.station
        printing = app.config.print
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)

        self.vars: dict[str, tk.Variable] = {
            "callsign": tk.StringVar(value=station.callsign),
            "operator": tk.StringVar(value=station.operator or station.callsign),
            "name": tk.StringVar(value=station.name),
            "grid": tk.StringVar(value=station.grid),
            "qth": tk.StringVar(value=station.qth),
            "address": tk.StringVar(value=station.address),
            "email": tk.StringVar(value=station.email),
            "qsl_via": tk.StringVar(value=station.qsl_via or "BUREAU"),
            "output_dir": tk.StringVar(value=app.config.output_dir),
            "adif_dir": tk.StringVar(value=self._first_adif_dir()),
            "paper": tk.StringVar(value=paper_label(printing.paper)),
            "template": tk.StringVar(value=app.config.template),
            "duplex": tk.BooleanVar(value=printing.duplex),
            "calibration": tk.BooleanVar(value=printing.calibration_lines),
            "bleed": tk.StringVar(value=f"{printing.bleed_mm:g}"),
            "calibration_mm": tk.StringVar(value=f"{printing.calibration_mm:g}"),
        }

        rows: list[tuple[str, str, str]] = [
            ("台站呼号", "callsign", "entry"),
            ("操作员", "operator", "entry"),
            ("姓名", "name", "entry"),
            ("网格", "grid", "entry"),
            ("QTH", "qth", "entry"),
            ("邮寄地址", "address", "entry"),
            ("邮箱", "email", "entry"),
            ("QSL 经手（可多选）", "qsl_via", "qslvia_multi"),
            ("输出目录", "output_dir", "browse_dir"),
            ("ADIF 目录", "adif_dir", "browse_adif"),
            ("纸张", "paper", "paper"),
            ("默认模板", "template", "template"),
            ("出血 (mm)", "bleed", "entry"),
            ("校准线 (mm)", "calibration_mm", "entry"),
        ]
        for index, (label, key, kind) in enumerate(rows):
            ttk.Label(body, text=label).grid(row=index, column=0, sticky="w", pady=3, padx=(0, 8))
            if kind == "entry":
                ttk.Entry(body, textvariable=self.vars[key], width=38).grid(
                    row=index, column=1, sticky="we"
                )
            elif kind == "qslvia_multi":
                holder = ttk.Frame(body)
                holder.grid(row=index, column=1, sticky="we")
                chosen = set(parse_qsl_via(str(self.vars[key].get())))
                self.via_vars = {}
                for position, (option, option_label) in enumerate(QSL_VIA_OPTIONS):
                    flag = tk.BooleanVar(value=option in chosen)
                    self.via_vars[option] = flag
                    ttk.Checkbutton(holder, text=option_label, variable=flag).grid(
                        row=position // 2, column=position % 2, sticky="w", padx=(0, 12)
                    )
            elif kind == "paper":
                ttk.Combobox(
                    body,
                    textvariable=self.vars[key],
                    values=paper_choices(),
                    width=35,
                    state="readonly",
                ).grid(row=index, column=1, sticky="we")
            elif kind == "template":
                ttk.Combobox(
                    body,
                    textvariable=self.vars[key],
                    values=available_templates(),
                    width=35,
                    state="readonly",
                ).grid(row=index, column=1, sticky="we")
            elif kind in ("browse_dir", "browse_adif"):
                holder = ttk.Frame(body)
                holder.grid(row=index, column=1, sticky="we")
                ttk.Entry(holder, textvariable=self.vars[key], width=28).pack(side="left")
                ttk.Button(
                    holder,
                    text="浏览…",
                    width=8,
                    command=lambda k=key: self._browse_dir(k),
                ).pack(side="left", padx=(4, 0))

        options = ttk.Frame(body)
        options.grid(row=len(rows), column=0, columnspan=2, sticky="w", pady=(8, 4))
        ttk.Checkbutton(options, text="双面打印（正反面拼版）", variable=self.vars["duplex"]).pack(
            side="left"
        )
        ttk.Checkbutton(options, text="显示 3 mm 校准线", variable=self.vars["calibration"]).pack(
            side="left", padx=(12, 0)
        )

        note = ttk.Label(
            body,
            text="凭证只保存在本机（Windows 使用 DPAPI 加密），不会写入配置文件或上传。",
            foreground="#555555",
            wraplength=380,
            justify="left",
        )
        note.grid(row=len(rows) + 1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=len(rows) + 2, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="保存", command=self._save, width=10).pack(side="right")
        ttk.Button(buttons, text="取消", command=self.destroy, width=10).pack(
            side="right", padx=(0, 8)
        )

        body.columnconfigure(1, weight=1)
        self.bind("<Return>", lambda _event: self._save())
        self.bind("<Escape>", lambda _event: self.destroy())
        self.grab_set()
        self.focus_set()

    def _first_adif_dir(self) -> str:
        paths = self.app.config.sources.get("adif", {}).get("paths", [])
        return str(paths[0]) if paths else ""

    def _browse_dir(self, key: str) -> None:
        current = str(self.vars[key].get())
        chosen = filedialog.askdirectory(initialdir=current or str(Path.home()))
        if chosen:
            self.vars[key].set(chosen)

    def _save(self) -> None:
        station = self.app.config.station
        station.callsign = str(self.vars["callsign"].get()).strip().upper()
        station.operator = str(self.vars["operator"].get()).strip()
        station.name = str(self.vars["name"].get()).strip()
        station.grid = str(self.vars["grid"].get()).strip().upper()
        station.qth = str(self.vars["qth"].get()).strip()
        station.address = str(self.vars["address"].get()).strip()
        station.email = str(self.vars["email"].get()).strip()
        station.qsl_via = format_qsl_via(
            option for option, flag in getattr(self, "via_vars", {}).items() if flag.get()
        )

        printing = self.app.config.print
        printing.paper = parse_paper(str(self.vars["paper"].get())) or printing.paper
        printing.duplex = bool(self.vars["duplex"].get())
        printing.calibration_lines = bool(self.vars["calibration"].get())
        printing.bleed_mm = self._number("bleed", printing.bleed_mm)
        printing.calibration_mm = self._number("calibration_mm", printing.calibration_mm)

        output_dir = str(self.vars["output_dir"].get()).strip()
        if output_dir:
            self.app.config.output_dir = output_dir
        template = str(self.vars["template"].get()).strip()
        if template:
            self.app.config.template = template

        adif_dir = str(self.vars["adif_dir"].get()).strip()
        sources = dict(self.app.config.sources)
        adif_section = (
            dict(sources.get("adif", {})) if isinstance(sources.get("adif"), dict) else {}
        )
        adif_section["paths"] = [adif_dir] if adif_dir else []
        sources["adif"] = adif_section
        self.app.config.sources = sources

        if not station.callsign:
            messagebox.showwarning("需要呼号", "请填写台站呼号，生成的卡片需要它。", parent=self)
            return
        Path(self.app.config.output_dir).mkdir(parents=True, exist_ok=True)
        save_config(self.app.config, self.app.config_path)
        self.saved = True
        self.destroy()

    def _number(self, key: str, fallback: float) -> float:
        try:
            return float(str(self.vars[key].get()).strip())
        except ValueError:
            return fallback


class QslCardWindow:
    """Main window; domain state lives in the shared App object."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.root = tk.Tk()
        self.root.title(f"{APP_TITLE} {__version__}")
        self.root.geometry("1140x720")
        self.root.minsize(960, 580)
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._offset = 0
        self._total = 0
        self._build()
        self.refresh_stats()
        self.load_page(0)
        self.root.after(100, self._drain)
        self.root.after(200, self._first_run_check)

    # -- construction ----------------------------------------------------
    def _build(self) -> None:
        style = ttk.Style()
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        self._build_menu()
        self._build_toolbar()
        self._build_filters()
        self._build_table()
        self._build_status()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_menu(self) -> None:
        menu = tk.Menu(self.root)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="导入 ADIF 文件…", command=self.choose_adif)
        file_menu.add_command(label="导入 ADIF 目录…", command=self.choose_adif_dir)
        file_menu.add_separator()
        file_menu.add_command(label="打开输出目录", command=self.open_output_dir)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self._on_close)
        menu.add_cascade(label="文件", menu=file_menu)

        tools_menu = tk.Menu(menu, tearoff=False)
        tools_menu.add_command(label="生成卡片 PDF", command=self.generate)
        tools_menu.add_command(label="导出 CSV", command=self.export_csv)
        tools_menu.add_separator()
        tools_menu.add_command(label="发卡记录表…", command=self.open_card_log)
        tools_menu.add_command(label="登记发卡（当前筛选）…", command=self.record_dispatch_now)
        tools_menu.add_command(label="从 QRZ / LoTW 拉取日志", command=self.pull_logbooks)
        tools_menu.add_separator()
        tools_menu.add_command(label="刷新台账", command=self.reload)
        menu.add_cascade(label="操作", menu=tools_menu)

        settings_menu = tk.Menu(menu, tearoff=False)
        settings_menu.add_command(label="台站与打印设置…", command=self.open_settings)
        settings_menu.add_command(label="数据源与凭证…", command=self.open_credentials)
        settings_menu.add_command(label="商业印刷参数…", command=self.open_print_panel)
        settings_menu.add_command(label="卡片设计器…", command=self.open_designer)
        settings_menu.add_separator()
        settings_menu.add_command(label="隐私与凭证…", command=self.show_privacy)
        settings_menu.add_separator()
        settings_menu.add_command(label="关于", command=self.show_about)
        menu.add_cascade(label="设置", menu=settings_menu)
        self.root.config(menu=menu)

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(fill="x")
        ttk.Button(bar, text="导入 ADIF", command=self.choose_adif).pack(side="left")
        ttk.Button(bar, text="设置", command=self.open_settings).pack(side="left", padx=4)
        ttk.Button(bar, text="刷新", command=self.reload).pack(side="left")
        ttk.Button(bar, text="印刷参数", command=self.open_print_panel).pack(
            side="left", padx=(12, 0)
        )
        ttk.Button(bar, text="卡片设计器", command=self.open_designer).pack(side="left", padx=4)
        ttk.Button(bar, text="发卡记录", command=self.open_card_log).pack(side="left", padx=4)
        ttk.Button(bar, text="数据源凭证", command=self.open_credentials).pack(side="left", padx=4)
        ttk.Button(bar, text="拉取日志", command=self.pull_logbooks).pack(side="left", padx=4)

        ttk.Label(bar, text="模板").pack(side="left", padx=(16, 2))
        self.template_var = tk.StringVar(value=self.app.config.template)
        ttk.Combobox(
            bar,
            textvariable=self.template_var,
            values=available_templates(),
            width=10,
            state="readonly",
        ).pack(side="left")

        ttk.Label(bar, text="纸张").pack(side="left", padx=(12, 2))
        self.paper_var = tk.StringVar(value=paper_label(self.app.config.print.paper))
        ttk.Combobox(
            bar, textvariable=self.paper_var, values=paper_choices(), width=20, state="readonly"
        ).pack(side="left")

        self.duplex_var = tk.BooleanVar(value=self.app.config.print.duplex)
        ttk.Checkbutton(bar, text="双面", variable=self.duplex_var).pack(side="left", padx=(12, 0))
        self.guides_var = tk.BooleanVar(value=self.app.config.print.calibration_lines)
        ttk.Checkbutton(bar, text="校准线", variable=self.guides_var).pack(side="left", padx=(8, 0))

        ttk.Button(bar, text="生成卡片 PDF", command=self.generate).pack(side="right")
        ttk.Button(bar, text="导出 CSV", command=self.export_csv).pack(side="right", padx=4)

    def _build_filters(self) -> None:
        frame = ttk.LabelFrame(self.root, text="筛选（待制卡规则）", padding=(8, 4))
        frame.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(frame, text="呼号前缀").pack(side="left")
        self.prefix_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.prefix_var, width=10).pack(side="left", padx=(4, 12))
        ttk.Label(frame, text="波段").pack(side="left")
        self.band_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.band_var, width=8).pack(side="left", padx=(4, 12))
        ttk.Label(frame, text="模式").pack(side="left")
        self.mode_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.mode_var, width=8).pack(side="left", padx=(4, 12))
        self.needs_card_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="仅未确认且未发卡", variable=self.needs_card_var).pack(
            side="left"
        )
        ttk.Button(frame, text="应用筛选", command=self.reload).pack(side="left", padx=12)

    def _build_table(self) -> None:
        frame = ttk.Frame(self.root)
        frame.pack(fill="both", expand=True, padx=8)
        columns = ("call", "date", "time", "band", "mode", "rst", "country", "name", "sent", "lotw")
        headings = {
            "call": "呼号",
            "date": "日期",
            "time": "UTC",
            "band": "波段",
            "mode": "模式",
            "rst": "RST",
            "country": "实体",
            "name": "姓名",
            "sent": "已发卡",
            "lotw": "LoTW",
        }
        widths = {
            "call": 100,
            "date": 92,
            "time": 62,
            "band": 60,
            "mode": 70,
            "rst": 62,
            "country": 120,
            "name": 120,
            "sent": 62,
            "lotw": 62,
        }
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", height=18)
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="w", stretch=False)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _build_status(self) -> None:
        bar = ttk.Frame(self.root, padding=(8, 4))
        bar.pack(fill="x")
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.status_var).pack(side="left")
        self.page_var = tk.StringVar(value="0 / 0")
        ttk.Button(
            bar, text="上一页", command=lambda: self.load_page(self._offset - PAGE_SIZE)
        ).pack(side="right")
        ttk.Label(bar, textvariable=self.page_var).pack(side="right", padx=6)
        ttk.Button(
            bar, text="下一页", command=lambda: self.load_page(self._offset + PAGE_SIZE)
        ).pack(side="right")

    # -- data ------------------------------------------------------------
    def _filter(self) -> QsoFilter:
        bands = tuple(b.strip() for b in self.band_var.get().split(",") if b.strip())
        modes = tuple(m.strip() for m in self.mode_var.get().split(",") if m.strip())
        return QsoFilter(
            call_prefix=self.prefix_var.get().strip(),
            band=bands,
            mode=modes,
            needs_card=True if self.needs_card_var.get() else None,
        )

    def refresh_stats(self) -> None:
        info = self.app.store.stats()
        self.status_var.set(
            f"共 {info['total']} 条 QSO，已确认 {info['confirmed']}，待制卡 {info['needs_card']}，"
            f"呼号 {info['calls']} 个    |    {self.app.config.station.callsign or '未设置呼号'}"
        )

    def reload(self) -> None:
        self.refresh_stats()
        self.load_page(0)

    def load_page(self, offset: int) -> None:
        offset = max(offset, 0)
        self._offset = offset
        flt = self._filter()
        self._total = self.app.store.count(flt)
        flt.limit = PAGE_SIZE
        flt.offset = offset
        self.tree.delete(*self.tree.get_children())
        for qso in self.app.store.query(flt):
            self.tree.insert(
                "",
                "end",
                values=(
                    qso.call,
                    qso.qso_date,
                    qso.time_on,
                    qso.band,
                    qso.mode,
                    f"{qso.rst_sent}/{qso.rst_rcvd}",
                    qso.country,
                    qso.name,
                    qso.qsl_sent,
                    qso.lotw_qsl_rcvd,
                ),
            )
        pages = max((self._total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
        self.page_var.set(f"{offset // PAGE_SIZE + 1} / {pages}")

    # -- background work --------------------------------------------------
    def _background(self, work: Callable[[], Any], done: str) -> None:
        def target() -> None:
            try:
                self._queue.put((done, work()))
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                self._queue.put(("error", exc))

        threading.Thread(target=target, daemon=True).start()

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "error":
                    self.status_var.set("出错")
                    messagebox.showerror("出错", str(payload))
                elif kind == "imported":
                    self.status_var.set(str(payload))
                    self.reload()
                    messagebox.showinfo("导入完成", str(payload))
                elif kind == "generated":
                    self.status_var.set(str(payload.get("summary", "已生成")))
                    self.refresh_stats()
                    self._offer_open(payload)
                    self._offer_dispatch(payload)
                elif kind == "pulled":
                    self.status_var.set("在线日志拉取完成")
                    self.reload()
                    messagebox.showinfo("拉取日志", str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def _offer_open(self, payload: dict[str, Any]) -> None:
        path = str(payload.get("path", ""))
        summary = str(payload.get("summary", ""))
        warnings = payload.get("warnings") or []
        text = summary
        if warnings:
            text += "\n\n注意：\n" + "\n".join(f"- {w}" for w in warnings)
        if (
            path
            and messagebox.askyesno("生成完成", f"{text}\n\n是否立即打开 PDF？")
            and not open_path(path)
        ):
            messagebox.showinfo("提示", f"文件已生成：\n{path}")

    def _offer_dispatch(self, payload: dict[str, Any]) -> None:
        """Offer the one-click dispatch log right after cards are generated."""
        keys = payload.get("keys") or []
        if not keys:
            return
        if not messagebox.askyesno(
            "登记发卡",
            f"是否把这 {len(keys)} 条记录登记为已发卡？\n\n"
            "可顺便把 QSL Card Sent 日期回写到 QRZ Logbook。",
        ):
            return
        from .cardlog_panel import ask_dispatch

        qsos = self.app.store.get_many(keys)
        message = ask_dispatch(
            self.root, self.app, qsos, batch_id=int(payload.get("batch", 0) or 0)
        )
        self.status_var.set(message.replace("\n", " "))
        self.refresh_stats()

    # -- actions ----------------------------------------------------------
    def _first_run_check(self) -> None:
        if not self.app.config.station.callsign and messagebox.askyesno(
            "首次使用",
            "还没有设置台站呼号，卡片上需要它。\n\n现在打开设置向导吗？",
        ):
            self.open_settings()

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.root, self.app)
        self.root.wait_window(dialog)
        if dialog.saved:
            self.template_var.set(self.app.config.template)
            self.paper_var.set(self.app.config.print.paper)
            self.refresh_stats()
            self.status_var.set("设置已保存")

    def open_print_panel(self) -> None:
        """Open the commercial print parameters panel (SRS CAL-007)."""
        from .print_panel import PrintSettingsPanel

        def saved(config: Any) -> None:
            self.app.config.print = config
            self.paper_var.set(paper_label(config.paper))
            self.duplex_var.set(config.duplex)
            self.guides_var.set(config.calibration_lines)
            if getattr(self.app, "config_path", ""):
                save_config(self.app.config, self.app.config_path)
            self.status_var.set(
                f"印刷参数已更新：{config.pdf_type.upper()} / "
                f"{config.color.upper()} / {config.dpi} DPI"
            )

        panel = PrintSettingsPanel(self.root, self.app.config.print, on_saved=saved)
        self.root.wait_window(panel)

    def open_card_log(self) -> None:
        """Open the dispatch table (SRS 6.7)."""
        from .cardlog_panel import CardLogPanel

        panel = CardLogPanel(self.root, self.app)
        self.root.wait_window(panel)
        self.refresh_stats()
        self.load_page(self._offset)

    def record_dispatch_now(self) -> None:
        """Quickly log the cards matching the current filter as posted."""
        from .cardlog_panel import ask_dispatch

        qsos = list(self.app.store.iter_qsos(self._filter()))
        if not qsos:
            messagebox.showinfo("提示", "当前筛选没有记录。")
            return
        if len(qsos) > 500:
            messagebox.showinfo("提示", "记录过多，请先用筛选缩小范围（最多 500 条）。")
            return
        message = ask_dispatch(self.root, self.app, qsos)
        self.status_var.set(message.replace("\n", " "))
        self.reload()

    def open_credentials(self) -> None:
        """Enter or update QRZ / LoTW / Club Log credentials (SRS chapter 18)."""
        from .credentials_panel import open_credentials

        dialog = open_credentials(self.root, self.app)
        if dialog is not None and dialog.saved:
            self.status_var.set("凭证已保存到本机凭证库")
            self.show_privacy_hint = True

    def pull_logbooks(self) -> None:
        """Pull the QRZ logbook (API) and LoTW confirmations (TQSL)."""
        from .tracking import pull_logbook

        self.status_var.set("正在拉取在线日志…")

        def work() -> str:
            messages: list[str] = []
            for name in ("qrz", "lotw"):
                try:
                    outcome = pull_logbook(self.app.store, name, self.app.source_context(name))
                    messages.extend(outcome.messages)
                except Exception as exc:  # noqa: BLE001 - reported in the dialog
                    messages.append(f"{name}：失败：{exc}")
            if any("未配置" in message for message in messages):
                messages.append("提示：请在「设置 → 数据源与凭证…」中填写账号或密钥后重试。")
            return "\n".join(messages)

        self._background(work, "pulled")

    def open_designer(self) -> None:
        """Open the graphical card designer (SRS section 7)."""
        from .designer import CardDesigner

        designer = CardDesigner(self.root, self.app, self.app.config.template)
        self.root.wait_window(designer)
        self.template_var.set(self.app.config.template)
        self.status_var.set("设计器已关闭")

    def choose_adif(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择 ADIF 文件",
            filetypes=[("ADIF 日志", "*.adi *.adif *.adx"), ("所有文件", "*.*")],
        )
        if paths:
            self._import(list(paths))

    def choose_adif_dir(self) -> None:
        chosen = filedialog.askdirectory(title="选择包含 ADIF 的目录")
        if chosen:
            self._import([chosen])

    def _import(self, paths: list[str]) -> None:
        self.status_var.set("正在导入…")

        def work() -> str:
            errors: list[dict[str, Any]] = []
            from .sources.adif_file import collect_adif_paths

            files = collect_adif_paths(paths)
            if not files:
                return "所选位置没有找到 ADIF 文件（*.adi / *.adif / *.adx）"

            def records():
                for path in files:
                    yield from iter_qsos(path, errors)

            stats = self.app.store.import_qsos(records(), strategy="merge", source="adif")
            self.app.store.record_batch("import", "adif", stats)
            message = (
                f"读取 {len(files)} 个文件：共 {stats.total} 条，新增 {stats.inserted}，"
                f"更新 {stats.updated}，跳过 {stats.skipped}"
            )
            if errors:
                message += f"\n\n有 {len(errors)} 处 ADIF 长度字段有误，已按分隔符自动修正。"
            return message

        self._background(work, "imported")

    def generate(self) -> None:
        template_name = self.template_var.get()
        paper = parse_paper(self.paper_var.get()) or self.app.config.print.paper
        duplex = self.duplex_var.get()
        guides = self.guides_var.get()
        if not self.app.config.station.callsign:
            messagebox.showwarning("需要呼号", "请先在「设置」里填写台站呼号。")
            return
        self.status_var.set("正在生成…")

        def work() -> dict[str, Any]:
            template = load_template(template_name, self.app.config.template_dir)
            rule = CardRule.from_dict(self.app.config.rules.get("needs_card"), name="needs_card")
            if self.prefix_var.get().strip():
                rule.call_prefix = self.prefix_var.get().strip()
            bands = tuple(b.strip() for b in self.band_var.get().split(",") if b.strip())
            if bands:
                rule.bands = bands
            modes = tuple(m.strip() for m in self.mode_var.get().split(",") if m.strip())
            if modes:
                rule.modes = modes
            if not self.needs_card_var.get():
                rule.require_unconfirmed = False
                rule.require_unsent = False
            selected = list(select_qsos(self.app.store, rule, self.app.rule_context(rule)))
            if not selected:
                return {"summary": "没有符合规则的记录", "path": "", "warnings": []}
            cards = plan_cards(selected, group_by="call", max_per_card=4)
            print_config = self.app.config.print
            if paper != print_config.paper:
                print_config = PrintConfig(**{**_print_dict(print_config), "paper": paper})
            options = RenderOptions(
                template=template,
                station=self.app.config.station,
                print_config=print_config,
                duplex=duplex,
                calibration_lines=guides,
            )
            out = Path(self.app.config.output_dir) / f"cards-{template_name}-{len(cards)}.pdf"
            try:
                result = CardRenderer(options).render(cards, out)
            except LayoutError as exc:
                return {"summary": f"拼版失败：{exc}", "path": "", "warnings": []}
            batch = self.app.store.record_batch(
                "cards", template_name, ImportStats(total=len(selected))
            )
            self.app.store.record_items(batch, [business_key(q) for c in cards for q in c.qsos])
            summary = summarize_cards(cards)
            return {
                "summary": (
                    f"已生成 {summary['cards']} 张卡片，{result.sheets} 张纸、{result.pages} 页\n"
                    f"{result.path}"
                ),
                "path": result.path,
                "warnings": list(result.warnings),
                "batch": batch,
                "keys": [business_key(q) for card in cards for q in card.qsos],
            }

        self._background(work, "generated")

    def export_csv(self) -> None:
        target = filedialog.asksaveasfilename(
            title="导出 CSV", defaultextension=".csv", filetypes=[("CSV", "*.csv")]
        )
        if not target:
            return
        from .cli import CSV_COLUMNS

        qsos = list(self.app.store.iter_qsos(self._filter()))
        with open(target, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
            writer.writeheader()
            for qso in qsos:
                writer.writerow({name: getattr(qso, name, "") for name in CSV_COLUMNS})
        messagebox.showinfo("导出完成", f"已导出 {len(qsos)} 条记录到\n{target}")
        open_path(Path(target).parent)

    def open_output_dir(self) -> None:
        target = Path(self.app.config.output_dir)
        target.mkdir(parents=True, exist_ok=True)
        if not open_path(target):
            messagebox.showinfo("输出目录", str(target))

    def show_privacy(self) -> None:
        from .privacy import DEFAULT_ALLOWED_HOSTS

        vault = self.app.vault
        backend = "-" if vault is None else vault.backend
        count = 0 if vault is None else len(vault.load())
        lines = [
            f"凭证后端：{backend}（仅本机，Windows 使用 DPAPI 加密）",
            f"已保存凭证：{count} 项",
            f"网络模式：{'离线' if self.app.config.offline else '在线（仅白名单域名）'}",
            f"允许域名：{len(DEFAULT_ALLOWED_HOSTS)} 个官方端点",
            "遥测：无",
            "新增或修改凭证：菜单「设置 → 数据源与凭证…」",
            f"数据目录：{Path(self.app.config.database).parent}",
        ]
        if vault is not None and count:
            masked = vault.masked()
            lines.append("")
            lines.append("已保存的凭证名（值已掩码）：")
            for name, value in list(masked.items())[:10]:
                lines.append(f"  {name} = {mask_secret(value)}")
        messagebox.showinfo("隐私与凭证", "\n".join(lines))

    def show_about(self) -> None:
        messagebox.showinfo(
            "关于",
            f"{APP_TITLE} {__version__}\n\n"
            "按《QSL 卡片打印系统 需求规格说明书》实现。\n"
            "默认卡片 90 x 140 mm，四边 3 mm 出血，成品线内侧 3 mm 校准线。\n"
            "核心仅依赖标准库，PDF 引擎按需加载。",
        )

    def _on_close(self) -> None:
        self.app.close()
        self.root.destroy()

    def run(self) -> int:
        self.root.mainloop()
        return 0


def _print_dict(config: PrintConfig) -> dict[str, Any]:
    return {
        "paper": config.paper,
        "preset": config.preset,
        "duplex": config.duplex,
        "bleed_mm": config.bleed_mm,
        "gap_mm": config.gap_mm,
        "margin_mm": config.margin_mm,
        "dpi": config.dpi,
        "color": config.color,
        "card_width_mm": config.card_width_mm,
        "card_height_mm": config.card_height_mm,
        "calibration_lines": config.calibration_lines,
        "calibration_mm": config.calibration_mm,
        "crop_marks": config.crop_marks,
    }


def run_gui(app: Any) -> int:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("未找到 tkinter，无法启动图形界面；请使用命令行子命令。")
        return 1
    return QslCardWindow(app).run()
