"""Headless construction tests for the Tk panels.

These do not simulate a user, but they do build every widget and run the panel
logic, which catches what unit tests of pure functions cannot: bad option names,
broken event bindings and layout crashes.

Two rules keep the suite reliable: every modal dialog is patched out, and one
Tk root is created for the whole module and kept alive, because repeatedly
creating and destroying roots makes Tcl intermittently refuse to start.
"""

from __future__ import annotations

from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog
from typing import Any

import pytest

tk = pytest.importorskip("tkinter")

from qslcard.config import AppConfig, PrintConfig  # noqa: E402
from qslcard.privacy import EgressPolicy  # noqa: E402
from qslcard.sources.base import SourceContext  # noqa: E402
from qslcard.store import Store  # noqa: E402


def _make_root() -> tk.Tk | None:
    """Create the one root this module uses, or None when Tk cannot work.

    tkinter can import while its Tcl data files are missing: GitHub's Windows
    runner ships exactly such a Python, where tk.Tk() raises
    "Can't find a usable init.tcl".  Creating and destroying extra roots also
    makes Tcl intermittently refuse to start, so this single root is kept alive
    for the whole module and shared by every test.
    """
    try:
        candidate = tk.Tk()
    except Exception:  # noqa: BLE001 - any Tcl/Tk problem means unusable
        return None
    candidate.withdraw()
    return candidate


_ROOT = _make_root()
if _ROOT is None:  # pragma: no cover - depends on the host
    pytest.skip("Tk is not usable in this environment", allow_module_level=True)


@pytest.fixture(autouse=True)
def no_modal_dialogs(monkeypatch):
    """Replace every dialog so tests never wait for a human."""
    monkeypatch.setattr(filedialog, "askopenfilename", lambda *a, **k: "")
    monkeypatch.setattr(filedialog, "askopenfilenames", lambda *a, **k: ())
    monkeypatch.setattr(filedialog, "askdirectory", lambda *a, **k: "")
    monkeypatch.setattr(filedialog, "asksaveasfilename", lambda *a, **k: "")
    monkeypatch.setattr(messagebox, "showinfo", lambda *a, **k: None)
    monkeypatch.setattr(messagebox, "showwarning", lambda *a, **k: None)
    monkeypatch.setattr(messagebox, "showerror", lambda *a, **k: None)
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(simpledialog, "askstring", lambda *a, **k: "")


class FakeApp:
    """Minimal stand-in for the shared App object."""

    def __init__(self, config: AppConfig, store: Any = None) -> None:
        self.config = config
        parent = Path(config.database).parent if config.database else Path.cwd()
        self.config_path = str(parent / "config.json")
        self.store = store if store is not None else Store(":memory:")
        self.policy = EgressPolicy(offline=True)
        self.vault = None
        self.cache = None

    def source_context(self, name: str) -> Any:
        return SourceContext(store=self.store, credentials={}, options={}, policy=self.policy)


def root() -> tk.Tk:
    """The single hidden root created at import time."""
    assert _ROOT is not None  # set at import; the module skips otherwise
    return _ROOT


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    config.station.callsign = "BG1XYZ"
    config.output_dir = str(tmp_path / "out")
    config.database = str(tmp_path / "qslcard.db")
    config.template_dir = str(tmp_path / "templates")
    return config


def test_print_panel_applies_presets_and_collects(tmp_path) -> None:
    from qslcard.print_panel import PrintSettingsPanel

    panel = PrintSettingsPanel(root(), PrintConfig())
    panel.update_idletasks()
    panel._apply_preset("commercial")
    collected = panel._collect()
    assert collected.pdf_type == "pdfx"
    assert collected.color == "CMYK"
    assert collected.registration_marks is True
    assert collected.calibration_lines is False
    panel._toggle_advanced()
    panel.update_idletasks()
    assert panel._advanced_shown is True
    panel._apply_preset("home")
    assert panel._collect().color == "RGB"
    panel._refresh_notes()
    assert "色彩模式" in panel.notes.get("1.0", "end")
    panel.destroy()


def test_print_panel_save_callback_receives_config() -> None:
    from qslcard.print_panel import PrintSettingsPanel

    captured: list[PrintConfig] = []
    panel = PrintSettingsPanel(root(), PrintConfig(), on_saved=captured.append)
    panel._apply_preset("commercial")
    panel._save()
    assert captured and captured[0].color == "CMYK"
    assert panel.result is captured[0]


def test_print_panel_rejects_negative_values() -> None:
    from qslcard.print_panel import PrintSettingsPanel

    panel = PrintSettingsPanel(root(), PrintConfig())
    panel.bleed.set("-2")
    panel._save()
    assert panel.result is None
    assert panel.winfo_exists()
    panel.destroy()


