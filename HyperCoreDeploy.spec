# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = []
hiddenimports += collect_submodules('installer')


a = Analysis(
    ['\\\\witburgdata\\home\\Projects\\programs\\hypercore-manager\\installer\\windows\\wizard.py'],
    pathex=['\\\\witburgdata\\home\\Projects\\programs\\hypercore-manager'],
    binaries=[],
    datas=[('\\\\witburgdata\\home\\Projects\\programs\\hypercore-manager\\build\\payload\\repo.tar.gz', 'payload'), ('\\\\witburgdata\\home\\Projects\\programs\\hypercore-manager\\installer\\favicon.ico', 'installer')],
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
    a.binaries,
    a.datas,
    [],
    name='HyperCoreDeploy',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['\\\\witburgdata\\home\\Projects\\programs\\hypercore-manager\\installer\\favicon.ico'],
)
