# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['D:\\文档\\ChatGPT\\电脑管理项目\\folder_pilot.py'],
    pathex=[],
    binaries=[],
    datas=[('D:\\文档\\ChatGPT\\电脑管理项目\\assets\\FolderPilot.ico', 'assets')],
    hiddenimports=[],
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
    name='FolderPilot',
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
    icon=['D:\\文档\\ChatGPT\\电脑管理项目\\assets\\FolderPilot.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='FolderPilot',
)