def test_designer_builds_edits_and_draws(tmp_path) -> None:
    from qslcard.designer import CardDesigner

    designer = CardDesigner(root(), FakeApp(_config(tmp_path)), "classic")
    designer.update_idletasks()
    assert designer.template.name == "classic"
    assert designer._face().elements

    designer._add("rect")  # only the image kind opens a dialog
    selected = designer.selected
    assert selected is not None and selected.type == "rect"
    designer._nudge(2.0, 2.0)
    designer.props["x"].set("12.5")
    designer.props["w"].set("33")
    designer.props["color"].set("#FF8800")
    designer._apply_props()
    assert selected.x_mm == pytest.approx(12.5)
    assert selected.w_mm == pytest.approx(33.0)
    assert selected.color == "#FF8800"

    designer._full_bleed()
    assert selected.x_mm == pytest.approx(-3.0)
    assert selected.w_mm == pytest.approx(96.0)

    before = len(designer._face().elements)
    designer._reorder(1)
    designer._delete()
    assert len(designer._face().elements) == before - 1

    designer.face_var.set(1)
    designer._switch_face()
    designer.update_idletasks()
    assert designer.face_index == 1
    designer.destroy()


def test_designer_image_element_reports_effective_dpi(tmp_path) -> None:
    from PIL import Image

    from qslcard.designer import CardDesigner
    from qslcard.editor import add_element

    source = tmp_path / "logo.png"
    Image.new("RGB", (1200, 1200), (200, 30, 30)).save(source)
    designer = CardDesigner(root(), FakeApp(_config(tmp_path)), "minimal")
    element = add_element(designer._face(), "image", designer.card, image=str(source))
    designer._select(element)
    designer.update_idletasks()
    designer._refresh_image_note()
    note = designer.image_note.cget("text")
    assert "1200x1200" in note
    assert "DPI" in note
    assert str(designer.image_note.cget("foreground")) == "#2E7D32"

    # A tiny asset must be flagged rather than silently accepted.
    tiny = tmp_path / "tiny.png"
    Image.new("RGB", (40, 40), (10, 10, 10)).save(tiny)
    element.image = str(tiny)
    designer._refresh_image_note()
    assert "低于" in designer.image_note.cget("text")
    designer.destroy()


def test_designer_background_image_set_and_clear(tmp_path) -> None:
    from PIL import Image

    from qslcard.designer import CardDesigner

    source = tmp_path / "bg.png"
    Image.new("RGB", (900, 1400), (20, 20, 20)).save(source)
    designer = CardDesigner(root(), FakeApp(_config(tmp_path)), "classic")
    designer._face().background_image = str(source)
    designer.bg_var.set(str(source))
    designer._draw()
    designer._clear_background()
    assert designer._face().background_image == ""
    assert designer.bg_var.get() == ""
    designer._draw()
    # Both faces must render without error.
    designer.face_var.set(1)
    designer._switch_face()
    designer._draw()
    designer.destroy()


def test_card_log_panel_lists_updates_and_exports(tmp_path) -> None:
    from qslcard.adif import qso_from_fields
    from qslcard.cardlog_panel import CardLogPanel, apply_dispatch, apply_return

    store = Store(":memory:")
    qsos = [
        qso_from_fields(
            {
                "CALL": "JA1AA",
                "QSO_DATE": "20260911",
                "TIME_ON": "1200",
                "BAND": "20m",
                "MODE": "SSB",
            }
        )
    ]
    store.import_qsos(qsos, strategy="skip")
    app = FakeApp(_config(tmp_path), store)

    message = apply_dispatch(
        app, qsos, {"date": "20260912", "via": "BURO", "note": "", "push": False}
    )
    assert "已记录 1" in message
    assert store.query()[0].qslsdate == "20260912"

    panel = CardLogPanel(root(), app)
    panel.update_idletasks()
    assert len(panel.tree.get_children()) == 1
    iid = panel.tree.get_children()[0]
    assert panel.tree.set(iid, "call") == "JA1AA"
    assert panel.tree.set(iid, "status") == "已发出"
    assert "已发出 1" in panel.stats_var.get()

    # Status changes go through the panel and are reflected on refresh.
    panel.tree.selection_set(iid)
    panel.set_status("bounced")
    assert store.card_log()[0]["status"] == "bounced"

    message = apply_return(
        app, qsos, {"date": "20261005", "via": "BURO", "note": "", "push": False}
    )
    assert "已记录 1" in message
    panel.refresh()
    assert panel.tree.set(panel.tree.get_children()[0], "status") == "已回卡"
    assert store.dispatch_stats()["returned"] == 1

    # Filters and the export path run without a dialog (filedialog is patched).
    panel.status_var.set("returned")
    panel.refresh()
    assert len(panel.tree.get_children()) == 1
    panel.status_var.set("sent")
    panel.refresh()
    assert panel.tree.get_children() == ()
    panel.export()
    panel.destroy()


def test_dispatch_dialog_validates_the_date() -> None:
    from qslcard.cardlog_panel import DispatchDialog

    dialog = DispatchDialog(root(), "登记发卡")
    dialog.date_var.set("2026-09-12")  # wrong format on purpose
    dialog._ok()
    assert dialog.result is None
    assert dialog.winfo_exists()
    dialog.date_var.set("20260912")
    dialog.via_var.set("DIRECT")
    dialog._ok()
    assert dialog.result == {"date": "20260912", "via": "DIRECT", "note": "", "push": False}


