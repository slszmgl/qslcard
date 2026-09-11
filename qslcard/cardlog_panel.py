"""QSL card dispatch table (SRS 6.6 and 6.7).

Two entry points share this module: the standalone table for browsing and
updating history, and a small form used right after cards are generated so
recording a dispatch takes seconds rather than a spreadsheet.
"""

from __future__ import annotations

import tkinter as tk
from datetime import date
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .model import QSO
from .store import CARD_STATUSES
from .tracking import card_log_csv, qrz_pusher, record_dispatch, record_returns

__all__ = [
    "CardLogPanel",
    "DispatchDialog",
    "apply_dispatch",
    "apply_return",
    "ask_dispatch",
    "ask_return",
]

VIA_CHOICES = ("BURO", "DIRECT", "OQRS", "BUREAU/DIRECT", "NONE")

STATUS_LABELS = {
    "queued": "待发",
    "printed": "已打印",
    "sent": "已发出",
    "returned": "已回卡",
    "bounced": "退件",
    "ignored": "已忽略",
}

COLUMNS = (
    ("call", "呼号", 100),
    ("qso_date", "通联日期", 90),
    ("band", "波段", 60),
    ("mode", "模式", 66),
    ("sent_date", "发卡日期", 90),
    ("sent_via", "方式", 74),
    ("status", "状态", 74),
    ("returned_date", "回卡日期", 90),
    ("batch_id", "批次", 56),
    ("note", "备注", 150),
)


def today_compact() -> str:
    return date.today().strftime("%Y%m%d")


class DispatchDialog(tk.Toplevel):
    """Small form for a dispatch or a return; returns a dict or None."""

    def __init__(
        self,
        parent: tk.Misc,
        title: str,
        *,
        default_via: str = "BURO",
        default_date: str = "",
        push_label: str = "同时回写 QRZ Logbook（QSL Card Sent 日期）",
        show_push: bool = True,
    ) -> None:
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.result: dict[str, Any] | None = None

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)

        self.date_var = tk.StringVar(value=default_date or today_compact())
        self.via_var = tk.StringVar(value=default_via or "BURO")
        self.note_var = tk.StringVar()
        self.push_var = tk.BooleanVar(value=False)

        ttk.Label(body, text="日期 (YYYYMMDD)").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(body, textvariable=self.date_var, width=18).grid(row=0, column=1, sticky="w")
        ttk.Label(body, text="寄送方式").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Combobox(
            body, textvariable=self.via_var, values=list(VIA_CHOICES), width=16, state="readonly"
        ).grid(row=1, column=1, sticky="w")
        ttk.Label(body, text="备注").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(body, textvariable=self.note_var, width=34).grid(
            row=2, column=1, columnspan=2, sticky="we"
        )
        if show_push:
            ttk.Checkbutton(body, text=push_label, variable=self.push_var).grid(
                row=3, column=0, columnspan=3, sticky="w", pady=(8, 0)
            )
        ttk.Label(
            body,
            text=(
                "回写只发送呼号/日期/波段/模式与 QSL 状态字段，"
                "不会上传地址或联系方式；API Key 只保存在本机。"
            ),
            wraplength=380,
            justify="left",
            foreground="#555555",
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=5, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="确定", width=10, command=self._ok).pack(side="right")
        ttk.Button(buttons, text="取消", width=10, command=self.destroy).pack(
            side="right", padx=(0, 8)
        )

        self.bind("<Return>", lambda _event: self._ok())
        self.bind("<Escape>", lambda _event: self.destroy())
        self.grab_set()
        self.focus_set()

    def _ok(self) -> None:
        text = self.date_var.get().strip()
        if not (len(text) == 8 and text.isdigit()):
            messagebox.showwarning(
                "日期格式", "请按 YYYYMMDD 填写日期，例如 20260912。", parent=self
            )
            return
        self.result = {
            "date": text,
            "via": self.via_var.get().strip(),
            "note": self.note_var.get().strip(),
            "push": bool(self.push_var.get()),
        }
        self.destroy()


