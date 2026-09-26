"""应用入口：装配服务 → 起 asyncio 线程 → 加载 QML。

界面线程只做渲染与信号槽；网络与编排都在 asyncio 线程里（见 `app/ui/async_runner.py`）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from app.bootstrap import build_context
from app.config import settings
from app.ui.agent_bridge import AgentBridge
from app.ui.async_runner import AsyncRunner
from app.ui.bridge import UiBridge
from app.ui.knowledge_bridge import KnowledgeBridge
from app.ui.selfupdate_bridge import SelfUpdateBridge
from app.ui.settings_bridge import SettingsBridge
from app.ui.theme import Theme, detect_system_dark

def _resolve_qml_dir() -> Path:
    """定位 QML 目录。

    源码运行：`app/ui/qml`；打包运行：脚本被放在 `_internal/` 下，而 Qt 的数据目录
    是 `_internal/ui/qml`（spec 里按 `app/ui/qml` 收集，PyInstaller 会剥掉顶层脚本目录）。
    两种情况都试一遍，避免「开发能跑、打包找不到界面」。
    """
    here = Path(__file__).resolve().parent
    for candidate in (
        here / "ui" / "qml",                    # 打包后：_internal/ui/qml
        here / "app" / "ui" / "qml",            # 打包后（保留包结构时）
        here.parent / "app" / "ui" / "qml",     # 源码运行
    ):
        if (candidate / "Main.qml").is_file():
            return candidate
    return here / "ui" / "qml"


QML_DIR = _resolve_qml_dir()


def _resolve_dark() -> bool:
    """按设置解析外观：system / light / dark。"""
    mode = str(settings.get("theme", "system") or "system").lower()
    if mode == "dark":
        return True
    if mode == "light":
        return False
    return detect_system_dark()


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv)
    self_test = "--self-test" in argv            # 打包产物的自检开关
    argv = [item for item in argv if item != "--self-test"]
    QQuickStyle.setStyle("Basic")        # 统一控件外观，才能按设计 token 上色

    app = QGuiApplication(argv)
    app.setApplicationName("Stillroom")
    app.setOrganizationName("Stillroom")

    from app.config import paths

    icon_path = paths.resource_path("app_icon.png")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    context = build_context()
    runner = AsyncRunner()
    theme = Theme(dark=_resolve_dark())
    bridge = UiBridge(context, runner, theme)
    settings_bridge = SettingsBridge(context, runner, theme)
    agent_bridge = AgentBridge(context, runner, bridge)
    selfupdate_bridge = SelfUpdateBridge(context, runner)
    knowledge_bridge = KnowledgeBridge(context, runner)

    engine = QQmlApplicationEngine()
    # 桥挂到引擎下：Python 先回收桥的话，QML 会拿着 null 去刷绑定，退出时刷一屏报错
    for obj in (bridge, settings_bridge, agent_bridge, selfupdate_bridge, knowledge_bridge):
        obj.setParent(engine)
    qml_messages: list[str] = []
    engine.warnings.connect(lambda items: qml_messages.extend(str(item) for item in items))
    engine.rootContext().setContextProperty("backend", bridge)
    engine.rootContext().setContextProperty("theme", theme)
    engine.rootContext().setContextProperty("settingsBridge", settings_bridge)
    engine.rootContext().setContextProperty("agentBridge", agent_bridge)
    engine.rootContext().setContextProperty("selfUpdateBridge", selfupdate_bridge)
    engine.rootContext().setContextProperty("knowledgeBridge", knowledge_bridge)
    engine.addImportPath(str(QML_DIR))
    engine.load(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")))

    if not engine.rootObjects():
        detail = "\n".join(qml_messages) or "(QML 引擎没有给出更多信息)"
        print(f"界面加载失败：QML 没有产生根对象\n{detail}", file=sys.stderr)
        if self_test:
            try:
                from app.config import paths

                (paths.runtime_dir() / "self-test.log").write_text(
                    f"自检失败：QML 没有产生根对象\n{detail}\n", encoding="utf-8"
                )
            except Exception:
                pass
        runner.close()
        return 1

    if self_test:
        # 不开窗口，只确认「能装配 + 能加载界面 + 事件循环能跑」，1.5 秒后退出。
        # 这样每次打包后都能自动验证一次，不必靠人手点开看。
        report = f"自检通过：QML 已加载，根对象 {len(engine.rootObjects())} 个\n"
        print(report, end="")
        try:
            # 打包成窗口程序后没有控制台，把结果写到 exe 同级，便于打包后自动验证
            from app.config import paths

            (paths.runtime_dir() / "self-test.log").write_text(report, encoding="utf-8")
        except Exception:
            pass
        QTimer.singleShot(1500, app.quit)

    def _shutdown() -> None:
        bridge.detach()
        agent_bridge.detach()
        # 关掉 HTTP 连接池与两个数据库句柄（在循环线程里做），再停循环线程。
        # 留着让解释器去收，退出时刻「谁先被回收」就不可控了。
        try:
            runner.run_blocking(context.aclose(), timeout=3.0)
        except Exception:                      # pragma: no cover - 收尾失败不该挡住退出
            pass
        runner.close()

    app.aboutToQuit.connect(_shutdown)
    code = app.exec()
    return code


if __name__ == "__main__":
    sys.exit(main())
