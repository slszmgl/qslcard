# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: a windowed GUI executable plus a console CLI executable.

Both share one Analysis, so the heavy third-party code (fpdf2, fontTools,
Pillow) is collected once and embedded in each single-file binary.
"""

from PyInstaller.utils.hooks import collect_submodules

hidden = (
    collect_submodules("fpdf")
    + collect_submodules("fontTools")
    # The GUI panels are imported lazily inside methods, so list the package
    # explicitly rather than trusting the bytecode scan.
    + collect_submodules("qslcard")
    + ["defusedxml", "PIL", "PIL.Image", "xml.etree.ElementTree", "tkinter", "tkinter.ttk"]
)

# Ship the reference templates so first run can seed editable copies.
datas = [("templates", "templates")]

# Keep the binaries lean: none of these are used by the application.
excludes = [
    "numpy",
    "pandas",
    "matplotlib",
    "scipy",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "IPython",
    "pytest",
    "notebook",
    "setuptools",
    "pip",
    "wheel",
    "pydoc_data",
]

a = Analysis(
    ["app_main.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

gui_exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="QSL卡片打印系统",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon="assets/qslcard.ico",
)

cli_exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="qslcard-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon="assets/qslcard.ico",
)