def apply_dispatch(app: Any, qsos: list[QSO], info: dict[str, Any], *, batch_id: int = 0) -> str:
    """Record a dispatch from already-collected form values.

    Separated from the dialog so the behaviour can be tested without a display.
    """
    if not qsos:
        return "没有可登记的记录"
    pusher = None
    if info.get("push"):
        pusher = qrz_pusher(
            app.source_context("qrz"),
            sent_date=str(info.get("date", "")),
            via=str(info.get("via", "")),
            received=False,
        )
    outcome = record_dispatch(
        app.store,
        qsos,
        sent_date=str(info.get("date", "")),
        via=str(info.get("via", "")),
        batch_id=batch_id,
        note=str(info.get("note", "")),
        pusher=pusher,
    )
    message = outcome.summary
    if outcome.warnings:
        message += "\n" + "\n".join(outcome.warnings)
    return message


def apply_return(app: Any, qsos: list[QSO], info: dict[str, Any]) -> str:
    """Record reply cards from already-collected form values."""
    if not qsos:
        return "没有可登记的记录"
    pusher = None
    if info.get("push"):
        pusher = qrz_pusher(
            app.source_context("qrz"),
            sent_date=str(info.get("date", "")),
            via=str(info.get("via", "")),
            received=True,
        )
    outcome = record_returns(
        app.store,
        qsos,
        returned_date=str(info.get("date", "")),
        via=str(info.get("via", "")),
        note=str(info.get("note", "")),
        pusher=pusher,
    )
    message = outcome.summary
    if outcome.warnings:
        message += "\n" + "\n".join(outcome.warnings)
    return message


def ask_dispatch(parent: tk.Misc, app: Any, qsos: list[QSO], *, batch_id: int = 0) -> str:
    """Ask for the dispatch details, then record them."""
    if not qsos:
        return "没有可登记的记录"
    dialog = DispatchDialog(
        parent,
        f"登记发卡（{len(qsos)} 条）",
        default_date=today_compact(),
        push_label="同时回写 QRZ Logbook 的 QSL Card Sent 日期",
    )
    parent.wait_window(dialog)
    if dialog.result is None:
        return "已取消"
    return apply_dispatch(app, qsos, dialog.result, batch_id=batch_id)


def ask_return(parent: tk.Misc, app: Any, qsos: list[QSO]) -> str:
    """Ask for the return details, then record them."""
    if not qsos:
        return "没有可登记的记录"
    dialog = DispatchDialog(
        parent,
        f"登记回卡（{len(qsos)} 条）",
        default_date=today_compact(),
        push_label="同时回写 QRZ Logbook 的 QSL Card Received 日期",
    )
    parent.wait_window(dialog)
    if dialog.result is None:
        return "已取消"
    return apply_return(app, qsos, dialog.result)


