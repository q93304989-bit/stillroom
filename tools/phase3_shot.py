"""界面截图：离屏渲染真实界面，导出生成页（深/浅）、视频页与历史页（列表/画廊）。

    python tools/phase3_shot.py            # 输出到 docs/design/
    python tools/phase3_shot.py --empty    # 只看空态

两点说明：

1. 离屏平台没有字体库，中文会渲染成方块，所以这里给 Qt 指一个字体目录
   （只影响截图；真实窗口用系统字体库，不受影响）。
2. 为了在没有密钥的情况下也能看到「有结果」的样子，脚本往 bridge 注入了展示用状态，
   并本地绘制了一张演示图。这只是截图工具，不参与运行期逻辑，也不发任何请求。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
# 用本工具自己的 settings.json：否则截图会跟着开发机上的真实偏好变（模式、主题、
# 来源开关都可能不同），同一个命令就出不了同样的图。必须在导入 app.* 之前设。
os.environ.setdefault(
    "AGNES_SETTINGS_FILE",
    str(Path(__file__).resolve().parents[1] / "docs" / "design" / ".shotdata" / "settings.json"),
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QObject, QUrl  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine  # noqa: E402
from PySide6.QtQuickControls2 import QQuickStyle  # noqa: E402

from app.bootstrap import build_context  # noqa: E402
from app.services.context_store import ContextItem  # noqa: E402
from app.services.history import Record  # noqa: E402
from app.ui.async_runner import AsyncRunner  # noqa: E402
from app.ui.bridge import UiBridge, _View  # noqa: E402
from app.ui.agent_bridge import AgentBridge, _AgentView  # noqa: E402
from app.ui.knowledge_bridge import KnowledgeBridge  # noqa: E402
from app.ui.selfupdate_bridge import SelfUpdateBridge  # noqa: E402
from app.ui.settings_bridge import SettingsBridge  # noqa: E402
from app.ui.theme import Theme  # noqa: E402

QML_DIR = ROOT / "app" / "ui" / "qml"
OUT_DIR = ROOT / "docs" / "design"
SHOT_ROOT = OUT_DIR / ".shotdata"


def make_demo_image(path: Path, size: tuple[int, int] = (1024, 768)) -> None:
    """本地画一张演示图，让预览区有真实内容（不外链、不联网）。

    坐标按 1024×768 为基准随尺寸缩放，竖版（如助手页的「中秋海报」）也能用。
    """
    from PIL import Image, ImageDraw

    width, height = size
    sx, sy = width / 1024, height / 768
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / height
        draw.line(
            [(0, y), (width, y)],
            fill=(
                int(24 + 190 * ratio),
                int(40 + 90 * (1 - ratio)),
                int(72 + 140 * ratio),
            ),
        )
    for index in range(9):
        box = (
            (70 + index * 96) * sx, (400 - index * 27) * sy,
            (170 + index * 96) * sx, (580 - index * 27) * sy,
        )
        draw.rounded_rectangle(box, radius=16, outline=(255, 209, 220), width=3)
    draw.ellipse((width - 250 * sx, 70 * sy, width - 110 * sx, 210 * sy), fill=(255, 209, 220))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def demo_image_view(demo_file: Path) -> _View:
    return _View(
        job_id="job-demo-image",
        tool="image.generate",
        status="succeeded",
        progress=100,
        message="图片已生成",
        result_url="https://cdn.example/images/demo-1024x768.png",
        result_local=str(demo_file),
    )


def demo_video_view() -> _View:
    return _View(
        job_id="job-demo-video",
        tool="video.generate",
        status="succeeded",
        progress=100,
        message="视频已生成",
        result_url="https://cos-platform-outputs.agnes-ai.cn/videos/agnes-video-2.5/task_9f3c.mp4",
    )


def seed_history(context, count: int = 14) -> None:
    """造一批演示历史（含真实图片与缩略图），让历史页截图有内容。"""
    from PIL import Image, ImageDraw

    palettes = [
        ((24, 40, 72), (255, 209, 220), (72, 140, 200)),
        ((40, 24, 60), (180, 220, 255), (120, 80, 190)),
        ((20, 46, 40), (210, 255, 220), (60, 160, 120)),
    ]
    media_dir = context.media.media_dir
    for index in range(count):
        top, accent, bottom = palettes[index % len(palettes)]
        path = media_dir / f"demo{index:02d}.png"
        if not path.exists():
            image = Image.new("RGB", (960, 720))
            draw = ImageDraw.Draw(image)
            for y in range(720):
                ratio = y / 720
                draw.line(
                    [(0, y), (960, y)],
                    fill=(
                        int(top[0] + (bottom[0] - top[0]) * ratio),
                        int(top[1] + (bottom[1] - top[1]) * ratio),
                        int(top[2] + (bottom[2] - top[2]) * ratio),
                    ),
                )
            for step in range(7):
                draw.rounded_rectangle(
                    (90 + step * 110, 520 - step * 46, 200 + step * 110, 660 - step * 46),
                    radius=18,
                    outline=accent,
                    width=4,
                )
            draw.ellipse((700, 90, 850, 240), fill=accent)
            image.save(path)

        kind = "video" if index % 5 == 0 else "image"
        record = Record(
            kind=kind,
            prompt=f"第 {index + 1} 条：日落时分的浮空城市与薄雾峡谷",
            media_path=str(path),
            params={
                "model": "agnes-video-2.5-flash" if kind == "video" else "agnes-image-2.5-flash",
                "size": "1024x768",
                "seconds": "8" if kind == "video" else "",
            },
            duration=4.0 + index * 0.7,
            created_at=time.time() - index * 3600,
        )
        context.history.add(record)
        thumb = context.media.thumbnail_for(record.id, path)
        if thumb:
            context.history.update(record.id, thumb_path=str(thumb))
        # 给部分记录加上评价信号，截图里能看到收藏与标签
        if index % 3 == 0:
            context.history.set_feedback(
                record.id,
                favorite=True,
                tags=["中秋", "竖版"] if index % 6 == 0 else ["参考"],
                action="accept",
            )
        elif index % 3 == 1:
            context.history.set_feedback(record.id, tags=["待改进"], action="retry")


def demo_agent_view(demo_file: Path) -> _AgentView:
    """「中秋海报跑完了」的展示状态：结论 + 参考 + 调用统计。"""
    return _AgentView(
        run_id="run-demo-agent",
        status="succeeded",
        requirement="做一张中秋月饼的海报，竖版，国潮插画风格",
        kind="image",
        prompt=("国潮插画风格中秋海报，竖版构图：一轮满月悬于中式庭院上空，"
                "桂花树下石桌摆着月饼与茶壶，暖黄灯笼光晕，靛青与朱红配色，"
                "画面上方留出版式空间"),
        references=[
            "https://cdn.example/images/history-mid-autumn-01.png",
            "https://cdn.example/images/history-guochao-02.png",
        ],
        result_url="https://cdn.example/images/demo-agent-poster.png",
        result_local=str(demo_file),
        message="已完成。评估达标，直接交付。",
        description={
            "subject": "中秋庭院与月饼",
            "style": "国潮插画",
            "composition": "竖构图，满月居中偏上",
            "lighting": "暖色灯笼光",
            "flaws": [],
        },
        evaluation={"fits": 0.92, "quality": 3.0, "confidence": 0.85, "decision": "accept"},
        usage={"rag.search": 1, "llm.chat": 1, "image.generate": 1,
               "vision.describe": 1, "judge.ask": 1},
        attempts=1,
    )


def demo_agent_steps() -> list[dict]:
    """与 demo_agent_view 对应的六步时间线（带耗时与结论的完整版）。"""
    return [
        {"phase": "understand", "title": "理解需求",
         "detail": "类型=图片 · 竖版 · 信息足够，不用追问", "ok": True, "seconds": 1.2},
        {"phase": "reference", "title": "找参考",
         "detail": "命中 2 张历史图（「中秋」「国潮」标签）", "ok": True, "seconds": 0.8},
        {"phase": "prompt", "title": "写提示词",
         "detail": "按参考图补全构图、光线与配色描述", "ok": True, "seconds": 1.6},
        {"phase": "generate", "title": "生成",
         "detail": "agnes-image-2.5-flash · 竖版", "ok": True, "seconds": 6.4,
         "data": {"job_id": "job-demo-agent"}},
        {"phase": "evaluate", "title": "评估",
         "detail": "符合度=0.92 可用度=3.0 置信度=0.85 → 达标，直接交付",
         "ok": True, "seconds": 1.3},
        {"phase": "deliver", "title": "交付",
         "detail": "已写入历史，可随时「载入生成页」接着手动调", "ok": True, "seconds": 0.1},
    ]


def seed_self_update(context) -> None:
    """给自更新卡片造一份「有版本历史、也有待决定建议」的展示状态。

    只写本地库（截图工具不联网、不调用模型）：两次接受 → v1 被取代、v2 生效，
    再提一条新的建议，于是卡片上「当前版本 / 可回滚 / 待决定 / 版本历史」四块都有内容。
    """
    for index in range(12):
        record = context.history.add(Record(
            kind="image",
            prompt=f"中秋海报 {index + 1}：暖色灯笼、国潮插画",
            meta={"agent": {
                "run_id": f"shot-run-{index}",
                "requirement": f"中秋海报 {index + 1}",
                "evaluation": {
                    "fits": 0.75, "quality": 2.0,
                    "fix": "lighting" if index % 3 else "aspect", "confidence": 0.8,
                },
            }},
        ))
        context.history.set_feedback(record.id, action="accept" if index % 3 else "retry")

    store = context.prompts
    first = store.propose(
        {"composer_suffix": "光线描述要具体到光源与方向"},
        reason="最近 12 次里 7 次被判光线描述太笼统",
        evidence={"samples": 12, "accepts": 8, "retries": 4, "top_fixes": [["lighting", 7]]},
    )
    if first:
        store.accept(first["id"])
    second = store.propose(
        {"composer_suffix": "光线描述要具体到光源与方向", "aspect_preference": "9:16"},
        reason="最近 12 次里有 3 次画幅被改回竖版",
        evidence={"samples": 12, "accepts": 8, "retries": 4,
                  "top_fixes": [["lighting", 7], ["aspect", 3]]},
    )
    if second:
        store.accept(second["id"])
    store.propose(
        {
            "composer_suffix": "光线描述要具体到光源、色温与方向，别用「氛围感」这类词",
            "question_overrides": {"evaluate.fix": "最影响成品可用性的一处问题是什么？"},
        },
        reason="还有 3 次被判「光线说不出所以然」，建议把评价口径也换得更具体",
        evidence={"samples": 12, "accepts": 8, "retries": 4,
                  "top_fixes": [["lighting", 7], ["aspect", 3]]},
    )


def shoot_self_update(
    app: QGuiApplication,
    engine: QQmlApplicationEngine,
    selfupdate_bridge: SelfUpdateBridge,
    *,
    out: Path,
    dark: bool = True,
) -> None:
    """设置页「自更新」卡片截图：切到设置页、滚到卡片所在的位置再抓图。"""
    theme: Theme = engine.rootContext().contextProperty("theme")
    theme.setDark(dark)

    root = engine.rootObjects()[0]
    stack = root.findChild(QObject, "pageStack")
    if stack is not None:
        # 5 = 设置页（与 Main.qml 的 StackLayout 顺序一致：
        # 0 助手 / 1 图片 / 2 视频 / 3 历史 / 4 知识库 / 5 设置）
        stack.setProperty("currentIndex", 5)
    # 先刷一次再量高度：卡片的绑定是页面创建时算的，那会儿还没有建议，
    # 「待决定」块要靠这次刷新才会出现——它的高度得算进滚动范围里。
    selfupdate_bridge.refresh()
    pump(app, 1.2)

    scroll = root.findChild(QObject, "settingsScroll")
    if scroll is not None:
        content = scroll.property("contentItem")
        if content is not None:                      # 滚到底：自更新卡片是最后一组卡片
            full = float(content.property("contentHeight"))
            height = float(content.property("height"))
            content.setProperty("contentY", max(0.0, full - height))
    pump(app, 1.2)

    image = root.grabWindow()
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(out))
    print(f"已保存 {out.name}（{image.width()}×{image.height()}）"
          f"· 当前版本 v{selfupdate_bridge.currentVersion}"
          f"、{'有' if selfupdate_bridge.hasPending else '没有'}待决定建议")


def shoot_agent(
    app: QGuiApplication,
    engine: QQmlApplicationEngine,
    agent_bridge: AgentBridge,
    *,
    view: _AgentView,
    steps: list[dict],
    out: Path,
    dark: bool = True,
    context=None,
) -> None:
    """助手页截图：注入一份「跑完了」的展示状态，再抓图。"""
    theme: Theme = engine.rootContext().contextProperty("theme")
    theme.setDark(dark)

    root = engine.rootObjects()[0]
    stack = root.findChild(QObject, "pageStack")
    if stack is not None:
        stack.setProperty("currentIndex", 0)

    # 让「改参考再跑一次」这个新按钮在截图里也看得见：给这份展示状态落一份快照
    if context is not None:
        context.contexts.save_snapshot(
            view.run_id,
            view.requirement,
            [
                ContextItem(kind="history", ref=ref, title=f"历史参考图 {index}",
                            origin="历史作品")
                for index, ref in enumerate(view.references, 1)
            ],
            kind=view.kind,
        )
    agent_bridge._view = view                    # 仅截图用的展示状态
    agent_bridge._raw_steps = [dict(step) for step in steps]
    agent_bridge._live_steps = len(steps)
    agent_bridge.stepsReplaced.emit(list(steps))
    agent_bridge.stateChanged.emit()
    pump(app, 1.2)

    image = root.grabWindow()
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(out))
    print(f"已保存 {out.name}（{image.width()}×{image.height()}）· 步骤 {len(steps)} 条")


def pump(app: QGuiApplication, seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)


def seed_context_draft(context, agent_bridge: AgentBridge) -> None:
    """造一份「上下文草稿 + 一条长期档案」的展示状态（只写本地库，不联网、不发请求）。

    草稿模式（先看后跑）的界面要单独截一张：来源开关、可改需求、条目逐条删、
    「我补一句」、长期档案这几块，是第四期最该让人看见的东西。
    """
    context.contexts.create(
        "做一张中秋月饼的海报，竖版，国潮插画风格",
        [
            ContextItem(kind="history", ref="https://cdn.example/images/history-mid-autumn-01.png",
                        title="历史参考图 1", origin="历史作品 · 采纳过"),
            ContextItem(kind="history", ref="https://cdn.example/images/history-guochao-02.png",
                        title="历史参考图 2", origin="历史作品"),
            ContextItem(kind="history_text",
                        ref="国潮插画，靛青与朱红配色，暖黄灯笼光晕，画面上方留出版式空间",
                        title="国潮插画，靛青与朱红配色…", origin="历史提示词片段"),
            ContextItem(kind="kb", ref="国潮海报的配色以红金为主，饱和度要压低，留白要足够。",
                        title="国潮海报的配色以红金为主…",
                        origin="知识库《国潮风格说明》第 4 片"),
            ContextItem(kind="web", ref="2026 中秋主题视觉趋势：月饼与满月仍是主视觉，配色偏暖黄与深靛。",
                        title="中秋主题视觉趋势",
                        origin="外部线索 · https://example.com/mid-autumn-trends",
                        meta={"url": "https://example.com/mid-autumn-trends"}),
        ],
        notes="不要出现任何文字；月饼要放在画面下半部",
        sources={"history": True, "knowledge": True, "web": True, "web_images": False},
        decide={"reason": "本地命中 4 条 ≥ 阈值 2，没联网（省额度）"},
        kind="image",
        aspect="9:16",
    )
    context.contexts.save_profile(
        "中秋海报（固定要求）",
        [
            ContextItem(kind="kb", ref="国潮海报的配色以红金为主，饱和度要压低，留白要足够。",
                        title="国潮海报的配色以红金为主…", origin="知识库《国潮风格说明》第 4 片"),
            ContextItem(kind="manual", ref="月饼要放在画面下半部",
                        title="月饼要放在画面下半部", origin="你加的"),
        ],
        notes="不要出现任何文字；留白要够",
        is_default=False,
    )


def seed_knowledge(context) -> None:
    """往知识库放两份真资料（写进演示库目录，只在本工具自己的缓存里）。"""
    src_dir = SHOT_ROOT / "kb_src"
    src_dir.mkdir(parents=True, exist_ok=True)
    docs = {
        "国潮风格说明.md": (
            "# 国潮配色\n\n"
            "国潮海报的配色以红金为主，饱和度要压低，留白要足够。\n\n"
            "## 元素\n\n"
            "常用元素：祥云、回纹、灯笼、折扇；主视觉留一个焦点，不要堆满。\n\n"
            "## 字体\n\n"
            "标题用有笔锋的衬线体，正文尽量弱化，避免抢主视觉。\n"
        ),
        "中秋选题备忘.txt": (
            "中秋主题的视觉趋势：月饼与满月仍是主视觉，配色偏暖黄与深靛。\n"
            "去年竖版海报的点击率高于横版，版式建议上方留出文字空间。\n"
        ),
    }
    for name, text in docs.items():
        path = src_dir / name
        path.write_text(text, encoding="utf-8")
        existing = {doc.name for doc in context.knowledge.documents()}
        if name in existing:
            continue
        doc = context.knowledge.add(path)
        context.knowledge.build(doc.id)


def shoot_agent_draft(
    app: QGuiApplication,
    engine: QQmlApplicationEngine,
    agent_bridge: AgentBridge,
    context,
    *,
    out: Path,
    dark: bool = True,
) -> None:
    """助手页「上下文草稿」截图：摊开一份可编辑的草稿，抓图。"""
    theme: Theme = engine.rootContext().contextProperty("theme")
    theme.setDark(dark)

    root = engine.rootObjects()[0]
    stack = root.findChild(QObject, "pageStack")
    if stack is not None:
        stack.setProperty("currentIndex", 0)

    seed_context_draft(context, agent_bridge)
    draft = context.contexts.list(state="draft", limit=1)[0]
    agent_bridge._draft_id = draft.id
    agent_bridge._view = _AgentView(
        status="draft", requirement=draft.requirement, kind=draft.kind, message=""
    )
    # 草稿阶段本来就没有「跑完的步骤」：不清掉会把上一次运行的步骤留在截图里
    agent_bridge._raw_steps = []
    agent_bridge._live_steps = 0
    agent_bridge.stepsCleared.emit()
    agent_bridge.stateChanged.emit()
    agent_bridge.contextChanged.emit()
    agent_bridge.profilesChanged.emit()
    pump(app, 1.2)

    # 滚到卡片底部：长期档案那一块（存 / 套用 / 设为默认）是第四期的新面孔，
    # 不滚下去就在可视区外，截不到。
    scroll = root.findChild(QObject, "agentScroll")
    if scroll is not None:
        content = scroll.property("contentItem")
        if content is not None:
            full = float(content.property("contentHeight"))
            height = float(content.property("height"))
            content.setProperty("contentY", max(0.0, full - height))
    pump(app, 0.8)

    image = root.grabWindow()
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(out))
    print(f"已保存 {out.name}（{image.width()}×{image.height()}）· 草稿条目 {len(draft.items)} 条")


def shoot_knowledge(
    app: QGuiApplication,
    engine: QQmlApplicationEngine,
    knowledge_bridge: KnowledgeBridge,
    out: Path,
    *,
    dark: bool = True,
) -> None:
    """知识库页截图（先切页再抓图；资料与检索结果由调用方事先种好）。"""
    theme: Theme = engine.rootContext().contextProperty("theme")
    theme.setDark(dark)

    root = engine.rootObjects()[0]
    stack = root.findChild(QObject, "pageStack")
    if stack is not None:
        # 页序号跟 Main.qml 的 StackLayout 顺序一致：4 = 知识库
        stack.setProperty("currentIndex", 4)
    knowledge_bridge.refresh()
    pump(app, 1.2)

    image = root.grabWindow()
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(out))
    print(f"已保存 {out.name}（{image.width()}×{image.height()}）"
          f"· 资料 {len(knowledge_bridge.documents)} 份")


def shoot(
    app: QGuiApplication,
    engine: QQmlApplicationEngine,
    bridge: UiBridge,
    *,
    dark: bool,
    page: int,
    view: _View,
    out: Path,
) -> None:
    theme: Theme = engine.rootContext().contextProperty("theme")
    theme.setDark(dark)

    root = engine.rootObjects()[0]
    stack = root.findChild(QObject, "pageStack")
    if stack is not None:
        stack.setProperty("currentIndex", page)

    bridge._view = view                       # 仅截图用的展示状态
    bridge.stateChanged.emit()
    pump(app, 1.2)

    image = root.grabWindow()
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(out))
    print(f"已保存 {out.name}（{image.width()}×{image.height()}）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="导出界面截图")
    ap.add_argument("--empty", action="store_true", help="不注入示例结果")
    ap.add_argument("--no-history", action="store_true", help="跳过历史页截图")
    args = ap.parse_args(argv)

    QQuickStyle.setStyle("Basic")
    app = QGuiApplication(sys.argv[:1])

    SHOT_ROOT.mkdir(parents=True, exist_ok=True)
    env_file = SHOT_ROOT / ".env"
    if not env_file.exists():
        env_file.write_text(
            "AGNES_API_KEY=sk-preview-only\nAGNES_BASE_URL=https://apihub.agnes-ai.com/v1\n",
            encoding="utf-8",
        )
    # 演示库每次从干净状态重来：留着上次的数据，截图会越来越多（记录数、版本号一直涨），
    # 同样的命令就出不了同样的图。这里删的只是本工具自己的缓存目录里的库文件。
    for junk in ("history.db", "history.db-wal", "history.db-shm"):
        (SHOT_ROOT / junk).unlink(missing_ok=True)

    context = build_context(env_file=env_file, data_dir=SHOT_ROOT)
    runner = AsyncRunner()
    theme = Theme(dark=True)
    bridge = UiBridge(context, runner, theme)
    settings_bridge = SettingsBridge(context, runner, theme)
    agent_bridge = AgentBridge(context, runner, bridge)
    selfupdate_bridge = SelfUpdateBridge(context, runner)
    knowledge_bridge = KnowledgeBridge(context, runner)

    engine = QQmlApplicationEngine()
    # 桥挂到引擎下：Python 先回收桥的话，QML 会拿着 null 去刷绑定，退出时刷一屏报错
    for obj in (bridge, settings_bridge, agent_bridge, selfupdate_bridge, knowledge_bridge):
        obj.setParent(engine)
    engine.warnings.connect(lambda items: [print("QML:", w) for w in items])
    engine.rootContext().setContextProperty("backend", bridge)
    engine.rootContext().setContextProperty("theme", theme)
    engine.rootContext().setContextProperty("settingsBridge", settings_bridge)
    engine.rootContext().setContextProperty("agentBridge", agent_bridge)
    engine.rootContext().setContextProperty("selfUpdateBridge", selfupdate_bridge)
    engine.rootContext().setContextProperty("knowledgeBridge", knowledge_bridge)
    engine.addImportPath(str(QML_DIR))
    engine.load(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")))
    if not engine.rootObjects():
        print("QML 加载失败", file=sys.stderr)
        return 1

    demo_file = SHOT_ROOT / "demo.png"
    if not demo_file.exists():
        make_demo_image(demo_file)
    agent_demo_file = SHOT_ROOT / "demo-agent.png"
    if not agent_demo_file.exists():
        make_demo_image(agent_demo_file, (768, 1152))     # 竖版「中秋海报」

    empty = _View()
    image_view = empty if args.empty else demo_image_view(demo_file)
    video_view = empty if args.empty else demo_video_view()

    # 页码跟 Main.qml 的 StackLayout 顺序一致：
    # 0 助手 / 1 图片 / 2 视频 / 3 历史 / 4 知识库 / 5 设置
    if not args.empty:
        shoot_agent(app, engine, agent_bridge,
                    view=demo_agent_view(agent_demo_file), steps=demo_agent_steps(),
                    out=OUT_DIR / "界面-助手页.png", context=context)
        # 第四期：上下文草稿（先看后跑）与知识库页
        shoot_agent_draft(app, engine, agent_bridge, context,
                          out=OUT_DIR / "界面-上下文草稿.png")
    shoot(app, engine, bridge, dark=True, page=1, view=image_view, out=OUT_DIR / "界面-深色.png")
    shoot(app, engine, bridge, dark=False, page=1, view=image_view, out=OUT_DIR / "界面-浅色.png")
    shoot(app, engine, bridge, dark=True, page=2, view=video_view, out=OUT_DIR / "界面-视频页.png")

    if not args.empty:
        seed_knowledge(context)
        knowledge_bridge.testSearch("国潮 海报 配色")
        pump(app, 1.5)                     # 等检索试跑出结果
        shoot_knowledge(app, engine, knowledge_bridge, out=OUT_DIR / "界面-知识库.png")

    if not args.no_history:
        seed_history(context)
        history_model = bridge.historyModel

        def shoot_history(mode: str, out: Path, dark: bool) -> None:
            root = engine.rootObjects()[0]
            stack = root.findChild(QObject, "pageStack")
            stack.setProperty("currentIndex", 3)
            history_page = root.findChild(QObject, "historyPage")
            if history_page is not None:
                history_page.setProperty("mode", mode)
            theme.setDark(dark)
            history_model.reload("all", "", 500)
            pump(app, 3.0)                     # 等模型加载 + 缩略图补齐
            image = root.grabWindow()
            out.parent.mkdir(parents=True, exist_ok=True)
            image.save(str(out))
            print(f"已保存 {out.name}（{image.width()}×{image.height()}）· 记录 {history_model.count} 条")

        shoot_history("list", OUT_DIR / "界面-历史列表.png", True)
        shoot_history("gallery", OUT_DIR / "界面-历史画廊.png", True)

    # 放在历史页截图之后：自更新要往历史里塞 12 条「带信号的运行」演示数据，
    # 先塞会把历史页截图的画面也改掉。
    if not args.empty:
        seed_self_update(context)
        shoot_self_update(app, engine, selfupdate_bridge, out=OUT_DIR / "界面-自更新.png")

    bridge.detach()
    try:
        runner.run_blocking(context.aclose(), timeout=5.0)   # 连接池 + 两个数据库句柄
    except Exception:                                        # pragma: no cover
        context.history.close()
        context.prompts.close()
    runner.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
