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
]
binaries = []

for package in ("faster_whisper", "ctranslate2", "whisper"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

a = Analysis(
    [os.path.join(repo_root, "main_qt.py")],
    pathex=[repo_root],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "IPython", "jupyter", "notebook"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
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
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="JARVIS",
)
