"""设计 token（浅 / 深两套），供 QML 直接绑定。

色值**逐值沿用旧版 `ui_theme.py`**（本文件的 DARK / LIGHT 就是那张表的搬运），
保证视觉延续。机制则完全不同：旧版靠「模块级重新绑定名字 + 重建整个窗口」，
这里只是一个 QObject——换主题就是改属性，QML 绑定自动重算，**不重建窗口**。

⚠️ 待确认的一点：旧版浅色的强调色是 Apple 蓝 `#0071E3`，深色却是粉 `#FFD1DC`，
两套主题的强调色并不一致。这里先**原样保留**，等配色确认后再统一。
"""

from __future__ import annotations

from PySide6.QtCore import Property, QObject, Signal, Slot
from PySide6.QtGui import QFontDatabase

#: 字体偏好顺序（旧版也是这么挑的）：优先中文无衬线，找不到再退到系统默认
FONT_PREFERENCES = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "PingFang SC",
    "Source Han Sans SC",
    "Noto Sans CJK SC",
    "SimHei",
    "Segoe UI",
)


def pick_font_family() -> str:
    """按偏好挑一个可用字体族；离屏渲染没有字体库时返回空串（交给 Qt 兜底）。"""
    try:
        families = set(QFontDatabase.families())
    except Exception:                     # pragma: no cover - 极端环境下没有字体库
        return ""
    for name in FONT_PREFERENCES:
        if name in families:
            return name
    return ""


def detect_system_dark() -> bool:
    """跟随系统时的外观判断（Qt 6 能直接给出色彩方案）。"""
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication

        hints = QGuiApplication.styleHints()
        if hints is None:
            return True
        return hints.colorScheme() == Qt.ColorScheme.Dark
    except Exception:
        return True

DARK = {
    "bg": "#1A1A1C",
    "card": "#232326",
    "cardBorder": "#3A3A3C",
    "field": "#2C2C2E",
    "fieldBorder": "#48484A",
    "textMain": "#F5F5F7",
    "textSub": "#A8A8AD",
    "textDisabled": "#6E6E73",
    "accent": "#FFD1DC",
    "accentHover": "#FFB7C5",
    "accentSoft": "#46263A",
    "accentSoftHover": "#57304A",
    "onAccent": "#3A1225",
    "fill": "#333338",
    "fillHover": "#3D3D44",
    "success": "#32D74B",
    "successBg": "#24382A",
    "warning": "#FF9F0A",
    "danger": "#FF453A",
    "dangerText": "#FF6B61",
    "dangerBg": "#3A2628",
    "videoText": "#8AB9FF",
    "videoBg": "#22344E",
    "selected": "#46263A",
    "scrollbar": "#4A4A4F",
    "scrollbarHover": "#5A5A60",
    "overlay": "#0E0E10",
    "tooltipBg": "#38383D",
}

LIGHT = {
    "bg": "#F3F3F3",
    "card": "#FFFFFF",
    "cardBorder": "#D2D2D7",
    "field": "#F3F3F3",
    "fieldBorder": "#E5E5EA",
    "textMain": "#1D1D1F",
    "textSub": "#6E6E73",
    "textDisabled": "#AEAEB2",
    "accent": "#0071E3",
    "accentHover": "#0064D2",
    "accentSoft": "#F0F7FF",
    "accentSoftHover": "#E0EEFC",
    "onAccent": "#FFFFFF",
    "fill": "#F3F3F3",
    "fillHover": "#E5E5EA",
    "success": "#1E8E3E",
    "successBg": "#E8F7EC",
    "warning": "#A05A00",
    "danger": "#FF3B30",
    "dangerText": "#D0342C",
    "dangerBg": "#FDEBEB",
    "videoText": "#0B5BCB",
    "videoBg": "#E3F0FF",
    "selected": "#F0F7FF",
    "scrollbar": "#D8D8DE",
    "scrollbarHover": "#C6C6D0",
    "overlay": "#1D1D1F",
    "tooltipBg": "#1D1D1F",
}

