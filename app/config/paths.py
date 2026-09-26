"""路径解析的唯一来源。

与旧版（三个模块各写一份 `resource_path`/`runtime_dir`）的差异：只有这里一份。

优先级与旧版保持一致，保证交付形态不变：

    .env       : 运行目录（exe 同级）> 打包内置 > 当前工作目录
    settings   : %APPDATA%\\AgnesGenerator\\settings.json
    数据目录   : 环境变量 AGNES_HISTORY_DIR > settings.data_dir > 默认目录
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 与旧版一致：Windows 默认放 F 盘独立数据目录
DEFAULT_DATA_DIR_NAME = "AgnesGeneratorData"
DEFAULT_DATA_DIR_WINDOWS = r"F:\AgnesGeneratorData"
CONFIG_DIR_NAME = "AgnesGenerator"


def is_frozen() -> bool:
    """是否运行在打包产物里（PyInstaller）。"""
    return bool(getattr(sys, "frozen", False))


def runtime_dir() -> Path:
    """程序运行目录：源码 = 项目根；打包后 = exe 所在目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def resource_dir() -> Path:
    """只读资源目录：打包后为解包目录，源码下与运行目录相同。"""
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else runtime_dir()


def resource_path(relative: str | os.PathLike[str]) -> Path:
    """资源文件绝对路径（图标、内置 .env 等）。"""
    return resource_dir() / relative


def env_candidates() -> list[Path]:
    """`.env` 的候选位置，**按优先级从高到低**（先命中的胜出）。"""
    out: list[Path] = []
    for candidate in (runtime_dir() / ".env", resource_dir() / ".env", Path.cwd() / ".env"):
        if candidate not in out:
            out.append(candidate)
    return out


def default_data_dir() -> Path:
    """默认数据目录（历史与媒体缓存）。"""
    if os.name == "nt":
        return Path(DEFAULT_DATA_DIR_WINDOWS)
    return Path(os.path.expanduser("~")) / DEFAULT_DATA_DIR_NAME


def app_config_dir() -> Path:
    """偏好文件所在目录（%APPDATA%\\AgnesGenerator）。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(base) / CONFIG_DIR_NAME


def settings_file() -> Path:
    """`settings.json` 路径；`AGNES_SETTINGS_FILE` 可覆盖（测试与绿色版用）。"""
    override = (os.environ.get("AGNES_SETTINGS_FILE") or "").strip()
    return Path(override) if override else app_config_dir() / "settings.json"


def data_dir(settings: dict | None = None) -> Path:
    """数据目录：环境变量 > settings.data_dir > 默认目录。"""
    env = (os.environ.get("AGNES_HISTORY_DIR") or "").strip()
    if env:
        return Path(env)
    if settings:
        configured = str(settings.get("data_dir") or "").strip()
        if configured:
            return Path(configured)
    return default_data_dir()


def ensure_dir(path: str | os.PathLike[str]) -> Path:
    """确保目录存在并返回它。"""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def local_path(value: str | os.PathLike[str] | None) -> str:
    """把 `file://` URL 或普通路径统一成本地路径。

    界面（QML 的文件选择器）给出的是 URL，而服务层要的是本地路径；
    转换放在这里，避免每个调用点各写一遍 replace。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower().startswith("file://"):
        from urllib.parse import unquote, urlsplit

        parts = urlsplit(text)
        path = unquote(parts.path)
        # Windows 上形如 /F:/dir/a.png，去掉开头那个斜杠
        if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return path
    return text
