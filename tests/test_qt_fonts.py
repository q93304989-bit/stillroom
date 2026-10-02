"""离屏截图环境的字体回归测试。"""

from __future__ import annotations

from PySide6.QtGui import QFontDatabase

from app.ui.theme import pick_font_family


def test_offscreen_qt_exposes_a_chinese_font(qt_app):
    families = set(QFontDatabase.families())
    chinese_families = {
        "Microsoft YaHei UI",
        "Microsoft YaHei",
        "PingFang SC",
        "Source Han Sans SC",
        "Noto Sans CJK SC",
        "SimHei",
    }

    assert families & chinese_families, sorted(families)
    assert pick_font_family() in chinese_families