class CardLogPanel(tk.Toplevel):
    """The dispatch table: filter, review, register returns, export, push."""

    def __init__(self, parent: tk.Misc, app: Any) -> None:
        super().__init__(parent)
        self.app = app
        self.title("QSL 发卡记录表")
        self.geometry("1040x620")
        self.minsize(900, 520)
        self.transient(parent)

        self._rows: dict[str, dict[str, Any]] = {}
        self._build()
        self.refresh()

    # -- construction -----------------------------------------------------
    def _build(self) -> None:
        style = ttk.Style()
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break

        top = ttk.Frame(self, padding=(8, 6))
        top.pack(fill="x")
        ttk.Label(top, text="状态").pack(side="left")
        self.status_var = tk.StringVar(value="")
        ttk.Combobox(
            top,
            textvariable=self.status_var,
            values=[""] + list(CARD_STATUSES),
            width=10,
            state="readonly",
        ).pack(side="left", padx=(4, 10))
        ttk.Label(top, text="呼号").pack(side="left")
        self.call_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.call_var, width=12).pack(side="left", padx=(4, 10))
        ttk.Label(top, text="批次").pack(side="left")
        self.batch_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.batch_var, width=8).pack(side="left", padx=(4, 10))
        ttk.Button(top, text="刷新", command=self.refresh).pack(side="left")
        ttk.Button(top, text="关闭", command=self.destroy).pack(side="right")

        self.stats_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.stats_var, padding=(10, 0)).pack(fill="x")

        table = ttk.Frame(self, padding=(8, 4))
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            table, columns=[name for name, _label, _width in COLUMNS], show="headings", height=18
        )
        for name, label, width in COLUMNS:
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, anchor="w", stretch=False)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-Button-1>", lambda _event: self.mark_returned())

        actions = ttk.Frame(self, padding=(8, 6))
        actions.pack(fill="x")
        ttk.Button(actions, text="登记回卡（选中）", command=self.mark_returned).pack(side="left")
        ttk.Button(actions, text="回写 QRZ（选中）", command=self.push_selected).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(actions, text="导出 CSV", command=self.export).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="标记退件", command=lambda: self.set_status("bounced")).pack(
            side="left", padx=(8, 0)
        )
        self.hint = tk.StringVar(value="双击一行可直接登记回卡。")
        ttk.Label(actions, textvariable=self.hint).pack(side="right")

    # -- data -------------------------------------------------------------
    def _filters(self) -> dict[str, Any]:
        batch = 0
        try:
            batch = int(self.batch_var.get().strip() or 0)
        except ValueError:
            batch = 0
        return {
            "status": self.status_var.get().strip(),
            "call": self.call_var.get().strip(),
            "batch_id": batch,
            "limit": 500,
        }

    def refresh(self) -> None:
        rows = self.app.store.card_log(**self._filters())
        self.tree.delete(*self.tree.get_children())
        self._rows.clear()
        for row in rows:
            iid = str(row["id"])
            self._rows[iid] = row
            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    row["call"],
                    row["qso_date"] or "",
                    row["band"] or "",
                    row["mode"] or "",
                    row["sent_date"] or "",
                    row["sent_via"] or "",
                    STATUS_LABELS.get(row["status"], row["status"]),
                    row["returned_date"] or "",
                    row["batch_id"] or "",
                    row["note"] or "",
                ),
            )
        info = self.app.store.dispatch_stats()
        self.stats_var.set(
            f"共 {info['total']} 条 · 已发出 {info['sent']} · 已回卡 {info['returned']} · "
            f"待回卡 {info['pending']} · 退件 {info['bounced']} · "
            f"回卡率 {info['return_rate'] * 100:.1f}%"
        )

    def _selected_qsos(self) -> list[QSO]:
        keys = [self._rows[iid]["bkey"] for iid in self.tree.selection() if iid in self._rows]
        return self.app.store.get_many(keys)

    # -- actions ----------------------------------------------------------
    def mark_returned(self) -> None:
        qsos = self._selected_qsos()
        if not qsos:
            messagebox.showinfo("提示", "请先选择要登记回卡的行。", parent=self)
            return
        message = ask_return(self, self.app, qsos)
        self.hint.set(message.replace("\n", " "))
        self.refresh()

    def set_status(self, status: str) -> None:
        selection = [iid for iid in self.tree.selection() if iid in self._rows]
        if not selection:
            messagebox.showinfo("提示", "请先选择一行。", parent=self)
            return
        for iid in selection:
            self.app.store.update_card_log(int(iid), status=status)
        self.refresh()

    def push_selected(self) -> None:
        rows = [self._rows[iid] for iid in self.tree.selection() if iid in self._rows]
        if not rows:
            messagebox.showinfo("提示", "请先选择要回写的行。", parent=self)
            return
        if not messagebox.askyesno(
            "回写 QRZ Logbook",
            f"将把 {len(rows)} 条记录的 QSL 状态写回 QRZ Logbook。\n\n"
            "只发送呼号/日期/波段/模式与 QSL 状态字段。继续吗？",
            parent=self,
        ):
            return
        qsos = self.app.store.get_many([row["bkey"] for row in rows])
        returned = [row for row in rows if row["status"] == "returned"]
        push = qrz_pusher(
            self.app.source_context("qrz"),
            sent_date="",
            via="",
            received=bool(returned) and len(returned) == len(rows),
        )
        try:
            pushed = push(qsos)
        except Exception as exc:  # noqa: BLE001 - reported to the user
            messagebox.showerror("回写失败", str(exc), parent=self)
            return
        self.hint.set(f"已回写 QRZ Logbook {pushed} 条")
        messagebox.showinfo("完成", f"已回写 {pushed} 条记录。", parent=self)

    def export(self) -> None:
        target = filedialog.asksaveasfilename(
            title="导出发卡记录",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile="qsl-card-log.csv",
        )
        if not target:
            return
        filters = self._filters()
        filters.pop("limit", None)
        count = card_log_csv(self.app.store, target, **filters)
        messagebox.showinfo("导出完成", f"已导出 {count} 条记录到\n{target}", parent=self)
