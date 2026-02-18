# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs


def _safe_collect_data(pkg: str):
    try:
        return collect_data_files(pkg, include_py_files=False)
    except Exception:
        return []


def _safe_collect_bins(pkg: str):
    try:
        return collect_dynamic_libs(pkg)
    except Exception:
        return []


extra_datas = [
    ("assets", "assets"),
    ("icon.ico", "."),
    ("icon32.png", "."),
]
extra_datas += _safe_collect_data("soundfile")

extra_bins = []
extra_bins += _safe_collect_bins("soundfile")
extra_bins += _safe_collect_bins("miniaudio")

a = Analysis(
    ["simple_moozic_builder_ui.py"],
    pathex=[],
    binaries=extra_bins,
    datas=extra_datas,
    hiddenimports=[
        "miniaudio",
        "soundfile",
        "numpy",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SimpleMoozicBuilder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["icon.ico"],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="SimpleMoozicBuilder",
)
