"""测试夹具：把配置与数据目录隔离到临时路径，避免碰到真实环境。

另外提供一个极小的异步测试支持（`pytest_pyfunc_call` 钩子）：本项目的测试几乎都是
异步的，用 6 行钩子就够，不必为此引入 pytest-asyncio 依赖。等真正需要异步 fixture
时再换插件。
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
from pathlib import Path

import pytest

def _configure_qt_rendering_environment() -> None:
    """在 Qt 初始化前准备好离屏渲染环境。

    Windows 的 Qt 安装包不带中文字体，必须显式把 QPA 指到系统字体目录，
    否则 QFontDatabase 可能为空，截图里的中文会全部变成方框。
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 界面测试一律离屏
    if "QT_QPA_FONTDIR" in os.environ:
        return
    candidates = [
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
        Path("/usr/share/fonts"),
    ]
    for candidate in candidates:
        if candidate.is_dir():
            os.environ["QT_QPA_FONTDIR"] = str(candidate)
            return


_configure_qt_rendering_environment()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    """整个测试会话共用一个离屏 QGuiApplication。"""
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQuickControls2 import QQuickStyle

    QQuickStyle.setStyle("Basic")
    app = QGuiApplication.instance() or QGuiApplication([])
    yield app


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """直接运行 `async def test_*`，无需插件。"""
    func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(func):
        return None
    kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
    asyncio.run(func(**kwargs))
    return True


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """每个测试用例都跑在独立的 settings.json 与数据目录上。"""
    monkeypatch.setenv("AGNES_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("AGNES_HISTORY_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("AGNES_VIDEO_QUERY_URL", raising=False)
    monkeypatch.delenv("AGNES_BASE_URL", raising=False)
    monkeypatch.delenv("AGNES_API_KEY", raising=False)
    settings.reset_cache()
    yield
    settings.reset_cache()


@pytest.fixture
def env_file(tmp_path):
    """返回一个可以自由写入的 .env 路径。"""
    return tmp_path / ".env"
