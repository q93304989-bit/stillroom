"""渲染配色确认图（供人工确认后再做界面）。

色值**不在这里定义**——直接从 `app/ui/theme.py` 里 AST 解析出 DARK / LIGHT 两张表，
保证「确认的」和「代码里跑的」是同一份数据。

用法：

    python tools/design/palette_preview.py
    # 输出 docs/design/配色方案.png
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
THEME_FILE = ROOT / "app" / "ui" / "theme.py"
OUT_FILE = ROOT / "docs" / "design" / "配色方案.png"

CANVAS = (1460, 1300)
INK = "#111114"
INK_SOFT = "#6B6B72"
HOST_BG = "#EDEDF0"
LABEL_GRAY = "#5A5A62"

FONT_CANDIDATES = {
    "regular": [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"],
    "bold": [r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\simhei.ttf"],
    "mono": [r"C:\Windows\Fonts\consola.ttf", r"C:\Windows\Fonts\msyh.ttc"],
}

ROLE_ORDER = [
    ("bg", "页面底色"),
    ("card", "卡片底"),
    ("cardBorder", "卡片描边"),
    ("field", "输入框底"),
    ("fieldBorder", "输入框描边"),
    ("fill", "中性填充"),
    ("fillHover", "中性悬停"),
    ("textMain", "正文"),
    ("textSub", "次要文字"),
    ("textDisabled", "禁用文字"),
    ("accent", "强调色"),
    ("accentHover", "强调悬停"),
    ("accentSoft", "强调浅底"),
    ("onAccent", "强调上文字"),
    ("success", "成功文字"),
    ("successBg", "成功底"),
    ("warning", "警告文字"),
    ("danger", "危险色"),
    ("dangerText", "错误文字"),
    ("dangerBg", "错误底"),
    ("videoText", "视频徽标文字"),
    ("videoBg", "视频徽标底"),
    ("selected", "选中底"),
    ("scrollbar", "滚动条"),
    ("overlay", "浮层底"),
    ("tooltipBg", "提示气泡"),
]


def load_palettes() -> tuple[dict, dict]:
    tree = ast.parse(THEME_FILE.read_text(encoding="utf-8"))
    found: dict[str, dict] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ("DARK", "LIGHT"):
                    found[target.id] = ast.literal_eval(node.value)
    missing = {"DARK", "LIGHT"} - set(found)
    if missing:
        raise SystemExit(f"没能在 {THEME_FILE} 里找到 {missing}")
    return found["DARK"], found["LIGHT"]


def load_fonts() -> dict[str, ImageFont.FreeTypeFont]:
    fonts: dict[str, ImageFont.FreeTypeFont] = {}
    for kind, candidates in FONT_CANDIDATES.items():
        for path in candidates:
            if Path(path).exists():
                for size in (11, 12, 13, 14, 15, 16, 18, 22, 30):
                    fonts[f"{kind}{size}"] = ImageFont.truetype(path, size)
                break
        else:
            for size in (11, 12, 13, 14, 15, 16, 18, 22, 30):
                fonts[f"{kind}{size}"] = ImageFont.load_default()
    return fonts


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def readable_on(bg: str) -> str:
    r, g, b = hex_to_rgb(bg)
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#1D1D1F" if luminance > 0.6 else "#FFFFFF"


class Sheet:
    def __init__(self) -> None:
        self.image = Image.new("RGB", CANVAS, HOST_BG)
        self.draw = ImageDraw.Draw(self.image)
        self.fonts = load_fonts()

    def text(self, xy, s, *, size=13, color=INK, bold=False, mono=False, anchor="la") -> None:
        key = f"{'mono' if mono else ('bold' if bold else 'regular')}{size}"
        self.draw.text(xy, s, font=self.fonts.get(key), fill=color, anchor=anchor)

    def rounded(self, box, radius=10, fill=None, outline=None, width=1) -> None:
        self.draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)

    def chip(self, box, color: str) -> None:
        self.rounded(box, radius=6, fill=color, outline="#00000022", width=1)


def draw_swatches(sheet: Sheet, palette: dict, origin: tuple[int, int]) -> None:
    """两列色卡：色块 + 角色名 + 十六进制值。"""
    x0, y0 = origin
    column_width = 300
    row_height = 28
    per_column = 13
    for index, (key, label) in enumerate(ROLE_ORDER):
        color = palette.get(key)
        if not color:
            continue
        column, row = divmod(index, per_column)
        x = x0 + column * column_width
        y = y0 + row * row_height
        sheet.chip((x, y, x + 40, y + 20), color)
        sheet.text((x + 48, y + 1), label, size=12, color=INK)
        sheet.text((x + 48, y + 15), color.upper(), size=11, color=INK_SOFT, mono=True)


def draw_example(sheet: Sheet, palette: dict, origin: tuple[int, int], size: tuple[int, int]) -> None:
    """把配色套进一个小样：卡片、文字层级、按钮、输入框、状态胶囊、徽标。"""
    x, y = origin
    w, h = size
    sheet.rounded((x, y, x + w, y + h), radius=14, fill=palette["card"], outline=palette["cardBorder"])

    sheet.text((x + 20, y + 18), "生成结果预览", size=16, color=palette["textMain"], bold=True)
    sheet.text((x + 20, y + 44), "已完成 · 12.3 秒", size=12, color=palette["textSub"])

    # 输入框
    field_box = (x + 20, y + 70, x + w - 20, y + 108)
    sheet.rounded(field_box, radius=8, fill=palette["field"], outline=palette["fieldBorder"])
    sheet.text((x + 32, y + 82), "描述画面：主体 + 风格 + 光线 + 构图", size=12, color=palette["textDisabled"])

    # 按钮
    button_y = y + 122
    sheet.rounded((x + 20, button_y, x + 116, button_y + 34), radius=9, fill=palette["accent"])
    sheet.text((x + 68, button_y + 17), "生成", size=13, color=palette["onAccent"], bold=True, anchor="mm")
    sheet.rounded((x + 124, button_y, x + 200, button_y + 34), radius=9, fill=palette["fill"])
    sheet.text((x + 162, button_y + 17), "取消", size=13, color=palette["textMain"], anchor="mm")

    # 状态胶囊
    pill_y = button_y + 48
    sheet.rounded((x + 20, pill_y, x + 92, pill_y + 24), radius=12, fill=palette["successBg"])
    sheet.text((x + 56, pill_y + 12), "已完成", size=12, color=palette["success"], anchor="mm")
    sheet.rounded((x + 100, pill_y, x + 160, pill_y + 24), radius=12, fill=palette["dangerBg"])
    sheet.text((x + 130, pill_y + 12), "失败", size=12, color=palette["dangerText"], anchor="mm")
    sheet.rounded((x + 168, pill_y, x + 224, pill_y + 24), radius=12, fill=palette["videoBg"])
    sheet.text((x + 196, pill_y + 12), "视频", size=12, color=palette["videoText"], anchor="mm")

    sheet.text((x + 20, pill_y + 36), "服务端队列已满，10 秒后重试", size=12, color=palette["warning"])

    # 选中态
    selected_y = pill_y + 62
    sheet.rounded((x + 20, selected_y, x + w - 20, selected_y + 32), radius=8, fill=palette["selected"])
    sheet.text((x + 32, selected_y + 16), "选中的历史卡片", size=12, color=palette["textMain"], anchor="lm")

    # 次要文字与滚动条
    sheet.text((x + 20, selected_y + 44), "次要说明文字（12px / textSub）", size=12, color=palette["textSub"])
    sheet.rounded((x + w - 34, y + 70, x + w - 26, y + h - 24), radius=4, fill=palette["scrollbar"])


def draw_theme_panel(sheet: Sheet, palette: dict, box: tuple[int, int, int, int], title: str, note: str) -> None:
    x0, y0, x1, y1 = box
    sheet.rounded(box, radius=18, fill=palette["bg"], outline=palette["cardBorder"], width=2)
    sheet.text((x0 + 24, y0 + 20), title, size=18, color=palette["textMain"], bold=True)
    sheet.text((x0 + 24, y0 + 46), note, size=12, color=palette["textSub"])
    draw_swatches(sheet, palette, (x0 + 24, y0 + 78))
    draw_example(sheet, palette, (x0 + 24, y0 + 456), (x1 - x0 - 48, 250))


def draw_accent_options(sheet: Sheet, dark: dict, light: dict, y: int) -> None:
    """强调色候选：浅色/深色各画一个按钮，便于直接对比。"""
    options = [
        ("A 沿用旧版（现状）", dark["accent"], light["accent"], "深粉 + 浅蓝，两套不一致"),
        ("B 统一粉", "#FFD1DC", "#FF5A8C", "保留深色粉，浅色改用玫粉"),
        ("C 统一蓝", "#7FB3FF", "#0071E3", "两套都用系统蓝，最克制"),
        ("D 统一紫", "#C9A7FF", "#6A4BE0", "偏设计感，与生成类工具调性接近"),
    ]
    sheet.text((40, y), "强调色候选（当前代码里是 A；其余为可选项）", size=16, color=INK, bold=True)
    top = y + 30
    width = 340
    for index, (name, dark_accent, light_accent, note) in enumerate(options):
        x = 40 + index * (width + 12)
        sheet.rounded((x, top, x + width, top + 150), radius=12, fill="#FFFFFF", outline="#D2D2D7")
        sheet.text((x + 16, top + 14), name, size=14, color=INK, bold=True)

        # 深色按钮
        sheet.rounded((x + 16, top + 40, x + 96, top + 70), radius=9, fill=dark_accent)
        sheet.text((x + 56, top + 55), "生成", size=13, color=readable_on(dark_accent), bold=True, anchor="mm")
        sheet.text((x + 106, top + 55), f"深色 {dark_accent.upper()}", size=11, color=INK_SOFT, mono=True, anchor="lm")

        # 浅色按钮
        sheet.rounded((x + 16, top + 82, x + 96, top + 112), radius=9, fill=light_accent)
        sheet.text((x + 56, top + 97), "生成", size=13, color=readable_on(light_accent), bold=True, anchor="mm")
        sheet.text((x + 106, top + 97), f"浅色 {light_accent.upper()}", size=11, color=INK_SOFT, mono=True, anchor="lm")

        sheet.text((x + 16, top + 124), note, size=11, color=LABEL_GRAY)


def main() -> int:
    dark, light = load_palettes()
    sheet = Sheet()

    sheet.text((40, 28), "Agnes Studio 配色方案（待确认）", size=30, color=INK, bold=True)
    sheet.text(
        (40, 70),
        "色值逐项取自旧版 ui_theme，视觉延续；左侧色卡为全部角色，右侧为套用效果。",
        size=13,
        color=INK_SOFT,
    )

    draw_theme_panel(sheet, dark, (40, 104, 710, 830), "深色主题", "默认跟随系统；深色下强调色为粉色")
    draw_theme_panel(sheet, light, (750, 104, 1420, 830), "浅色主题", "浅色下强调色为 Apple 蓝（与深色不一致）")

    draw_accent_options(sheet, dark, light, 862)

    sheet.text((40, 1078), "需要你确认的几点", size=16, color=INK, bold=True)
    notes = [
        "1. 强调色：保持旧版的「深色粉 + 浅色蓝」，还是统一成一个色？（上图 A/B/C/D）",
        "2. 底色层次：页面 / 卡片 / 输入框三级抬升是否保留（深色尤其明显）？",
        "3. 语义色：成功绿、警告橙、失败红是否够用，还是需要「排队中」等更多状态色？",
        "4. 视频徽标用蓝色区分于图片，是否保留？",
    ]
    for index, line in enumerate(notes):
        sheet.text((40, 1108 + index * 26), line, size=13, color=INK_SOFT)

    sheet.text(
        (40, 1224),
        "确认后我会把选定方案写回 app/ui/theme.py，并按这套 token 实现 Phase 3 的界面；改色只需改这一个文件。",
        size=12,
        color=LABEL_GRAY,
    )
    sheet.text(
        (40, 1250),
        "注：本图的色值由脚本从代码里解析生成，不是另外维护的一份稿子——改了代码重跑脚本即可更新。",
        size=12,
        color=LABEL_GRAY,
    )

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    sheet.image.save(OUT_FILE)
    print(f"已生成 {OUT_FILE}")
    print(f"深色 {len(dark)} 个角色 / 浅色 {len(light)} 个角色")
    return 0


if __name__ == "__main__":
    sys.exit(main())
