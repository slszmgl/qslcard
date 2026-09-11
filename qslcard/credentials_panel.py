"""Data source credentials dialog (SRS 4.3-4.7, chapter 18).

The GUI needs a place to enter QRZ, LoTW and Club Log credentials; until now
only the CLI could write them.  Everything typed here goes into the local vault
(DPAPI on Windows), never into the configuration file, and the dialog says so.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any

from .privacy import mask_secret

__all__ = ["CREDENTIAL_GROUPS", "CredentialsDialog", "ensure_vault", "open_credentials"]

#: Vault-stored secrets: (key, label, is_secret).
CREDENTIAL_GROUPS: tuple[tuple[str, tuple[tuple[str, str, bool], ...]], ...] = (
    (
        "QRZ.com",
        (
            ("qrz_username", "用户名（XML 订阅账号）", False),
            ("qrz_password", "密码", True),
            ("qrz_logbook_key", "Logbook API Key", True),
        ),
    ),
    (
        "HamQTH（免费兜底 Callbook）",
        (
            ("hamqth_username", "用户名", False),
            ("hamqth_password", "密码", True),
        ),
    ),
    (
        "ARRL LoTW",
        (("lotw_password", "证书口令（调用 TQSL 时使用）", True),),
    ),
    (
        "Club Log",
        (
            ("clublog_email", "注册邮箱", False),
            ("clublog_password", "应用专用密码", True),
            ("clublog_api_key", "API Key", True),
        ),
    ),
)

#: Non-secret settings kept in the configuration file rather than the vault.
OPTION_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    ("lotw", "tqsl_path", "TQSL 程序路径 (tqsl.exe)", "file"),
    ("lotw", "report_path", "LoTW 导出报告路径（可选，用于手动导入）", "file"),
    ("eqsl", "report_path", "eQSL InBox 导出的 ADIF 路径", "file"),
)

_HELP: dict[str, str] = {
    "QRZ.com": (
        "QRZ 呼号查询需要 XML 订阅账号；Logbook API Key 在 "
        "qrz.com -> Logbook -> Settings 中生成，用于拉取日志与回写 QSL 状态。"
    ),
    "HamQTH（免费兜底 Callbook）": "没有 QRZ 订阅时用作兜底查询，注册免费。",
    "ARRL LoTW": (
        "LoTW 没有公开 API，本程序通过本地 TQSL 命令行下载确认数据，"
        "因此需要 TQSL 路径与证书口令。口令只传给子进程环境变量，不写入命令行、不入日志。"
    ),
    "Club Log": "用于实时上传与 OQRS 索卡；建议使用 Club Log 的「应用专用密码」而不是主密码。",
}


def ensure_vault(app: Any, parent: tk.Misc) -> Any:
    """Return a usable vault, asking for a master password when required."""
    vault = getattr(app, "vault", None)
    if vault is None:
        messagebox.showwarning(
            "无法使用凭证库",
            "当前没有可用的本机凭证库，请检查配置文件中的 vault_path。",
            parent=parent,
        )
        return None
    if vault.needs_master_password():
        password = simpledialog.askstring(
            "主口令",
            "本机凭证库需要主口令加密（此平台没有 DPAPI）。\n请输入主口令：",
            show="*",
            parent=parent,
        )
        if not password:
            return None
        vault.master_password = password
    return vault


class CredentialsDialog(tk.Toplevel):
    """Tabbed editor for every credential the connectors can use."""

    def __init__(self, parent: tk.Misc, app: Any) -> None:
        super().__init__(parent)
        self.app = app
        self.title("数据源与凭证")
        self.geometry("620x520")
        self.minsize(560, 460)
        self.transient(parent)
        self.saved = False

        self.vault = ensure_vault(app, parent)
        if self.vault is None:
            self.destroy()
            return

        self._secret_vars: dict[str, tk.StringVar] = {}
        self._secret_flags: dict[str, bool] = {}
        self._option_vars: dict[tuple[str, str], tk.StringVar] = {}
        self._entries: list[ttk.Entry] = []
        self._entry_by_key: dict[str, ttk.Entry] = {}

        self._build()
        self._load()
        self.grab_set()
        self.focus_set()
        self.bind("<Escape>", lambda _event: self.destroy())

    # -- construction -----------------------------------------------------
    def _build(self) -> None:
        style = ttk.Style()
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break

        header = ttk.Frame(self, padding=(10, 8))
        header.pack(fill="x")
        ttk.Label(
            header,
            text=(
                "凭证只保存在本机（Windows 使用 DPAPI 加密，绑定当前用户），"
                "不写入配置文件、不上传、不进入导出物。"
            ),
            wraplength=560,
            justify="left",
            foreground="#555555",
        ).pack(anchor="w")
        self.show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            header, text="显示明文", variable=self.show_var, command=self._toggle_visibility
        ).pack(anchor="w", pady=(4, 0))

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10)

        for title, fields in CREDENTIAL_GROUPS:
            frame = ttk.Frame(notebook, padding=10)
            notebook.add(frame, text=title.split("（")[0][:12])
            for row, (key, label, secret) in enumerate(fields):
                ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
                variable = tk.StringVar()
                entry = ttk.Entry(
                    frame, textvariable=variable, width=40, show="*" if secret else ""
                )
                entry.grid(row=row, column=1, sticky="we", padx=(6, 0))
                self._secret_vars[key] = variable
                self._secret_flags[key] = secret
                self._entries.append(entry)
                self._entry_by_key[key] = entry
                ttk.Button(
                    frame,
                    text="清除",
                    width=6,
                    command=lambda k=key: self._clear_secret(k),
                ).grid(row=row, column=2, sticky="w", padx=(6, 0))
            if title in _HELP:
                ttk.Label(
                    frame,
                    text=_HELP[title],
                    wraplength=520,
                    justify="left",
                    foreground="#666666",
                ).grid(row=len(fields), column=0, columnspan=3, sticky="w", pady=(10, 0))
            frame.columnconfigure(1, weight=1)

        # LoTW / eQSL also need file paths, which live in the configuration.
        paths = ttk.Frame(notebook, padding=10)
        notebook.add(paths, text="路径")
        for row, (source, key, label, kind) in enumerate(OPTION_FIELDS):
            ttk.Label(paths, text=label).grid(row=row, column=0, sticky="w", pady=4)
            variable = tk.StringVar()
            ttk.Entry(paths, textvariable=variable, width=36).grid(
                row=row, column=1, sticky="we", padx=(6, 0)
            )
            ttk.Button(
                paths,
                text="浏览…",
                width=8,
                command=lambda s=source, k=key, v=variable, t=kind: self._browse(s, k, v, t),
            ).grid(row=row, column=2, sticky="w", padx=(6, 0))
            self._option_vars[(source, key)] = variable
        ttk.Label(
            paths,
            text="这些是路径设置，写在配置文件里；凭证仍只存本机凭证库。",
            wraplength=520,
            justify="left",
            foreground="#666666",
        ).grid(row=len(OPTION_FIELDS), column=0, columnspan=3, sticky="w", pady=(10, 0))
        paths.columnconfigure(1, weight=1)

        footer = ttk.Frame(self, padding=(10, 8))
        footer.pack(fill="x")
        self.status = tk.StringVar(value="")
        ttk.Label(footer, textvariable=self.status, foreground="#2E7D32").pack(side="left")
        ttk.Button(footer, text="保存", width=10, command=self._save).pack(side="right")
        ttk.Button(footer, text="关闭", width=10, command=self.destroy).pack(
            side="right", padx=(0, 8)
        )
        ttk.Button(footer, text="清除全部凭证", width=14, command=self._clear_all).pack(
            side="right", padx=(0, 8)
        )

    # -- data -------------------------------------------------------------
    def _load(self) -> None:
        stored = self.vault.load() if self.vault is not None else {}
        for key, variable in self._secret_vars.items():
            if key in stored:
                variable.set(stored[key])
        sources = self.app.config.sources if isinstance(self.app.config.sources, dict) else {}
        for (source, key), variable in self._option_vars.items():
            section = sources.get(source)
            if isinstance(section, dict) and section.get(key):
                variable.set(str(section[key]))
        self.status.set(
            f"已保存 {len(stored)} 项凭证（{self.vault.backend if self.vault else '-'}）"
        )

    def _toggle_visibility(self) -> None:
        for entry in self._entries:
            entry.configure(show="" if self.show_var.get() else "*")

    def _clear_secret(self, key: str) -> None:
        self._secret_vars[key].set("")
        self.status.set(f"将清除 {key}（保存后生效）")

    def _clear_all(self) -> None:
        if not messagebox.askyesno(
            "清除全部凭证",
            "将删除本机凭证库中的全部账号与密钥，确定吗？",
            parent=self,
        ):
            return
        self.vault.clear()
        for variable in self._secret_vars.values():
            variable.set("")
        self.status.set("本机凭证库已清空")

    def _browse(self, source: str, key: str, variable: tk.StringVar, kind: str) -> None:
        current = variable.get().strip()
        if kind == "file":
            chosen = filedialog.askopenfilename(
                title="选择文件",
                initialdir=str(Path(current).parent) if current else None,
                filetypes=[("程序或日志", "*.exe *.adi *.adif *.adx"), ("所有文件", "*.*")],
            )
        else:
            chosen = filedialog.askdirectory(initialdir=current or None)
        if chosen:
            variable.set(chosen)

    def _save(self) -> None:
        saved = 0
        cleared = 0
        for key, variable in self._secret_vars.items():
            value = variable.get().strip()
            if value:
                self.vault.set(key, value)
                saved += 1
            elif self.vault.get(key):
                self.vault.delete(key)
                cleared += 1

        sources = dict(self.app.config.sources) if isinstance(self.app.config.sources, dict) else {}
        for (source, key), variable in self._option_vars.items():
            section = dict(sources.get(source, {})) if isinstance(sources.get(source), dict) else {}
            value = variable.get().strip()
            if value:
                section[key] = value
            else:
                section.pop(key, None)
            sources[source] = section
        self.app.config.sources = sources
        config_path = getattr(self.app, "config_path", "")
        if config_path:
            from .config import save_config

            save_config(self.app.config, config_path)

        self.saved = True
        self.status.set(f"已保存 {saved} 项凭证" + (f"，清除 {cleared} 项" if cleared else ""))
        self.app.store.add_audit("credentials", "saved", f"{saved} stored, {cleared} cleared")
        messagebox.showinfo(
            "已保存",
            f"凭证已写入本机凭证库（{self.vault.backend}）。\n"
            f"已保存 {saved} 项" + (f"，清除 {cleared} 项" if cleared else "") + "。",
            parent=self,
        )

    def summary(self) -> str:
        stored = self.vault.load() if self.vault is not None else {}
        if not stored:
            return "尚未保存任何凭证"
        return "、".join(f"{name}={mask_secret(value)}" for name, value in sorted(stored.items()))


def open_credentials(parent: tk.Misc, app: Any) -> CredentialsDialog | None:
    """Show the dialog; returns it so callers can inspect what happened."""
    dialog = CredentialsDialog(parent, app)
    if not dialog.winfo_exists() or dialog.vault is None:
        return None
    parent.wait_window(dialog)
    return dialog
