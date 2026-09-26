"""Phase 4 验收：用真实界面 + 500 条历史记录测滚动帧时间。

沿用 Phase 0 的方法学（采集 `frameSwapped` 间隔 + 1ms 定时器探针），因此两组数字可以直接对比。

    python tools/phase4_bench.py                  # 离屏（软件渲染）
    python tools/phase4_bench.py --visible        # 真窗口（GPU + vsync）
    python tools/phase4_bench.py --records 200    # 换记录数
    python tools/phase4_bench.py --cold           # 不预生成缩略图，测首屏与后台补齐

测量项：
  1. 打开历史页到首帧的耗时（首屏可交互）
  2. 画廊匀速滚动 6 秒的帧时间分布（≥55fps / 无空白帧）
  3. 滚动期间 UI 线程阻塞探针（单帧任务是否被长任务占住）
  4. 冷启动时 500 张缩略图补齐要多久
"""
from __future__ import annotations

import argparse
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "phase0"))

from bench_qtquick import rss_mb, summarize          # noqa: E402
from PIL import Image                                # noqa: E402
from PySide6.QtCore import QObject, QTimer, QUrl     # noqa: E402
from PySide6.QtGui import QGuiApplication            # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine      # noqa: E402
from PySide6.QtQuickControls2 import QQuickStyle     # noqa: E402

from app.bootstrap import build_context              # noqa: E402
from app.services.history import Record              # noqa: E402
from app.ui.agent_bridge import AgentBridge          # noqa: E402
from app.ui.async_runner import AsyncRunner          # noqa: E402
from app.ui.bridge import UiBridge                   # noqa: E402
from app.ui.settings_bridge import SettingsBridge    # noqa: E402
from app.ui.theme import Theme                       # noqa: E402

QML_DIR = ROOT / "app" / "ui" / "qml"


def seed(context, count: int, *, make_thumbs: bool) -> float:
    """造 count 条记录（带真实图片），返回造数据耗时。"""
    started = time.perf_counter()
    media_dir = context.media.media_dir
    for index in range(count):
        path = media_dir / f"bench{index:04d}.jpg"
        if not path.exists():
            image = Image.new("RGB", (480, 360), (40 + index % 160, 90, 160))
            image.save(path, "JPEG", quality=80)
        context.history.add(
            Record(
                kind="image" if index % 4 else "video",
                prompt=f"第 {index} 条：日落时分的浮空城市与薄雾峡谷",
                media_path=str(path),
                params={"model": "agnes-image-2.5-flash", "size": "1024x768"},
                duration=3.0 + (index % 20) * 0.5,
            )
        )
        if make_thumbs:
            record_id = context.history.list(limit=1)[0].id
            thumb = context.media.thumbnail_for(record_id, path)
            if thumb:
                context.history.update(record_id, thumb_path=str(thumb))
    return time.perf_counter() - started


