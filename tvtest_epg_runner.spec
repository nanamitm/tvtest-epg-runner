# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller の指示書。

    pyinstaller tvtest_epg_runner.spec

The sync server is imported by path at runtime rather than by a normal
import, so PyInstaller cannot find it on its own: its sources ride along as
data and syncserver.py looks for them in the bundle.
"""

import os

ROOT = os.path.abspath(os.getcwd())
SYNC_APP = os.path.join(
    ROOT, "thirdparty", "home-assistant-addons", "tvtest_epg_sync", "app")

if not os.path.isfile(os.path.join(SYNC_APP, "server.py")):
    raise SystemExit(
        "EPG 共有サーバのソースがありません。"
        "git submodule update --init を実行してください。")

datas = [
    (os.path.join(SYNC_APP, "server.py"), "epgsync"),
    (os.path.join(SYNC_APP, "epg_parser.py"), "epgsync"),
    (os.path.join(SYNC_APP, "arib_png.py"), "epgsync"),
    (os.path.join(SYNC_APP, "static"), os.path.join("epgsync", "static")),
]

a = Analysis(
    ["run.pyw"],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    # 使わない Qt の部品まで抱えないよう、明らかに不要なものを外す
    excludes=[
        "tkinter", "unittest", "pydoc_data",
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.Qt3DCore", "PySide6.QtCharts", "PySide6.QtDataVisualization",
        "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
        "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtSql",
        "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TVTestEpgRunner",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,              # トレイに常駐するのでコンソールは出さない
    icon=os.path.join(ROOT, "tvtest_epg_runner", "ui", "app.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="TVTestEpgRunner",
)
