# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_submodules
import os

repo_root = os.path.abspath(os.path.join(SPECPATH, ".."))

hiddenimports = collect_submodules("plugins") + [
    "pyttsx3.drivers.sapi5",
    "pyttsx3.drivers.nsss",
    "pyttsx3.drivers.espeak",
]
datas = [
    (os.path.join(repo_root, "assets", "jarvis.ico"), "assets"),
    (os.path.join(repo_root, "assets", "jarvis_icon_1024.png"), "assets"),
    (os.path.join(repo_root, "config", "web_providers.json"), "config"),
    (os.path.join(repo_root, "config", "finance_symbols.json"), "config"),
    (os.path.join(repo_root, "config", "desktop_messaging_providers.json"), "config"),
]
binaries = []

def runtime_submodule(name):
    """Keep dynamic runtime modules while excluding package QA/CLI payloads."""
    blocked = (
        ".tests", ".test_", ".testing_utils", ".commands", ".__main__",
        ".__pyinstaller", "pywinauto.linux", "ctranslate2.converters",
    )
    return not any(part in name for part in blocked)

for package in (
    "faster_whisper", "ctranslate2", "whisper", "diffusers", "accelerate",
    "yt_dlp", "yfinance", "pywinauto", "jamo", "g2pk",
):
    package_datas, package_binaries, package_hidden = collect_all(
        package,
        filter_submodules=runtime_submodule,
    )
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

runtime_a = Analysis(
    [os.path.join(repo_root, "main_qt.py")],
    pathex=[repo_root],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "IPython", "jupyter", "notebook", "tensorboard", "tensorflow", "tkinter"],
    noarchive=False,
    optimize=1,
)
# Poppler's ICU uses version-suffixed exports while Qt 6 deliberately imports
# Windows' unversioned system ICU.  The bundled Codex runtime places Poppler on
# PATH during builds, so PyInstaller can mistake that incompatible DLL for a
# Qt dependency.  Shipping it makes QtCore fail with WinError 127.
poppler_icu_names = {"icuuc.dll", "icudt78.dll", "icuin78.dll"}
runtime_a.binaries = [
    entry for entry in runtime_a.binaries
    if entry[0].lower() not in poppler_icu_names
]
runtime_pyz = PYZ(runtime_a.pure)

runtime_exe = EXE(
    runtime_pyz,
    runtime_a.scripts,
    [],
    exclude_binaries=True,
    name="JARVIS-runtime",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=os.path.join(repo_root, "assets", "jarvis.ico"),
)

launcher_a = Analysis(
    [os.path.join(repo_root, "packaging", "entrypoint.py")],
    pathex=[repo_root],
    binaries=[],
    datas=[(os.path.join(repo_root, "assets", "jarvis.ico"), "assets")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
launcher_pyz = PYZ(launcher_a.pure)
launcher_exe = EXE(
    launcher_pyz,
    launcher_a.scripts,
    [],
    exclude_binaries=True,
    name="JARVIS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=os.path.join(repo_root, "assets", "jarvis.ico"),
)

coll = COLLECT(
    launcher_exe,
    runtime_exe,
    launcher_a.binaries,
    launcher_a.datas,
    runtime_a.binaries,
    runtime_a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="JARVIS",
)
