import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


root = Path(SPECPATH)
hiddenimports = collect_submodules("pyvisa_py") + collect_submodules("vortran_lbl")
conda_bin = Path(sys.base_prefix) / "Library" / "bin"
extra_binaries = [
    (str(conda_bin / name), ".")
    for name in ("ffi.dll", "libmpdec-4.dll", "tcl86t.dll", "tk86t.dll")
    if (conda_bin / name).exists()
]

a = Analysis(
    [str(root / "pcmwriter_app.py")],
    pathex=[str(root)],
    binaries=extra_binaries,
    datas=[
        (str(root / "pumpauto" / "assets"), "pumpauto/assets"),
        (str(root / "pumpauto" / "spectra"), "pumpauto/spectra"),
    ],
    hiddenimports=hiddenimports,
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
    name="PCMWriter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(root / "pumpauto" / "assets" / "pcmwriter_icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="PCMWriter",
)