#: 展示用（配色图按这个顺序渲染；键名 → 中文角色名）
ROLE_LABELS = {
    "bg": "页面底色",
    "card": "卡片底",
    "cardBorder": "卡片描边",
    "field": "输入框底",
    "fieldBorder": "输入框描边",
    "textMain": "正文",
    "textSub": "次要文字",
    "textDisabled": "禁用文字",
    "accent": "强调色",
    "accentHover": "强调悬停",
    "accentSoft": "强调浅底",
    "onAccent": "强调色上文字",
    "fill": "中性填充",
    "fillHover": "中性悬停",
    "success": "成功",
    "successBg": "成功底",
    "warning": "警告",
    "danger": "危险",
    "dangerText": "错误文字",
    "dangerBg": "错误底",
    "videoText": "视频徽标文字",
    "videoBg": "视频徽标底",
    "selected": "选中底",
    "scrollbar": "滚动条",
    "scrollbarHover": "滚动条悬停",
    "overlay": "浮层底",
    "tooltipBg": "提示气泡底",
}


class Theme(QObject):
    """主题对象：`dark` 一变，下面所有颜色属性一起发通知。"""

    changed = Signal()

    def __init__(self, dark: bool = True, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._dark = bool(dark)
        self._font_family = pick_font_family()

    # ---------------------------------------------------------------- 状态

    def _get_dark(self) -> bool:
        return self._dark

    def _set_dark(self, value: bool) -> None:
        value = bool(value)
        if value != self._dark:
            self._dark = value
            self.changed.emit()

    dark = Property(bool, _get_dark, _set_dark, notify=changed)

    fontFamily = Property(str, lambda self: self._font_family, constant=True)

    @Slot()
    def toggle(self) -> None:
        self._set_dark(not self._dark)

    @Slot(bool)
    def setDark(self, value: bool) -> None:
        self._set_dark(value)

    # ---------------------------------------------------------------- 颜色

    def _color(self, key: str) -> str:
        return (DARK if self._dark else LIGHT)[key]

    def _make(key: str, notify: Signal):
        def getter(self: "Theme") -> str:
            return self._color(key)

        return Property(str, getter, notify=notify)

    bg = _make("bg", changed)
    card = _make("card", changed)
    cardBorder = _make("cardBorder", changed)
    field = _make("field", changed)
    fieldBorder = _make("fieldBorder", changed)
    textMain = _make("textMain", changed)
    textSub = _make("textSub", changed)
    textDisabled = _make("textDisabled", changed)
    accent = _make("accent", changed)
    accentHover = _make("accentHover", changed)
    accentSoft = _make("accentSoft", changed)
    accentSoftHover = _make("accentSoftHover", changed)
    onAccent = _make("onAccent", changed)
    fill = _make("fill", changed)
    fillHover = _make("fillHover", changed)
    success = _make("success", changed)
    successBg = _make("successBg", changed)
    warning = _make("warning", changed)
    danger = _make("danger", changed)
    dangerText = _make("dangerText", changed)
    dangerBg = _make("dangerBg", changed)
    videoText = _make("videoText", changed)
    videoBg = _make("videoBg", changed)
    selected = _make("selected", changed)
    scrollbar = _make("scrollbar", changed)
    scrollbarHover = _make("scrollbarHover", changed)
    overlay = _make("overlay", changed)
    tooltipBg = _make("tooltipBg", changed)

    # ---------------------------------------------------------------- 几何常量

    radiusCard = Property(int, lambda self: 14, constant=True)
    radiusControl = Property(int, lambda self: 10, constant=True)
    radiusPill = Property(int, lambda self: 999, constant=True)
    spacing = Property(int, lambda self: 8, constant=True)
    pagePadding = Property(int, lambda self: 16, constant=True)
    cardPadding = Property(int, lambda self: 16, constant=True)
    controlHeight = Property(int, lambda self: 32, constant=True)
    buttonHeight = Property(int, lambda self: 36, constant=True)