def test_settings_dialog_shows_mm_paper_and_multi_select_via(tmp_path) -> None:
    from qslcard.gui import SettingsDialog

    config = _config(tmp_path)
    config.station.callsign = "BG1XYZ"
    config.station.qsl_via = "BUREAU"
    app = FakeApp(config)

    dialog = SettingsDialog(root(), app)
    dialog.update_idletasks()
    # Paper choices now carry their size in millimetres.
    assert dialog.vars["paper"].get() == "A4 (210 x 297 mm)"
    # QSL route is a multi-select; the stored value pre-selects its boxes.
    assert dialog.via_vars["BUREAU"].get() is True
    assert dialog.via_vars["DIRECT"].get() is False
    assert dialog.via_vars["LOTW"].get() is False

    dialog.via_vars["DIRECT"].set(True)
    dialog.via_vars["OQRS"].set(True)
    dialog.vars["paper"].set("LETTER (215.9 x 279.4 mm)")
    dialog._save()

    assert config.station.qsl_via == "BUREAU,DIRECT,OQRS"
    assert config.print.paper == "LETTER"
    assert dialog.saved is True
    assert Path(app.config_path).is_file()


def test_credentials_dialog_saves_paths_and_secrets_into_the_vault(tmp_path) -> None:
    from qslcard.credentials_panel import CredentialsDialog
    from qslcard.privacy import CredentialVault

    config = _config(tmp_path)
    config.vault_path = str(tmp_path / "vault.json")
    app = FakeApp(config)
    app.vault = CredentialVault(config.vault_path, master_password="test", iterations=1000)

    dialog = CredentialsDialog(root(), app)
    dialog.update_idletasks()
    assert dialog.vault is not None
    # Secrets are masked until the user asks to see them.
    assert dialog._entry_by_key["qrz_password"].cget("show") == "*"
    assert dialog._entry_by_key["qrz_username"].cget("show") == ""  # user names stay readable
    dialog.show_var.set(True)
    dialog._toggle_visibility()
    assert dialog._entry_by_key["qrz_password"].cget("show") == ""

    dialog._secret_vars["qrz_username"].set("bg1xyz")
    dialog._secret_vars["qrz_password"].set("s3cr3t")
    dialog._secret_vars["qrz_logbook_key"].set("api-key-123")
    dialog._secret_vars["lotw_password"].set("cert-pass")
    dialog._option_vars[("lotw", "tqsl_path")].set("C:/TrustedQSL/tqsl.exe")
    dialog._option_vars[("eqsl", "report_path")].set(str(tmp_path / "eqsl.adi"))
    dialog._save()
    dialog.update_idletasks()

    assert app.vault.get("qrz_logbook_key") == "api-key-123"
    assert app.vault.get("lotw_password") == "cert-pass"
    assert app.config.sources["lotw"]["tqsl_path"] == "C:/TrustedQSL/tqsl.exe"
    assert app.config.sources["eqsl"]["report_path"].endswith("eqsl.adi")
    # The vault file must never contain plaintext.
    assert "api-key-123" not in Path(config.vault_path).read_text(encoding="utf-8")
    assert "*" in dialog.summary()

    # Clearing a field and saving removes just that entry.
    dialog._clear_secret("qrz_username")
    dialog._save()
    assert app.vault.get("qrz_username") is None
    assert app.vault.get("qrz_password") == "s3cr3t"
    dialog.destroy()


def test_credentials_dialog_without_a_vault_reports_and_closes(tmp_path) -> None:
    from qslcard.credentials_panel import CredentialsDialog, ensure_vault

    config = _config(tmp_path)
    app = FakeApp(config)
    app.vault = None
    assert ensure_vault(app, root()) is None
    dialog = CredentialsDialog(root(), app)
    assert not dialog.winfo_exists()


def test_main_window_exposes_the_credentials_entry(tmp_path) -> None:
    from qslcard.gui import QslCardWindow

    window = QslCardWindow(FakeApp(_config(tmp_path)))
    window.root.update_idletasks()
    try:
        assert hasattr(window, "open_credentials")
        # The entry point must be reachable from the menu, not only the toolbar.
        menu = window.root.nametowidget(window.root.cget("menu"))
        labels: list[str] = []
        for index in range(menu.index("end") + 1):
            if menu.type(index) == "cascade":
                sub = window.root.nametowidget(menu.entrycget(index, "menu"))
                for sub_index in range(sub.index("end") + 1):
                    if sub.type(sub_index) != "separator":
                        labels.append(str(sub.entrycget(sub_index, "label")))
        assert any("数据源与凭证" in label for label in labels), labels
        assert any("商业印刷参数" in label for label in labels), labels
    finally:
        window.root.destroy()
