# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

要点：

1. **QML 目录必须一起打包**。QtQuick 的 QML 模块里是插件 DLL，Qt 在运行时按
   import 路径去磁盘加载，PyInstaller 的 Python 分析看不到它们，所以显式列出。
2. **排除用不到的 Qt 大件**。`Qt6WebEngineCore.dll` 单个 194MB，另外 3D / Charts /
   DataVisualization 等也都不小，本应用一个都不用。
3. **QtMultimedia 默认不进包**（ffmpeg 后端约 15MB）。`VideoPlayer.qml` 用 Loader 懒加载，
   缺模块只提示一句、不影响主界面；需要内置播放时用 `python tools/build.py --with-video`。
"""

import os
from pathlib import Path

PROJECT = Path(SPECPATH).resolve()
VENV_SITE = PROJECT / ".venv" / "Lib" / "site-packages" / "PySide6"

WITH_VIDEO = os.environ.get("AGNES_WITH_VIDEO") == "1"

# ---------------------------------------------------------------- 数据文件
datas = [
    (str(PROJECT / "app" / "ui" / "qml"), "app/ui/qml"),   # 我们自己的界面
    (str(PROJECT / ".env.example"), "."),
]
for asset in ("app_icon.png", "app_icon.ico"):
    if (PROJECT / asset).exists():
        datas.append((str(PROJECT / asset), "."))

# Qt 自带的 QML 模块：只用 QtQuick 与 QtQml
for module in ("QtQml", "QtQuick"):
    source = VENV_SITE / "qml" / module
    if source.exists():
        datas.append((str(source), f"PySide6/qml/{module}"))
if WITH_VIDEO:
    datas.append((str(VENV_SITE / "qml" / "QtMultimedia"), "PySide6/qml/QtMultimedia"))

# ---------------------------------------------------------------- 排除项
excludes = [
    # 用不到的 Qt 大件
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras", "PySide6.QtQuick3D",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtLocation", "PySide6.QtPositioning",
    "PySide6.QtSerialPort", "PySide6.QtSensors", "PySide6.QtRemoteObjects",
    "PySide6.QtScxml", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtTextToSpeech",
    "PySide6.QtSql", "PySide6.QtUiTools", "PySide6.QtOpenGLWidgets",
    # 界面/数据都不用它：图片处理走 Qt，历史走标准库 sqlite3
    "tkinter", "numpy", "PIL", "matplotlib", "pytest", "IPython",
    # 打包工具自身
    "PyInstaller",
]
if not WITH_VIDEO:
    excludes += ["PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets"]

a = Analysis(
    [str(PROJECT / "app" / "main.py")],
    pathex=[str(PROJECT)],
    binaries=[],
    datas=datas,
    hiddenimports=["PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickControls2"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Stillroom",
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
    icon=str(PROJECT / "app_icon.ico") if (PROJECT / "app_icon.ico").exists() else None,
)

# ---------------------------------------------------------------- 事后过滤
# PySide6 官方 hook 会把整个 Qt bin 目录塞进来（WebEngine 单个 194MB）；构建时如果
# PATH 里还有 Anaconda，它带的 ICU 73 也会被打进去——而那恰好是当初让 PySide6 加载
# 失败的东西（导出的是带版本后缀的符号）。这里按名单剔除，交给 Windows 自带 ICU。
DROP_PATTERNS = (
    "qt6webengine", "qt6pdf", "qt63d", "qt6quick3d", "qt6shadertools",
    "qt6designer", "qt6help", "qt6test", "qt6charts", "qt6datavisualization",
    "qt6sql", "qt6bluetooth", "qt6nfc", "qt6positioning", "qt6location",
    "qt6sensors", "qt6serialport", "qt6texttospeech", "qt6remoteobjects",
    "qt6scxml",
    "icudt", "icuin", "icuuc",                       # ← 用系统 ICU
    # 下面这些都不能剔（都是实测撞出来的）：
    #   QtOpenGL.pyd —— PySide6.QtQuick 的必需兄弟模块，剔掉后 import 直接失败
    #   Qt6Widgets   —— QQuickWidget 依赖
    #   opengl32sw   —— 没有独显/驱动异常时的软件 OpenGL 兜底
    #   控件风格插件 —— QtQuick Controls 会按风格名去找
)


def _drop(name: str) -> bool:
    lowered = Path(name).name.lower()
    return any(pattern in lowered for pattern in DROP_PATTERNS)


kept_binaries = [item for item in a.binaries if not _drop(item[0])]
kept_datas = [item for item in a.datas if not _drop(item[0])]
print(
    f"[spec] 过滤：二进制 {len(a.binaries)} → {len(kept_binaries)}，"
    f"数据 {len(a.datas)} → {len(kept_datas)}"
)

coll = COLLECT(
    exe,
    kept_binaries,
    kept_datas,
    strip=False,
    upx=False,
    name="Stillroom",
)
