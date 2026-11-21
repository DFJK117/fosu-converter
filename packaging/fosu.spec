# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['fosu_app.py'],
    pathex=[],
    # ffmpeg.exe 绑进 exe（_MEIPASS/ffmpeg.exe）：音频转码 / 混音 / 视频 / 编谱器播放都靠它
    binaries=[('ffmpeg.exe', '.')],
    datas=[],
    # 编谱器与音频引擎是惰性导入（运行时 import），必须显式列为 hiddenimports
    hiddenimports=['fosu_editor', 'fosu_audio', 'fosu_osumod', 'wave', 'array', 'ctypes'],
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
    name='fosu',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