def pump(app: QGuiApplication, seconds: float) -> None:
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.002)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phase 4 历史页滚动基准")
    ap.add_argument("--records", type=int, default=500)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--speed", type=float, default=1200.0, help="滚动速度 px/s")
    ap.add_argument("--visible", action="store_true")
    ap.add_argument("--cold", action="store_true", help="不预生成缩略图")
    ap.add_argument("--keep", action="store_true", help="保留临时数据目录")
    args = ap.parse_args(argv)

    if not args.visible:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["QT_QUICK_BACKEND"] = "software"

    workdir = Path(tempfile.mkdtemp(prefix="agnes_phase4_"))
    env_file = workdir / ".env"
    env_file.write_text(
        "AGNES_API_KEY=sk-bench\nAGNES_BASE_URL=https://apihub.agnes-ai.com/v1\n", encoding="utf-8"
    )

    QQuickStyle.setStyle("Basic")
    app = QGuiApplication(sys.argv[:1])
    context = build_context(env_file=env_file, data_dir=workdir / "data")
    print(f"造数据：{args.records} 条…")
    seed_cost = seed(context, args.records, make_thumbs=not args.cold)
    print(f"  完成，耗时 {seed_cost:.1f}s（缩略图{'未' if args.cold else '已'}预生成）")

    runner = AsyncRunner()
    theme = Theme(dark=True)
    bridge = UiBridge(context, runner, theme)
    settings_bridge = SettingsBridge(context, runner, theme)
    agent_bridge = AgentBridge(context, runner, bridge)
    engine = QQmlApplicationEngine()
    engine.warnings.connect(lambda items: [print("QML:", w) for w in items])
    engine.rootContext().setContextProperty("backend", bridge)
    engine.rootContext().setContextProperty("theme", theme)
    engine.rootContext().setContextProperty("settingsBridge", settings_bridge)
    engine.rootContext().setContextProperty("agentBridge", agent_bridge)
    engine.addImportPath(str(QML_DIR))

    # ---------------- 打开历史页并等首帧 ----------------
    opened = time.perf_counter()
    engine.load(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")))
    root = engine.rootObjects()[0]
    stack = root.findChild(QObject, "pageStack")
    stack.setProperty("currentIndex", 3)          # 历史页（顺序：助手/图片/视频/历史/设置）

    frames: list[float] = []
    window = root
    window.frameSwapped.connect(lambda: frames.append(time.perf_counter()))
    pump(app, 0.8)
    first_frame = (frames[0] - opened) * 1000 if frames else -1

    history_model = bridge.historyModel
    deadline = time.perf_counter() + 30
    while history_model.count == 0 and time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.01)
    load_cost = time.perf_counter() - opened
    print(f"历史页：首帧 {first_frame:.0f}ms · 模型加载完成 {load_cost * 1000:.0f}ms · {history_model.count} 条")

    # 切到画廊
    page_item = stack.property("currentIndex")
    history_page = None
    for child in stack.children():
        if child.objectName() == "historyPage":
            history_page = child
    if history_page is not None:
        history_page.setProperty("mode", "gallery")
    pump(app, 0.6)

    gallery = root.findChild(QObject, "galleryView")
    if gallery is None:
        print("没找到画廊视图（objectName=galleryView），无法测滚动", file=sys.stderr)
        return 2

    # ---------------- 滚动采样 ----------------
    viewport_height = float(gallery.property("height") or 600)
    content_height = float(gallery.property("contentHeight") or 0)
    print(f"画廊：视口 {viewport_height:.0f}px · 内容 {content_height:.0f}px")

    frames.clear()
    scroll_started = time.perf_counter()
    last = scroll_started
    y = 0.0
    lag_samples: list[float] = []
    lag_state = {"last": time.perf_counter(), "active": True}

    def on_lag_tick() -> None:
        now = time.perf_counter()
        lag_samples.append((now - lag_state["last"]) * 1000)
        lag_state["last"] = now

    lag_timer = QTimer()
    lag_timer.setInterval(1)
    lag_timer.timeout.connect(on_lag_tick)
    lag_timer.start()

    deadline = scroll_started + args.seconds
    next_tick = time.perf_counter()
    while time.perf_counter() < deadline:
        app.processEvents()
        now = time.perf_counter()
        # 按 ~60Hz 推进滚动（真实拖动就是这个节奏）。若每个事件循环都改 contentY，
        # 软件渲染会被刷爆，测出来的是「刷新频率」而不是「滚动性能」。
        if now >= next_tick:
            delta = now - last
            last = now
            next_tick = now + 0.016
            y += args.speed * delta
            max_y = max(0.0, float(gallery.property("contentHeight") or 0) - viewport_height)
            if max_y > 0 and y > max_y:
                y = 0.0                            # 到底回到顶部，持续采样
            gallery.setProperty("contentY", y)
        time.sleep(0.001)

    lag_timer.stop()

    # ---------------- 冷启动补齐耗时 ----------------
    backfill = None
    if args.cold:
        started = time.perf_counter()
        deadline = started + 60
        while time.perf_counter() < deadline:
            app.processEvents()
            if context.history.count(keyword="") and _all_thumbs_ready(history_model):
                break
            time.sleep(0.02)
        backfill = time.perf_counter() - started

    stats = summarize("scroll", frames)
    print()
    print(f"{'阶段':<10}{'帧数':>7}{'FPS':>8}{'中位ms':>9}{'p95ms':>8}{'p99ms':>8}{'最大ms':>9}{'>16.7':>7}{'>33.3':>7}")
    print(
        f"{'画廊滚动':<10}{stats.get('frames', 0):>7}{stats.get('fps', 0):>8}"
        f"{stats.get('median_ms', 0):>9}{stats.get('p95_ms', 0):>8}{stats.get('p99_ms', 0):>8}"
        f"{stats.get('max_ms', 0):>9}{stats.get('over_16ms', 0):>7}{stats.get('over_33ms', 0):>7}"
    )
    if lag_samples:
        print(
            f"\nUI 线程探针：中位 {statistics.median(lag_samples):.2f}ms · "
            f"p99 {sorted(lag_samples)[int(len(lag_samples) * 0.99) - 1]:.1f}ms · "
            f"最大 {max(lag_samples):.1f}ms · >50ms {sum(1 for v in lag_samples if v > 50)} 次"
        )
    if backfill is not None:
        print(f"冷启动缩略图补齐：{backfill:.1f}s（{args.records} 条）")
    print(f"峰值内存：{rss_mb():.0f}MB")

    bridge.detach()
    runner.close()
    context.history.close()
    if args.keep:
        print(f"临时目录保留：{workdir}")
    else:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


def _all_thumbs_ready(model) -> bool:
    for row in range(model.count):
        index = model.index(row, 0)
        if model.data(index, model.KindRole) == "image" and not model.data(index, model.ThumbRole):
            return False
    return True


if __name__ == "__main__":
    sys.exit(main())
